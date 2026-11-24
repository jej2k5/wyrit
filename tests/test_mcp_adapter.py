import os
import sys
import unittest
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from native_support import require_native

from anyio.from_thread import start_blocking_portal
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from src.wyrit.capabilities import CapabilityRegistry, RunAuthorization
from src.wyrit.mcp_adapter import MCPAdapter
from src.wyrit.runtime import ActionRecord, RunResult, run_javascript


SERVER = r"""
import base64
import json
import os
import sys
import time

counts = {}
secret = os.environ["WYRIT_MCP_TEST_CREDENTIAL"]
integer = {"type": "integer"}
object_schema = {"type": "object"}
row_schema = {
    "type": "object", "properties": {"value": integer},
    "required": ["value"], "additionalProperties": False,
}
first = {
    "name": "increment", "title": "Increment value", "description": "Add one",
    "inputSchema": {
        "$defs": {"row": row_schema}, "$ref": "#/$defs/row",
    },
    "outputSchema": row_schema,
    "annotations": {"readOnlyHint": True}, "_meta": {"credential": secret},
}
second = [
    {"name": "format", "inputSchema": row_schema, "annotations": {"readOnlyHint": True}},
    {"name": "sample", "inputSchema": object_schema, "annotations": {"readOnlyHint": True}},
    {"name": "stats", "inputSchema": object_schema, "annotations": {"readOnlyHint": True}},
    {"name": "external-ref", "inputSchema": {"$ref": "https://example.invalid/schema"},
     "annotations": {"readOnlyHint": True}},
]

for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    reply = {"jsonrpc": "2.0", "id": request["id"]}
    params = request.get("params", {})
    method = request["method"]
    if method == "initialize":
        reply["result"] = {
            "protocolVersion": params["protocolVersion"], "capabilities": {"tools": {}},
            "serverInfo": {"name": "fixture", "version": "1"},
        }
    elif method == "tools/list":
        reply["result"] = (
            {"tools": second} if params.get("cursor") == "page-2"
            else {"tools": [first], "nextCursor": "page-2"}
        )
    elif method == "tools/call":
        assert secret == "host-test-canary"
        name, args = params["name"], params["arguments"]
        counts[name] = counts.get(name, 0) + 1
        result = {"content": []}
        if name == "increment":
            result.update(
                structuredContent={"value": args["value"] + 1 if args["value"] >= 0 else secret},
                content=[{"type": "text", "text": "fallback"}],
            )
        elif name == "format":
            result["content"] = [{"type": "text", "text": f'value={args["value"]}'}]
        elif name == "stats":
            result["structuredContent"] = counts
        elif name == "sample":
            case = args.get("case")
            if case == "rpc":
                reply["error"] = {"code": -32603, "message": secret}
            elif case == "failure":
                result.update(isError=True, content=[{"type": "text", "text": secret}])
            elif case == "slow":
                time.sleep(0.75)
                continue
            elif case == "resource":
                result["content"] = [{"type": "resource_link", "uri": "file:///etc/passwd",
                                      "name": "fixture"}]
            elif case == "text":
                result["content"] = [{"type": "text", "text": "x" * 65536}]
            elif case == "missing-content":
                result = {}
            elif case == "invalid-error-flag":
                result["isError"] = "false"
            else:
                data = {
                    "invalid": "!!!!", "large": base64.b64encode(b"x" * 8193).decode(),
                    "oversized-invalid": "A" * 12000 + "!!!!",
                    "aggregate": base64.b64encode(b"x" * 4097).decode(), "small": "AQI=",
                }[case]
                block = {"type": "image", "data": data, "mimeType": "image/png", "_meta": secret}
                result["content"] = [block, block] if case == "aggregate" else [block]
        if "error" not in reply:
            reply["result"] = result
    print(json.dumps(reply), flush=True)
"""


@asynccontextmanager
async def session_for_fixture():
    parameters = StdioServerParameters(
        command=sys.executable, args=["-u", "-c", SERVER],
        env={"WYRIT_MCP_TEST_CREDENTIAL": os.environ["WYRIT_MCP_TEST_CREDENTIAL"]},
    )
    async with AsyncExitStack() as stack:
        streams = await stack.enter_async_context(stdio_client(parameters))
        session = await stack.enter_async_context(ClientSession(*streams))
        await session.initialize()
        yield session


class MCPAdapterTests(unittest.TestCase):
    def setUp(self):
        require_native(self)
        stack = self.enterContext(ExitStack())
        self.ledger = Path(stack.enter_context(TemporaryDirectory())) / "runs.jsonl"
        stack.enter_context(patch.dict(os.environ, {"WYRIT_MCP_TEST_CREDENTIAL": "host-test-canary"}))
        portal = stack.enter_context(start_blocking_portal())
        session = stack.enter_context(portal.wrap_async_context_manager(session_for_fixture()))
        self.adapter = MCPAdapter("demo", session, portal)
        self.registry = CapabilityRegistry(self.adapter.capabilities)

    def run_js(self, source, *names, authorization=None, timeout=2.0):
        grant = authorization or self.registry.authorization_for("run", [f"demo.{n}" for n in names])
        return run_javascript(
            source, run_id="run", authorization=grant, dispatch=self.adapter.dispatch, timeout=timeout,
            ledger_path=self.ledger,
        )

    def test_discovers_and_composes_tools_without_guest_authority(self):
        descriptor = self.registry.describe("demo.increment")
        self.assertEqual(descriptor["description"], "Add one")
        self.assertEqual(descriptor["input_schema"]["$ref"], "#/$defs/row")
        self.assertIn("value", descriptor["output_schema"]["properties"])
        self.assertNotIn("adapter_metadata", descriptor)
        self.assertNotIn("host-test-canary", self.registry.to_json())

        self.assertEqual(self.run_js("""
            const row = await tools.call("demo.increment", {value: 41});
            const result = await tools.call("demo.format", row);
            const globals = [typeof process, typeof require, typeof fetch, typeof WebSocket];
            const imports = [];
            for (const module of ["node:fs", "node:net", "node:child_process"]) {
                try { await import(module); imports.push(false); }
                catch { imports.push(true); }
            }
            return {result, globals, imports};
        """, "increment", "format"), RunResult("SUCCESS", {
            "result": {"content": [{"type": "text", "text": "value=42"}]},
            "globals": ["undefined"] * 4, "imports": [True] * 3,
        }, actions=(
            ActionRecord(1, "demo.increment", "SUCCEEDED"),
            ActionRecord(2, "demo.format", "SUCCEEDED"),
        )))

    def test_malformed_results_keep_downstream_outcome_unknown(self):
        result = self.run_js("""
            const errors = [];
            for (const kind of ["missing-content", "invalid-error-flag", "oversized-invalid"]) {
                try { await tools.call("demo.sample", {case: kind}); }
                catch (error) { errors.push(error.message); }
            }
            return errors;
        """, "sample")
        self.assertEqual(result, RunResult(
            "SUCCESS", ["Invalid capability response"] * 3, unknown_calls=(1, 2, 3),
            actions=tuple(
                ActionRecord(call_id, "demo.sample", "UNKNOWN", "INVALID_RESULT")
                for call_id in (1, 2, 3)
            ),
        ))

    def test_denials_and_invalid_arguments_never_reach_mcp(self):
        self.assertEqual(self.run_js("""
            const messages = [];
            for (const [name, args] of [
                ["demo.format", {value: 1}], ["demo.missing", {}],
                ["demo.increment", []], ["demo.increment", {value: "1"}],
                ["demo.increment", {value: 1, extra: true}], ["demo.external-ref", {}]
            ]) {
                try { await tools.call(name, args); }
                catch (error) { messages.push(error.message); }
            }
            return [messages, await tools.call("demo.stats", {})];
        """, authorization=RunAuthorization("run", {
            "demo.increment", "demo.missing", "demo.stats", "demo.external-ref",
        })), RunResult("SUCCESS", [
            ["Capability call denied"] * 2 + ["Invalid capability arguments"] * 4,
            {"stats": 1},
        ], actions=(ActionRecord(7, "demo.stats", "SUCCEEDED"),)))
        self.assertEqual(
            self.run_js('return await tools.call("demo.increment", {value: 1});',
                        authorization=RunAuthorization("stale", {"demo.increment"})),
            RunResult("FAILED", reason="Invalid run authorization"),
        )

    def test_result_adaptation_and_sanitized_errors(self):
        self.assertEqual(self.run_js("""
            const small = await tools.call("demo.sample", {case: "small"});
            const errors = [];
            for (const kind of ["invalid", "resource", "large", "aggregate", "failure", "rpc"]) {
                try { await tools.call("demo.sample", {case: kind}); }
                catch (error) { errors.push(error.message); }
            }
            try { await tools.call("demo.increment", {value: -1}); }
            catch (error) { errors.push(error.message); }
            return [small, errors, await tools.call("demo.stats", {})];
        """, "sample", "increment", "stats"), RunResult("SUCCESS", [
            {"content": [{"type": "image", "data": "AQI=", "mimeType": "image/png"}]},
            ["Invalid capability response"] * 2 + ["Capability response too large"] * 2
            + ["Capability tool reported an error", "Capability outcome unknown",
               "Invalid capability response"],
            {"sample": 7, "increment": 1, "stats": 1},
        ], unknown_calls=(2, 7, 8), actions=(
            ActionRecord(1, "demo.sample", "SUCCEEDED"),
            ActionRecord(2, "demo.sample", "UNKNOWN", "INVALID_RESULT"),
            ActionRecord(3, "demo.sample", "SUCCEEDED", "INVALID_RESULT"),
            ActionRecord(4, "demo.sample", "SUCCEEDED", "RESULT_TOO_LARGE"),
            ActionRecord(5, "demo.sample", "SUCCEEDED", "RESULT_TOO_LARGE"),
            ActionRecord(6, "demo.sample", "FAILED", "TOOL_ERROR"),
            ActionRecord(7, "demo.sample", "UNKNOWN", "OUTCOME_UNKNOWN"),
            ActionRecord(8, "demo.increment", "UNKNOWN", "INVALID_RESULT"),
            ActionRecord(9, "demo.stats", "SUCCEEDED"),
        )))

    def test_oversized_result_stops_run_after_recording_successful_action(self):
        result = self.run_js("""
            try { await tools.call("demo.sample", {case: "text"}); } catch {}
            return await tools.call("demo.stats", {});
        """, "sample", "stats")
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.limit_hit["name"], "max_output_bytes")
        self.assertEqual(result.actions, (
            ActionRecord(1, "demo.sample", "SUCCEEDED", "RESULT_TOO_LARGE"),
        ))

    def test_deadline_preserves_unknown_call_and_stops_run(self):
        result = self.run_js(
            'return await tools.call("demo.sample", {case: "slow"});',
            "sample", timeout=0.4,
        )
        self.assertEqual(result, RunResult(
            "TIMED_OUT", reason="Run deadline exceeded", unknown_calls=(1,),
            actions=(ActionRecord(1, "demo.sample", "UNKNOWN", "OUTCOME_UNKNOWN"),),
        ))
        self.assertEqual(self.run_js(
            'return await tools.call("demo.stats", {});', "stats",
        ), RunResult("SUCCESS", {"sample": 1, "stats": 1},
                     actions=(ActionRecord(1, "demo.stats", "SUCCEEDED"),)))
