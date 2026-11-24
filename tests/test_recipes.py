import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from anyio.from_thread import start_blocking_portal
from mcp.shared.memory import create_connected_server_and_client_session

from native_support import require_native
from src.wyrit.configured_host import configured_host
from src.wyrit.ledger import read_ledger
from src.wyrit.mcp_server import create_server
from src.wyrit.recipes import source_digest
from test_budgets import PEER
from test_live_sessions import FIXTURE


SCHEMA = {
    "type": "object", "properties": {"number": {"type": "integer"}},
    "required": ["number"], "additionalProperties": False,
}


@unittest.skipUnless(shutil.which("node"), "Node.js required for recipe proposals")
class RecipeTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.fixture = self.directory / "peer.py"
        self.fixture.write_text(FIXTURE)
        self.config = self.directory / "wyrit.toml"
        self.path = self.directory / "recipes" / "test-recipe.json"
        self.write_config()
        self.host = self.enterContext(configured_host(self.config))
        self.portal = self.enterContext(start_blocking_portal())
        server = create_server(self.host)
        self.client = self.enterContext(self.portal.wrap_async_context_manager(
            create_connected_server_and_client_session(server._mcp_server)
        ))

    def write_config(self, *, suffix="", extra_args=()):
        args = [str(self.fixture), str(self.directory / "pid"), str(self.directory / "calls"),
                *extra_args]
        self.config.write_text(
            'recipes_path = "recipes"\nledger_path = "runs.jsonl"\n'
            '[limits]\nmax_calls = 1\n'
            '[servers.peer]\nnamespace = "peer"\ntransport = "stdio"\nexpose = "all"\n'
            f'command = {json.dumps(sys.executable)}\nargs = {json.dumps(args)}\n'
            + suffix
        )

    def call(self, tool, **arguments):
        return self.portal.call(self.client.call_tool, tool, arguments)

    def result(self, tool, **arguments):
        response = self.call(tool, **arguments)
        self.assertFalse(response.isError, response)
        return json.loads(response.content[0].text)

    def proposal(self, source):
        return dict(name="test-recipe", description="A reviewed example", input_schema=SCHEMA, source=source)

    def approve(self):
        entry, = self.host.recipes(details=True)
        self.host.change_recipe("approve", entry["name"], entry["revision"], "operator", set())
        return json.loads(self.path.read_text())

    def description(self):
        return next(tool.description for tool in self.portal.call(self.client.list_tools).tools
                    if tool.name == "execute")

    def test_proposal_discovery_and_drift(self):
        request = self.proposal('return await tools.call("peer.original", args);')
        for statement in ('return tools.call(args.id, args);', 'const call = tools.call; return call(args.id, args);'):
            self.assertTrue(self.call("propose_recipe", **{**request, "source": statement}).isError)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.result("propose_recipe", **request)["state"], "pending")
        self.assertNotIn("approval", json.loads(self.path.read_text()))
        self.assertNotIn("test-recipe", self.description())
        self.assertEqual(self.result("search", query="test-recipe")["matches"], [])
        self.assertTrue(self.call("run_recipe", name="test-recipe", arguments={"number": 1}).isError)
        approved = self.approve()
        self.assertEqual(approved["approval"]["source_digest"], source_digest(approved))
        self.assertIn('test-recipe [read] ({"number"*: integer})', self.description())
        self.assertEqual(self.result("search", query="test-recipe")["matches"][0]["tool"], "run_recipe")
        self.assertTrue(self.call("propose_recipe", **request).isError)
        self.assertEqual(json.loads(self.path.read_text()), approved)
        invalid = self.call("run_recipe", name="test-recipe", arguments={"number": "2"})
        self.assertTrue(invalid.isError)
        self.assertIn("INVALID_ARGUMENTS", invalid.content[0].text)

        self.path.write_text(json.dumps({**approved, "source": "return 42;"}))
        entry, = self.host.recipes(details=True)
        self.assertEqual(entry["state"], "unapproved")
        self.assertIn("return 42", entry["diff"])
        self.assertNotIn("test-recipe", self.description())
        self.assertEqual(self.result("search", query="test-recipe")["matches"], [])
        self.path.write_text(json.dumps(approved))
        self.write_config(suffix='[servers.peer.capabilities.original]\nclassification = "write"\n')
        self.host.reload(self.config)
        entry, = self.host.recipes()
        self.assertEqual(entry["state"], "stale")
        self.assertIn("peer.original", entry["reason"])
        self.fixture.write_text(FIXTURE.replace('"type": "object"', '"type": "object", "maxProperties": 3'))
        self.write_config(extra_args=("restart",))
        self.host.reload(self.config)
        entry, = self.host.recipes()
        self.assertEqual(entry["state"], "stale")
        self.assertIn("peer.original", entry["reason"])
        self.assertNotIn("test-recipe", self.description())

    def test_approved_run_binds_guest_arguments(self):
        require_native(self)
        self.result("propose_recipe", **self.proposal('return {number: args.number + 1};'))
        approved = self.approve()
        result = self.result("run_recipe", name="test-recipe", arguments={"number": 4})
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["value"], {"number": 5})
        record, = read_ledger(self.directory / "runs.jsonl")
        self.assertEqual(record["recipe"], "test-recipe")
        self.assertEqual(record["source_digest"], source_digest(approved))

    def test_recipe_checks_undeclared_calls_and_enforces_policy_and_budget(self):
        self.result("propose_recipe", **self.proposal('return await tools.call("peer.change", args);'))
        approved = self.approve()
        denied = {"type": "call", "call_id": 1, "capability": "peer.change",
                  "args": {}, "expect_error": "DENIED"}
        undeclared = {"type": "call", "call_id": 1, "capability": "peer.original",
                      "args": {}, "expect_error": "RECIPE_UNDECLARED"}
        result = {"type": "result", "result": True}

        def run(frames):
            # Use the fixed protocol peer to test trusted admission without sandbox availability.
            with patch("src.wyrit.runtime.native_command") as launcher:
                launcher.return_value.__enter__.return_value = (
                    [sys.executable, "-I", "-c", PEER, json.dumps(frames)], (),
                )
                return self.result("run_recipe", name="test-recipe", arguments={"number": 1})

        self.assertEqual(run([undeclared, result])["value"], True)
        self.assertEqual(run([denied, result])["value"], True)
        self.assertEqual(run([denied, {**denied, "call_id": 2}])["limit_hit"]["name"], "max_calls")
        self.assertFalse((self.directory / "pid.requests").exists())
        rows = list(read_ledger(self.directory / "runs.jsonl"))
        self.assertEqual(rows[0]["actions"][0]["reason"], "RECIPE_UNDECLARED")
        self.assertEqual(rows[1]["actions"][0]["reason"], "WRITE_POLICY")
        self.assertTrue(all(row["recipe"] == "test-recipe" and
                            row["source_digest"] == source_digest(approved) for row in rows))
