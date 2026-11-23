"""Offline coverage for monitoring projections, rotation and capture retention."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from src.wyrit.capture import DEFAULT_SOURCE_SECONDS, CaptureStore, RunCapture, sidecar
from src.wyrit.configured_host import configured_host
from src.wyrit.console.monitoring import Monitoring, percentile
from src.wyrit.ledger import append_record, open_ledger


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "runs.jsonl"
        self.monitor = Monitoring(self.path)
        self.addCleanup(self.monitor.close)
        self.now = 700_000

    @staticmethod
    def append(path, *records):
        with open_ledger(path) as ledger:
            for record in records:
                append_record(ledger, record)

    def run_row(self, name, age, actions=(), limit=None):
        return {"run_id": name, "started_at": self.now - age - 5, "ended_at": self.now - age,
                "outcome": "SUCCESS", "actions": list(actions), "limit_hit": limit}

    @staticmethod
    def action(outcome, duration, **fields):
        return {"outcome": outcome, "capability": "peer.tool", "duration_seconds": duration,
                "result_size_bytes": 12 if outcome == "SUCCEEDED" else None, **fields}

    def snapshot(self, window="hour", **kwargs):
        return self.monitor.snapshot("local", window, kwargs.pop("after", 0),
                                     kwargs.pop("epoch", ""), now=self.now, **kwargs)

    def test_views_use_recorded_outcomes_and_sources(self):
        ok, fail, deny, unknown = [self.action(status, duration, **fields) for status, duration, fields in (
            ("SUCCEEDED", 1, {}), ("FAILED", 2, {}),
            ("DENIED", 3, {"reason": "WRITE_POLICY", "call_id": 3}),
            ("UNKNOWN", 4, {"reason": "CONNECTION_DROP", "server": "peer"}),
        )]
        self.append(self.path,
                    self.run_row("prior-week", 90_000, [ok]),
                    self.run_row("prior-day", 5000, [ok]),
                    self.run_row("current", 120, [ok, fail, deny, unknown], {"name": "max_calls", "value": 4}),
                    self.run_row("legacy", 60, [self.action("DENIED", 1, reason=None) | {"capability": None}]))
        history = sidecar(self.path, ".health.jsonl")
        states = [("peer", "connected", 2, False), ("peer", "down", 0, False),
                  ("peer", "connected", 3, True), ("backup", "down", 0, False)]
        self.append(history, *[
            {"host": "local", "server": name, "status": status, "capabilities": caps,
             "at": self.now - 300 + index, "reconnect": reconnect}
            for index, (name, status, caps, reconnect) in enumerate(states)
        ])
        self.append(history, {"host": "other", "server": "backup", "status": "connected",
                              "capabilities": 20, "at": self.now, "reconnect": False})

        hour = self.snapshot()
        strip = hour["health"]
        self.assertEqual((strip["connected"], strip["exposed"], strip["runs"]), (1, 3, 2))
        self.assertEqual(strip["refs"], ["h3", "h4"])
        self.assertEqual(strip["run_refs"], ["r3", "r4"])
        self.assertEqual([item["label"] for item in strip["attention"]], [
            "backup is down", "FAILED action rate above the previous 23 hours",
            "DENIED action rate above the previous 23 hours",
            "Runs with writes denied in the last hour (1)", "Runs that hit max_calls (1)",
            "UNKNOWN actions in the last hour (1)",
        ])
        self.assertEqual([row["record"]["run_id"] for row in hour["feed"]], ["legacy", "current", "prior-day", "prior-week"])
        self.assertEqual(hour["capabilities"], [{
            "capability": "peer.tool", "calls": 4, "rates": {status: 0.25 for status in
                ("SUCCEEDED", "FAILED", "DENIED", "UNKNOWN")},
            "p50": 2, "p95": 4, "refs": ["r3"],
        }])
        self.assertEqual(self.snapshot("day")["capabilities"][0]["rates"]["SUCCEEDED"], 2 / 5)
        self.assertEqual(self.snapshot("week")["capabilities"][0]["calls"], 6)
        self.assertEqual(hour["servers"]["peer"]["reconnects"], ["h3"])
        self.assertEqual(hour["servers"]["peer"]["drops"], [{"ref": "r3", "call": 4, "capability": "peer.tool"}])
        self.assertEqual(hour["denials"], [{"ref": "r3", "call": 3, "capability": "peer.tool"}])
        self.assertEqual(hour["limits"], [{"ref": "r3", "name": "max_calls", "value": 4}])
        self.assertEqual(self.monitor.source("r3")["record"]["actions"][3], unknown)
        self.assertEqual(self.monitor.source("h3")["line"], 3)

    def test_attention_without_baseline(self):
        deadline = {"name": "run_deadline", "value": 5}
        denied = self.action("DENIED", 1, reason="WRITE_POLICY")
        self.append(self.path,
                    self.run_row("success", 7, [self.action("SUCCEEDED", 1)]),
                    *(self.run_row("timeout", age, limit=deadline) | {"outcome": "TIMED_OUT"}
                      for age in (6, 5, 4)),
                    self.run_row("write-one", 3, [denied]),
                    self.run_row("write-two", 2, [denied]),
                    self.run_row("failure", 1) | {"outcome": "FAILED"})
        self.assertEqual(self.snapshot()["health"]["attention"], [
            {"label": "DENIED actions in the last hour (2)", "refs": ["r5", "r6"]},
            {"label": "Failed runs in the last hour (1)", "refs": ["r7"]},
            {"label": "Runs with writes denied in the last hour (2)", "refs": ["r5", "r6"]},
            {"label": "Runs that hit run_deadline (3)", "refs": ["r2", "r3", "r4"]},
        ])

    def test_percentile_boundaries(self):
        self.assertIsNone(percentile([], 0.95))
        self.assertEqual(percentile([7], 0.50), 7)
        self.assertEqual(percentile(list(range(20, 0, -1)), 0.95), 19)

    def test_cursor_survives_rename_and_open_writer(self):
        with open_ledger(self.path) as old:
            append_record(old, self.run_row("old", 120))
            initial = self.snapshot()
            cursor = {"after": initial["cursor"], "epoch": initial["epoch"]}
            self.path.rename(self.path.with_suffix(".1"))
            self.assertEqual(self.snapshot(**cursor)["feed"], [])
            self.append(self.path, self.run_row("new", 60))
            append_record(old, self.run_row("late", 90))
            self.assertEqual(self.snapshot(paused=True, **cursor)["feed"], [])
            resumed = self.snapshot(**cursor)
            self.assertEqual([row["record"]["run_id"] for row in resumed["feed"]], ["new", "late"])
            self.assertEqual(resumed["cursor"], 3)
            self.assertNotEqual(self.monitor.source("r1")["inode"], self.monitor.source("r3")["inode"])
            self.assertEqual(self.monitor.source("r2")["line"], 2)
            self.assertEqual(self.monitor.source("r3")["line"], 1)

    def test_copy_truncate_and_incomplete_line(self):
        self.append(self.path, self.run_row("before", 10))
        first = self.snapshot()
        cursor = {"after": first["cursor"], "epoch": first["epoch"]}
        record = json.dumps(self.run_row("after-" + "x" * 150, 9)).encode()
        self.path.write_bytes(record)
        self.assertEqual(self.snapshot(**cursor)["feed"], [])
        with self.path.open("ab") as stream:
            stream.write(b"\n")
        self.assertEqual(self.snapshot(**cursor)["feed"][0]["line"], 1)
        self.assertEqual(self.snapshot(**cursor)["cursor"], 2)


class CaptureTests(unittest.TestCase):
    def test_running_host_purges_expired_rows_without_console_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "wyrit.toml"
            config.write_text('ledger_path = "runs.jsonl"\n')
            store = CaptureStore(root / "runs.jsonl", clock=lambda: 1)
            key = ("peer", "peer")
            store.configure(key, 60)
            store.save(key, "run", store.setting(key), {"arguments": {}}, set())
            store.configure_source(key, 60)
            store.save_source("run", "return 1;", {key}, store.source_policies())

            def captured_counts():
                with closing(sqlite3.connect(store.path)) as db:
                    return db.execute("SELECT (SELECT count(*) FROM payloads), (SELECT count(*) FROM sources)").fetchone()

            self.assertEqual(captured_counts(), (1, 1))
            with configured_host(config):
                deadline = time.monotonic() + 3
                while any(captured_counts()) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertEqual(captured_counts(), (0, 0))

    def test_opt_in_expiration_disable_and_secret_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.jsonl"
            now = 100
            store = CaptureStore(path, clock=lambda: now)
            key = ("Playground", "demo")
            run = RunCapture("run", "return 7;")
            run.start(path)
            run.store.clock = store.clock
            self.assertIsNone(run.sent(key, "demo.sample", {"input": 4}, set()))
            run.finish()
            self.assertEqual(store.payloads("run", [key]), [])
            self.assertEqual(store.sources(["run"])["run"],
                             {"text": "return 7;", "remaining_seconds": DEFAULT_SOURCE_SECONDS})
            store.configure(key, 60)
            setting = store.setting(key)
            self.assertEqual(store.settings(), {key: 60})
            fake = "ghp_" + "x" * 36
            store.save(key, "run", setting, {"arguments": {"value": fake}}, set())
            store.save(key, "run", setting, {"arguments": {"value": "private"}}, {"private"})
            store.save(key, "run", setting, {"arguments": {"value": 7}}, set())
            self.assertEqual(store.payloads("run", [key]), [{"arguments": {"value": 7}}])
            self.assertEqual(store.payloads("run", [("Other", "demo")]), [])
            now = 160
            self.assertEqual(store.payloads("run", [key]), [])
            store.purge()
            with closing(sqlite3.connect(store.path)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM payloads").fetchone(), (0,))
            store.configure(key, 0)
            store.configure(key, 30)
            store.save(key, "run", setting, {"arguments": "stale opt-in"}, set())
            self.assertEqual(store.payloads("run", [key]), [])
            run = RunCapture("fresh", "return 11;")
            run.start(path)
            run.store.clock = lambda: now
            captured = run.sent(key, "demo.sample", {"input": 4}, set())
            run.completed(captured, 11)
            run.finish()
            self.assertEqual(store.payloads("fresh", [key]), [{
                "server": key[0], "capability": "demo.sample",
                "arguments": {"input": 4}, "result": 11,
            }])
            store.configure(key, 0)
            self.assertEqual(store.payloads("fresh", [key]), [])


    def test_source_withholding_and_restrictive_server_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.jsonl"
            now = 100
            store = CaptureStore(path, clock=lambda: now)
            first, second = ("Peer", "demo"), ("Other", "other")
            store.configure(first, 60)
            store.configure_source(second, 600)
            actions = [{"capability": "demo.sample"}, {"capability": "other.sample"}]

            def capture(run_id, source, secrets=()):
                run = RunCapture(run_id, source)
                run.routes = {"demo.sample": first, "other.sample": second}
                run.secrets.update(secrets)
                run.start(path)
                run.store.clock = store.clock
                captured = run.sent(first, "demo.sample", {"input": 4}, set())
                run.completed(captured, 11)
                return run.finish(actions)

            fake = "ghp_" + "x" * 36
            resolved = "private-header-value"
            self.assertEqual(capture("shaped", f"// {fake}"), "withheld")
            self.assertEqual(capture("resolved", f"// {resolved}", [resolved]), "withheld")
            self.assertEqual(store.sources(["resolved", "shaped"]), {})
            self.assertEqual(store.payloads("shaped", [first]), [])
            source = "  const n = 11;\n  return n;"
            self.assertIsNone(capture("retained", source))
            self.assertEqual(store.sources(["retained"], preview=True)["retained"],
                             {"text": "  const n = 11;", "remaining_seconds": 600})
            self.assertEqual(store.sources(["retained"])["retained"]["text"], source)
            store.configure_source(second, 60)
            self.assertEqual(store.sources(["retained"])["retained"]["remaining_seconds"], 60)
            now = 160
            self.assertEqual(store.sources(["retained"]), {})
            capture("remove", source)
            store.configure(second, 60)
            store.save(second, "legacy", store.setting(second), {"arguments": {}, "source": source}, set())
            store.configure_source(second, 0)
            self.assertEqual(store.payloads("legacy", [second]), [{"arguments": {}}])
            with closing(sqlite3.connect(store.path)) as db:
                legacy, = db.execute("SELECT data FROM payloads WHERE run = 'legacy'").fetchone()
                self.assertNotIn("source", json.loads(legacy))
            self.assertEqual(store.sources(["remove"]), {})
            capture("disabled", source)
            self.assertEqual(store.sources(["disabled"]), {})
            self.assertTrue(store.payloads("disabled", [first]))
