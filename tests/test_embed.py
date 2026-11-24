import asyncio
from dataclasses import FrozenInstanceError
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from native_support import require_native
from src.wyrit.budgets import RunBudgets
from src.wyrit.embed import Capability, CapabilityError, Host, RunContext
from src.wyrit.invocation import CALL_CODES
from src.wyrit.ledger import read_ledger
from src.wyrit.native_sandbox import SandboxUnavailable
from src.wyrit.recipes import Recipes
from test_budgets import PEER


READ = Capability("data.read", "Read a row", {"type": "object"}, classification="read")
DETAILS = Capability("data.details", "Read extra details", True, classification="read")
WRITE = Capability("data.write", "Write a row", True)
ALICE = RunContext("audit-a", "principal-a", "tenant-a", {"private": ["context-canary"]})
BOB = RunContext("audit-b", "principal-b", "tenant-b")


class EmbedTests(unittest.IsolatedAsyncioTestCase):
    def test_context_is_deeply_immutable_and_catalog_is_scoped(self):
        attributes = {"nested": [{"value": "original"}]}
        context = RunContext("ref", "principal", "tenant", attributes)
        attributes["nested"][0]["value"] = "changed"
        self.assertEqual(context.attributes["nested"][0]["value"], "original")
        with self.assertRaises(TypeError):
            context.attributes["nested"][0]["value"] = "changed"
        with self.assertRaises(FrozenInstanceError):
            context.principal = "changed"

        host = Host(lambda context: [READ, DETAILS] if context is ALICE else [READ])
        self.assertIn("data.details", host.catalog_text(ALICE))
        self.assertNotIn("data.details", host.catalog_text(BOB))

    async def test_concurrent_contexts_are_isolated_and_audited(self):
        require_native(self)
        seen, records = [], []
        both_started = asyncio.Event()

        async def dispatch(capability, args, context):
            seen.append((capability, context))
            if capability == READ.id:
                if sum(cap == READ.id for cap, _ in seen) == 2:
                    both_started.set()
                await both_started.wait()
                return "row-a" if context is ALICE else "row-b"
            return "extra"

        host = Host(lambda ctx: [READ, DETAILS] if ctx is ALICE else [READ],
                    dispatch, ledger_sink=records.append)
        source = """
            const row = await tools.call("data.read", {});
            let details;
            try { details = await tools.call("data.details", {}); }
            catch (error) { details = [error.code, error.reason]; }
            return {row, details, globals: Reflect.ownKeys(globalThis).map(String),
                    types: [typeof context, typeof principal, typeof tenant, typeof attributes]};
        """
        first, second = await asyncio.gather(host.execute(source, ALICE), host.execute(source, BOB))
        self.assertEqual(first["status"], "SUCCESS", first)
        self.assertEqual(second["status"], "SUCCESS", second)
        self.assertEqual((first["value"]["row"], first["value"]["details"]), ("row-a", "extra"))
        self.assertEqual(second["value"]["row"], "row-b")
        self.assertEqual(second["value"]["details"], ["DENIED", "NOT_AUTHORIZED"])
        self.assertCountEqual([(cap, ctx.context_id) for cap, ctx in seen],
                              [(READ.id, "audit-a"), (READ.id, "audit-b"), (DETAILS.id, "audit-a")])
        for result, context in [(first, ALICE), (second, BOB)]:
            self.assertEqual(result["value"]["types"], ["undefined"] * 4)
            self.assertTrue(set(result["value"]["globals"]).isdisjoint(
                {"context", "context_id", "principal", "tenant", "attributes"}))
            for secret in (context.context_id, context.principal, context.tenant, "context-canary"):
                self.assertNotIn(secret, json.dumps(result))
            by_run = [record for record in records if record["run_id"] == result["run_id"]]
            self.assertEqual([record["type"] for record in by_run], ["run", "action", "action"])
            self.assertEqual({record["context_id"] for record in by_run}, {context.context_id})
            self.assertNotIn(context.principal, json.dumps(by_run))
            self.assertNotIn(context.tenant, json.dumps(by_run))

    async def test_dispatch_outcome_codes_and_exception_redaction(self):
        require_native(self)
        records = []

        async def dispatch(_capability, args, context):
            if args["code"] == "unexpected":
                raise RuntimeError(context.attributes["private"][0])
            raise CapabilityError(args["code"], "host-" + args["code"])

        host = Host([READ], dispatch, ledger_sink=records.append)
        codes = sorted(CALL_CODES) + ["unexpected"]
        result = await host.execute("""
            const outcomes = [];
            for (const code of """ + json.dumps(codes) + """) {
                try { await tools.call("data.read", {code}); }
                catch (error) { outcomes.push([error.code, error.reason ?? null, error.message]); }
            }
            return outcomes;
        """, ALICE)
        self.assertEqual(result["status"], "SUCCESS", result)
        outcomes = result["value"]
        self.assertEqual([item[:2] for item in outcomes],
                         [[code, "host-" + code] for code in sorted(CALL_CODES)]
                         + [["TOOL_ERROR", None]])
        self.assertEqual(set(result["unknown_calls"]), {
            codes.index("CALL_TIMEOUT") + 1, codes.index("OUTCOME_UNKNOWN") + 1,
        })
        self.assertNotIn("context-canary", json.dumps([result, records]))

    async def test_budgets_write_policy_and_default_ledger(self):
        require_native(self)
        seen = []

        async def dispatch(capability, args, context):
            seen.append(capability)
            return args

        echo = Capability("fake.echo", "Dispatch echo", True, classification="read")
        limited = Host([READ, WRITE, echo], dispatch, budgets=RunBudgets({"max_calls": 2}))
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_STATE_HOME": directory}):
            result = await limited.execute("""
                let rejection;
                try { await tools.call("data.write", {}); }
                catch (error) { rejection = error.reason; }
                return [rejection, await tools.call("fake.echo", 2)];
            """, ALICE)
            self.assertEqual(result["value"], ["WRITE_POLICY", 2], result)
            exhausted = await limited.execute("""
                for (let i = 0; i < 3; i++) await tools.call("data.read", {});
            """, BOB)
            records = list(read_ledger())
        self.assertEqual(seen, ["fake.echo", "data.read", "data.read"])
        self.assertEqual(exhausted["limit_hit"]["name"], "max_calls")
        self.assertEqual([record["context_id"] for record in records], ["audit-a", "audit-b"])
        self.assertEqual(records[0]["actions"][0]["reason"], "WRITE_POLICY")
        self.assertEqual(records[0]["actions"][0]["context_id"], "audit-a")

    async def test_timeout_keeps_unknown_outcome_and_does_not_retry(self):
        calls = 0

        async def dispatch(_capability, _args, _context):
            nonlocal calls
            calls += 1
            await asyncio.sleep(10)

        host = Host([READ], dispatch, budgets=RunBudgets({"call_timeout": 0.2}),
                    ledger_sink=lambda record: None)
        frames = [
            {"type": "call", "call_id": 1, "capability": READ.id, "args": {},
             "expect_error": "CALL_TIMEOUT"},
            {"type": "result", "result": True},
        ]
        # The fixed peer tests trusted dispatch without interpreting guest source.
        with (
            patch("src.wyrit.runtime.shutil.which", return_value=sys.executable),
            patch("src.wyrit.runtime.native_command") as launcher,
        ):
            launcher.return_value.__enter__.return_value = (
                [sys.executable, "-I", "-c", PEER, json.dumps(frames)], (),
            )
            result = await host.execute("", ALICE)
        self.assertIs(result["value"], True, result)
        self.assertEqual(result["unknown_calls"], [1])
        self.assertEqual(calls, 1)

    async def test_unavailable_sandbox_fails_closed(self):
        records = []

        async def dispatch(*_):
            self.fail("Dispatch reached without a native sandbox")

        host = Host([READ], dispatch, ledger_sink=records.append)
        with (
            patch("src.wyrit.runtime.shutil.which", return_value="/trusted/node"),
            patch("src.wyrit.runtime.native_command", side_effect=SandboxUnavailable("unavailable")),
            patch("src.wyrit.runtime.subprocess.Popen") as launch,
        ):
            result = await host.execute('return await tools.call("data.read", {});', ALICE)
        launch.assert_not_called()
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["actions"], [])
        self.assertEqual([(record["type"], record["context_id"]) for record in records],
                         [("run", ALICE.context_id)])

    async def test_recipes_require_approval_schema_and_current_catalog(self):
        require_native(self)
        with tempfile.TemporaryDirectory() as directory:
            recipes = Recipes(Path(directory))
            catalog = {cap.id: cap for cap in (READ, WRITE)}
            schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
            recipes.propose("sample", "Read a sample", schema, """
                let write;
                try { await tools.call("data.write", args); }
                catch (error) { write = error.reason; }
                return [write, await tools.call("data.read", args)];
            """, catalog, set())

            async def dispatch(capability, args, _context):
                self.assertEqual(capability, READ.id)
                return args["n"]

            records = []
            host = Host(lambda ctx: catalog.values() if ctx is ALICE else [READ], dispatch,
                        recipes=directory, ledger_sink=records.append)
            with self.assertRaisesRegex(ValueError, "pending"):
                await host.run_recipe("sample", {"n": 3}, ALICE)
            entry = recipes.get("sample", catalog, set())
            recipes.change("approve", "sample", entry["revision"], "reviewer", catalog, set())
            self.assertIn("sample [write]", host.catalog_text(ALICE))
            self.assertNotIn("sample [write]", host.catalog_text(BOB))
            with self.assertRaisesRegex(ValueError, "INVALID_ARGUMENTS"):
                await host.run_recipe("sample", {"n": "3"}, ALICE)
            with self.assertRaisesRegex(ValueError, "stale"):
                await host.run_recipe("sample", {"n": 3}, BOB)
            result = await host.run_recipe("sample", {"n": 3}, ALICE)
            self.assertEqual(result["value"], ["WRITE_POLICY", 3], result)
            self.assertEqual(records[0]["recipe"], "sample")
