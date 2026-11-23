import json
import os
from pathlib import Path
import shutil
import stat
import sys
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from native_support import require_native
from src.wyrit.budgets import RunBudgets
from src.wyrit.capabilities import RunAuthorization
from src.wyrit.host_config import ConfigError, load_config
from src.wyrit.invocation import CallError, InvocationProtocol
from src.wyrit.ledger import read_ledger
from src.wyrit.runtime import run_javascript


# This fixed peer exchanges protocol messages without interpreting the source.
PEER = """
import json
import sys

init = json.loads(sys.stdin.readline())
envelope = {"version": 1, "run_id": init["run_id"]}
for frame in json.loads(sys.argv[1]):
    expected = frame.pop("expect_error", None)
    print(json.dumps({**envelope, **frame}), flush=True)
    if frame["type"] == "call":
        line = sys.stdin.readline()
        if not line:
            break
        reply = json.loads(line)
        if expected is not None and reply.get("code") != expected:
            sys.exit(2)
"""


class BudgetTests(unittest.TestCase):
    capability = "fixture.nested.write"

    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory(dir=os.getcwd())))
        self.ledger = self.directory / "runs.jsonl"

    def run_source(self, source, **options):
        return run_javascript(
            source, run_id="run-id", authorization=RunAuthorization("run-id", {self.capability}),
            ledger_path=self.ledger, **options,
        )

    def run_peer(self, frames, **options):
        with (
            patch("src.wyrit.runtime.shutil", wraps=shutil) as paths,
            patch("src.wyrit.runtime.native_command") as launcher,
        ):
            paths.which.return_value = sys.executable
            launcher.return_value.__enter__.return_value = (
                [sys.executable, "-I", "-c", PEER, json.dumps(frames)], (),
            )
            return self.run_source("generated-code-canary", **options)

    def call(self, call_id, args=None, capability=None):
        return {"type": "call", "call_id": call_id,
                "capability": capability or self.capability, "args": args}

    def test_config_precedence_and_validation(self):
        config = self.directory / "wyrit.toml"
        config.write_text("""
ledger_path = "runs.jsonl"
[limits]
run_deadline = 7
max_calls = 2
[servers.fixture]
namespace = "fixture.nested"
transport = "stdio"
command = "fixture"
expose = "all"
[servers.fixture.limits]
max_calls = 1
call_timeout = 0.25
""")
        loaded = load_config(config)
        self.assertEqual(loaded.ledger_path, self.ledger)
        budgets = loaded.budgets()
        self.assertEqual(budgets.report()["servers"]["fixture.nested"], {
            "run_deadline": {"value": 7, "source": "global"},
            "max_calls": {"value": 1, "source": "server"},
            "call_timeout": {"value": 0.25, "source": "server"},
            "max_output_bytes": {"value": 16384, "source": "default"},
        })
        calls = []
        result = self.run_peer([self.call(1), self.call(2)], budgets=budgets,
                               dispatch=lambda *_, **kw: calls.append(1))
        self.assertEqual(result.limit_hit, {
            "name": "max_calls", "value": 1, "source": "server", "namespace": "fixture.nested",
        })
        self.assertEqual(calls, [1])
        for invalid in ("true", "0", "1.5", "nan", "-1"):
            config.write_text("[limits]\\nmax_calls = " + invalid)
            with self.subTest(invalid=invalid), self.assertRaises(ConfigError):
                load_config(config)

    def test_server_deadline_stops_run_and_preserves_outcomes(self):
        release = Event()

        def dispatch(_, args, *, deadline):
            if args == 2:
                release.wait(3)
            return True

        try:
            result = self.run_peer([self.call(1, 1), self.call(2, 2)], dispatch=dispatch,
                                   budgets=RunBudgets(server_limits={
                                       "fixture.nested": {"run_deadline": 0.6},
                                   }))
        finally:
            release.set()
        self.assertEqual(result.status, "TIMED_OUT")
        self.assertEqual(result.limit_hit["name"], "run_deadline")
        self.assertEqual(result.limit_hit["source"], "server")
        self.assertEqual([row.status for row in result.actions], ["SUCCEEDED", "UNKNOWN"])

    def test_call_timeout_leaves_sent_call_unknown_and_allows_next_call(self):
        release = Event()
        calls = []

        def dispatch(_, args, *, deadline):
            calls.append(args)
            if args == "first":
                release.wait(3)
            return "result-canary"

        try:
            result = self.run_peer(
                [dict(self.call(1, "first"), expect_error="CALL_TIMEOUT"), self.call(2, "second"),
                 {"type": "result", "result": "ok"}],
                dispatch=dispatch, budgets=RunBudgets({"call_timeout": 0.15}),
            )
        finally:
            release.set()
        self.assertEqual(calls, ["first", "second"])
        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(result.limit_hit["name"], "call_timeout")
        self.assertEqual(result.actions[0].code, "CALL_TIMEOUT")
        self.assertEqual(result.unknown_calls, (1,))
        record, = read_ledger(self.ledger)
        self.assertEqual(record["limit_hit"], result.limit_hit)
        self.assertEqual([row["outcome"] for row in record["actions"]], ["UNKNOWN", "SUCCEEDED"])

    def test_timeout_before_send_does_not_leave_an_unknown_action(self):
        release = Event()
        finished = Event()
        sent = []

        def dispatch(_, args, *, deadline, on_send):
            release.wait(3)
            try:
                on_send()
                sent.append(args)
            finally:
                finished.set()

        try:
            result = self.run_peer(
                [self.call(1), {"type": "result", "result": "ok"}], dispatch=dispatch,
                tracks_send=True, budgets=RunBudgets({"call_timeout": 0.1}),
            )
        finally:
            release.set()
        self.assertTrue(finished.wait(3))
        self.assertEqual(sent, [])
        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(result.actions, ())
        record, = read_ledger(self.ledger)
        self.assertEqual(record["actions"][0]["outcome"], "FAILED")

    def check_run_interruption_before_send(self, expected, *, timeout, cancel_event=None):
        release = Event()
        blocked = Event()
        finished = Event()
        sent = []

        def dispatch(_, args, *, deadline, on_send):
            if args == 2:
                blocked.set()
                if cancel_event is not None:
                    cancel_event.set()
                release.wait(5)
            try:
                on_send()
                sent.append(args)
                return True
            finally:
                if args == 2:
                    finished.set()

        try:
            result = self.run_peer([self.call(1, 1), self.call(2, 2)], dispatch=dispatch,
                                   tracks_send=True, timeout=timeout, cancel_event=cancel_event)
            self.assertTrue(blocked.is_set())
            self.assertFalse(finished.is_set())
        finally:
            release.set()
        self.assertTrue(finished.wait(3))
        self.assertEqual(sent, [1])
        self.assertEqual(result.status, expected)
        if expected == "TIMED_OUT":
            self.assertEqual(result.limit_hit["name"], "run_deadline")
        else:
            self.assertTrue(result.cancellation_requested)
            self.assertTrue(result.cancellation_confirmed)
        self.assertEqual([(a.call_id, a.status) for a in result.actions], [(1, "SUCCEEDED")])
        self.assertEqual(result.unknown_calls, ())
        record, = read_ledger(self.ledger)
        self.assertEqual(record["outcome"], expected)
        self.assertEqual(record["limit_hit"], result.limit_hit)
        self.assertEqual([row["outcome"] for row in record["actions"]], ["SUCCEEDED", "FAILED"])

    def test_run_deadline_before_send_preserves_actions_and_ledger(self):
        self.check_run_interruption_before_send("TIMED_OUT", timeout=1)

    def test_cancellation_before_send_preserves_actions_and_ledger(self):
        self.check_run_interruption_before_send("CANCELLED", timeout=3, cancel_event=Event())

    def test_unexpected_failure_preserves_actions_and_ledger(self):
        respond = InvocationProtocol.respond

        def fail_after_call(protocol, message):
            respond(protocol, message)
            raise RuntimeError("private-exception-canary")

        with patch.object(InvocationProtocol, "respond", fail_after_call):
            result = self.run_peer([self.call(1)], dispatch=lambda *args, **kwargs: True)
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.reason, "Unexpected runtime failure")
        self.assertEqual([action.status for action in result.actions], ["SUCCEEDED"])
        record, = read_ledger(self.ledger)
        self.assertEqual(record["outcome"], "FAILED")
        self.assertEqual([row["outcome"] for row in record["actions"]], ["SUCCEEDED"])
        self.assertNotIn("private-exception-canary", self.ledger.read_text())

    def test_output_limits_and_ledger_redaction(self):
        def dispatch(_, args, *, deadline):
            if args == "error-canary":
                raise CallError("TOOL_ERROR")
            return "result-canary"

        result = self.run_peer([
            self.call(1, "argument-canary", "unknown.canary"), self.call(2, "error-canary"),
            self.call(3, "argument-canary"),
        ], budgets=RunBudgets({"max_output_bytes": 1024}, {
            "fixture.nested": {"max_output_bytes": 5},
        }), dispatch=dispatch)
        self.assertEqual(result.limit_hit, {"name": "max_output_bytes",
                                           "source": "server", "value": 5,
                                           "namespace": "fixture.nested"})
        self.assertEqual(result.actions[-1].status, "SUCCEEDED")
        final = self.run_peer([{"type": "result", "result": "final-value-canary"}],
                              budgets=RunBudgets({"max_output_bytes": 6}))
        self.assertEqual(final.limit_hit["name"], "max_output_bytes")
        self.assertEqual(final.limit_hit["source"], "global")
        first, second = read_ledger(self.ledger)
        self.assertEqual(first["run_id"], "run-id")
        self.assertLessEqual(first["started_at"], first["ended_at"])
        self.assertEqual(first["outcome"], "FAILED")
        self.assertEqual(first["limit_hit"], result.limit_hit)
        self.assertEqual([row["outcome"] for row in first["actions"]], ["DENIED", "FAILED", "SUCCEEDED"])
        self.assertEqual(first["actions"][-1]["capability"], self.capability)
        self.assertEqual(first["actions"][-1]["result_size_bytes"], len(b'"result-canary"'))
        self.assertTrue(all(row["duration_seconds"] >= 0 for row in first["actions"]))
        self.assertEqual(second["actions"], [])
        self.assertEqual(stat.S_IMODE(self.ledger.stat().st_mode), 0o600)
        self.assertNotIn("canary", self.ledger.read_text())

    def test_default_ledger_includes_run_that_fails_before_launch(self):
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.directory)}):
            result = run_javascript("generated-code-canary", run_id="run-id",
                                    authorization=RunAuthorization("wrong-id", ()))
            record, = read_ledger()
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(record["run_id"], "run-id")
        self.assertEqual(record["outcome"], "FAILED")
        self.assertEqual(record["actions"], [])
        self.assertTrue((self.directory / "wyrit" / "runs.jsonl").is_file())

    def test_guest_catches_call_timeout_and_large_final_result_is_structured(self):
        require_native(self)
        release = Event()
        try:
            result = self.run_source("""
                try { await tools.call("fixture.nested.write", {}); }
                catch (error) { return error.message; }
            """, budgets=RunBudgets({"call_timeout": 0.15}),
                dispatch=lambda *args, **kwargs: release.wait(3))
        finally:
            release.set()
        self.assertEqual(result.value, "Capability call timed out")
        self.assertEqual(result.unknown_calls, (1,))
        result = self.run_source('return "x".repeat(100000);')
        self.assertEqual(result.limit_hit["name"], "max_output_bytes")
