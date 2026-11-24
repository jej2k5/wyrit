"""Exercise the optional pairing through the SDK's Streamable HTTP transport."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from anyio import run
import httpx
from mcp.shared.memory import create_connected_server_and_client_session

from native_support import require_native
from src.wyrit.capabilities import RunAuthorization
from src.wyrit.configured_host import configured_host
from src.wyrit.host_config import load_config
from src.wyrit.invocation import InvocationProtocol
from src.wyrit.mcp_server import create_server


class MCPxyPairingTests(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.ledger = directory / "runs.jsonl"
        self.path = directory / "wyrit.toml"
        self.path.write_text("""
ledger_path = "runs.jsonl"

[servers.proxy]
namespace = "github"
transport = "http"
url = "https://proxy.example.invalid/mcp/github"
expose = ["list_issues", "create_issue"]
deny_writes = false

[servers.proxy.headers]
Authorization = { env = "WYRIT_PAIRING_CANARY", prefix = "Bearer " }
""", encoding="utf-8")
        self.secret = "gh" + "p_" + "x" * 36
        self.traffic = []
        self.enterContext(patch.dict(os.environ, {"WYRIT_PAIRING_CANARY": self.secret}))
        self.config = load_config(self.path)
        client = httpx.AsyncClient
        self.enterContext(patch.object(
            httpx, "AsyncClient", side_effect=lambda **kwargs: client(
                transport=httpx.MockTransport(self.downstream), **kwargs,
            ),
        ))

    def downstream(self, request):
        if request.url.path != "/mcp/github":
            return httpx.Response(404)
        principals = {"Bearer " + self.secret: "wyrit-test-client"}
        principal = principals.get(request.headers.get("Authorization"))
        if principal is None:
            return httpx.Response(401)
        message = json.loads(request.content)
        if "id" not in message:
            return httpx.Response(202)
        reply = {"jsonrpc": "2.0", "id": message["id"]}
        params = message.get("params", {})
        match message["method"]:
            case "initialize":
                reply["result"] = {
                    "protocolVersion": params["protocolVersion"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fixture", "version": "1"},
                }
            case "tools/list":
                reply["result"] = {"tools": [
                    {"name": name, "inputSchema": {"type": "object"},
                     "annotations": {"readOnlyHint": name == "list_issues"}}
                    for name in ("list_issues", "create_issue")
                ]}
            case "tools/call":
                name = params["name"]
                self.traffic.append((principal, "github", name))
                if name == "create_issue":
                    reply["error"] = {
                        "code": -32003,
                        "message": ("internal error" if params["arguments"].get("rpc_failure")
                                    else "policy_blocked:write_denied"),
                        "data": {"canary": self.secret},
                    }
                else:
                    value = self.secret if params["arguments"].get("echo_credential") else {"issues": [42]}
                    reply["result"] = {"content": [], "structuredContent": value}
            case _:
                self.fail("Unexpected MCP method")
        return httpx.Response(200, json=reply)

    def test_authenticated_dispatch_and_guest_protocol(self):
        with patch.dict(os.environ, {"WYRIT_PAIRING_CANARY": "incorrect"}):
            with configured_host(load_config(self.path)) as rejected:
                self.assertEqual(rejected.health["proxy"].status, "down")
                self.assertEqual(rejected.ids, ())
        with configured_host(self.config) as host:
            ids, dispatch = host.bind_run()
            audit = []
            protocol = InvocationProtocol(
                "test", RunAuthorization("test", ids), dispatch=dispatch,
                deadline=time.monotonic() + 5, audit=audit,
            )
            replies = [
                protocol.respond({"run_id": "test", "call_id": index,
                                  "capability": "github." + tool, "args": args})
                for index, (tool, args) in enumerate([
                    ("list_issues", {}), ("create_issue", {}),
                    ("list_issues", {"echo_credential": True}),
                    ("create_issue", {"rpc_failure": True}),
                ], 1)
            ]
        self.assertEqual(replies[0]["result"], {"issues": [42]})
        self.assertEqual((replies[1]["code"], replies[1]["reason"]), ("DENIED", "WRITE_POLICY"))
        self.assertEqual(replies[2]["code"], "INVALID_RESULT")
        self.assertEqual(replies[3]["code"], "OUTCOME_UNKNOWN")
        self.assertEqual([row["outcome"] for row in audit],
                         ["SUCCEEDED", "DENIED", "UNKNOWN", "UNKNOWN"])
        self.assertNotIn(self.secret, json.dumps([replies, audit, self.traffic]))
        self.assertEqual(self.traffic, [
            ("wyrit-test-client", "github", name)
            for name in ("list_issues", "create_issue", "list_issues", "create_issue")
        ])

    def test_native_guest_and_ledger(self):
        require_native(self)

        async def execute(server):
            async with create_connected_server_and_client_session(server._mcp_server) as client:
                return await client.call_tool("execute", {"source": """
                    const read = await tools.call("github.list_issues", {});
                    const errors = [];
                    try { await tools.call("github.create_issue", {}); }
                    catch (error) { errors.push(error.message); }
                    try { await tools.call("github.list_issues", {echo_credential: true}); }
                    catch (error) { errors.push(error.message); }
                    return {read, errors, processType: typeof process};
                """})

        with configured_host(self.config) as host:
            reply = run(execute, create_server(host))
        self.assertFalse(reply.isError)
        result = getattr(reply, "structuredContent", None) or json.loads(reply.content[0].text)
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["value"], {
            "read": {"issues": [42]}, "processType": "undefined",
            "errors": ["Capability call denied", "Invalid capability response"],
        })
        self.assertEqual([action["status"] for action in result["actions"]],
                         ["SUCCEEDED", "DENIED", "UNKNOWN"])
        self.assertEqual(result["unknown_calls"], [3])
        ledger = self.ledger.read_text(encoding="utf-8")
        self.assertEqual(json.loads(ledger)["actions"][1]["reason"], "WRITE_POLICY")
        self.assertNotIn(self.secret, json.dumps(result))
        self.assertNotIn(self.secret, ledger)
