"""Exercise live host management over real stdio MCP transports."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from anyio.from_thread import start_blocking_portal
from mcp.shared.memory import create_connected_server_and_client_session

from src.wyrit.capabilities import RunAuthorization
from src.wyrit.configured_host import configured_host
from src.wyrit.host_config import ConfigError
from src.wyrit.invocation import InvocationProtocol
from src.wyrit.ledger import read_ledger
from src.wyrit.mcp_server import create_server
from src.wyrit.native_sandbox import SandboxUnavailable


FIXTURE = """
import json
import os
from pathlib import Path
import sys
import time

pidfile, call_log = map(Path, sys.argv[1:3])
pidfile.write_text(str(os.getpid()))
tool = "original"

def send(**payload):
    print(json.dumps(dict(jsonrpc="2.0", **payload)), flush=True)

for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    params = request.get("params", {})
    if method == "initialize":
        result = {
            "protocolVersion": params["protocolVersion"],
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": "fixture", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": [{"name": name, "inputSchema": {"type": "object"}}
                            for name in (tool, "change")]}
    else:
        if params["arguments"].get("block"):
            with call_log.open("a") as log:
                log.write(str(os.getpid()) + "\\n")
            time.sleep(60)
        if params["name"] == "change":
            tool = "updated"
            send(method="notifications/tools/list_changed")
        result = {"content": [], "structuredContent": {"pid": os.getpid()}}
    send(id=request["id"], result=result)
"""


class LiveSessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.fixture = self.directory / "fake.py"
        self.fixture.write_text(FIXTURE, encoding="utf-8")
        self.config = self.directory / "host.toml"
        self.log = self.directory / "calls"

    def server(self, name, *, command=sys.executable, extra=()):
        args = [str(self.fixture), str(self.directory / name), str(self.log), *extra]
        return (
            f'[servers.{name}]\nnamespace = "{name}"\ntransport = "stdio"\n'
            f'expose = "all"\ncommand = {json.dumps(command)}\nargs = {json.dumps(args)}\n'
        )

    def write_config(self, *servers):
        self.config.write_text("".join(servers), encoding="utf-8")
        return self.config

    def wait_for(self, predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if result := predicate():
                return result
            time.sleep(0.02)
        self.fail("Timed out waiting for downstream session state")

    def test_drop_marks_action_unknown_and_reconnects_existing_mcp_server(self):
        host = self.enterContext(configured_host(self.write_config(
            self.server("fixture"), self.server("healthy"),
            self.server("broken", command="/usr/bin/false"),
        )))
        portal = self.enterContext(start_blocking_portal())
        server = create_server(host)
        client = self.enterContext(portal.wrap_async_context_manager(
            create_connected_server_and_client_session(server._mcp_server)
        ))

        def search(query):
            reply = portal.call(client.call_tool, "search", {"query": query})
            return json.loads(reply.content[0].text)

        self.assertEqual(search("broken")["unavailable"][0]["reason"],
                         "Downstream connection failed")
        self.assertEqual(search("fixture.original")["matches"][0]["id"], "fixture.original")
        pid = int((self.directory / "fixture").read_text())
        healthy = host.health["healthy"]
        _, dispatch = host.bind_run()
        protocol = InvocationProtocol(
            "run", RunAuthorization("run", {"fixture.original"}), dispatch=dispatch,
            deadline=time.monotonic() + 12,
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            action = pool.submit(protocol.respond, protocol.envelope(
                "call", call_id=1, capability="fixture.original", args={"block": True},
            ))
            self.wait_for(lambda: self.log.exists() and self.log.read_text())
            os.kill(pid, signal.SIGKILL)
            self.assertEqual(action.result(timeout=5)["code"], "OUTCOME_UNKNOWN")
        self.assertEqual(protocol.finalize_actions()[0].status, "UNKNOWN")
        unavailable = self.wait_for(lambda: search("fixture").get("unavailable"))
        self.assertIn("session lost", unavailable[0]["reason"])
        self.wait_for(lambda: "fixture.original" in host.ids)
        self.assertEqual(host.health["healthy"], healthy)
        self.assertEqual(search("fixture.original")["matches"][0]["id"], "fixture.original")
        result = host.dispatch("fixture.original", {}, deadline=time.monotonic() + 3)
        self.assertNotEqual(result["pid"], pid)
        self.assertEqual(self.log.read_text().splitlines(), [str(pid)])

        host.dispatch("fixture.change", {}, deadline=time.monotonic() + 3)
        self.wait_for(lambda: "fixture.updated" in host.ids)
        self.assertNotIn("fixture.original", host.ids)
        self.assertEqual(search("fixture.updated")["matches"][0]["id"], "fixture.updated")
        self.assertEqual(host.dispatch("fixture.updated", {}, deadline=time.monotonic() + 3), result)

    def test_reload_reconciles_sessions_and_rejects_invalid_config(self):
        kept, removed, added = map(self.server, ("kept", "removed", "added"))
        with configured_host(self.write_config(kept, removed)) as host:
            def pid(name):
                return int((self.directory / name).read_text())

            kept_pid, removed_pid = pid("kept"), pid("removed")
            kept_health = host.health["kept"]
            host.reload(self.write_config(kept, added))
            self.assertEqual(set(host.health), {"kept", "added"})
            self.assertEqual(host.health["kept"], kept_health)
            self.assertEqual(pid("kept"), kept_pid)
            self.assertIn("added.original", host.ids)
            with self.assertRaises(ProcessLookupError):
                os.kill(removed_pid, 0)

            added_pid = pid("added")
            broken = added.replace('namespace = "added"', 'namespace = "kept"')
            with self.assertRaises(ConfigError):
                host.reload(self.write_config(kept, broken))
            self.assertEqual(set(host.health), {"kept", "added"})
            self.assertEqual(host.dispatch("added.original", {}, deadline=time.monotonic() + 3),
                             {"pid": added_pid})
            host.reload(self.write_config(self.server("kept", extra=("changed",)), added))
            self.assertNotEqual(pid("kept"), kept_pid)
            self.assertEqual(pid("added"), added_pid)

    def test_execute_reads_reloaded_budgets_and_ledger_path(self):
        fixture = self.server("fixture")
        limits = '[servers.fixture.limits]\nmax_calls = {}\n'
        host = self.enterContext(configured_host(self.write_config(
            'ledger_path = "first.jsonl"\n', fixture, limits.format(2),
        )))
        pid = (self.directory / "fixture").read_text()
        _, _, previous, previous_path = host.bind_run(include_settings=True)
        host.reload(self.write_config('ledger_path = "second.jsonl"\n', fixture, limits.format(1)))
        self.assertEqual((self.directory / "fixture").read_text(), pid)
        self.assertEqual(previous.effective("fixture")["max_calls"].value, 2)
        self.assertEqual(previous_path, self.directory / "first.jsonl")

        portal = self.enterContext(start_blocking_portal())
        server = create_server(host)
        client = self.enterContext(portal.wrap_async_context_manager(
            create_connected_server_and_client_session(server._mcp_server)
        ))
        with (
            patch("src.wyrit.runtime.shutil.which", return_value="/trusted/node"),
            patch("src.wyrit.runtime.native_command", side_effect=SandboxUnavailable("sandbox unavailable")),
        ):
            reply = portal.call(client.call_tool, "execute", {"source": "return 42;"})
        self.assertFalse(reply.isError)
        result = json.loads(reply.content[0].text)
        self.assertEqual(result["limits"]["servers"]["fixture"]["max_calls"],
                         {"value": 1, "source": "server"})
        record, = read_ledger(self.directory / "second.jsonl")
        self.assertEqual(record["run_id"], result["run_id"])
        self.assertEqual(record["outcome"], "FAILED")
        self.assertFalse(previous_path.exists())
