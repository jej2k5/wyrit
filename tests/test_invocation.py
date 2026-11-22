import json
import unittest
from unittest.mock import patch
from native_support import require_native

from src.wyrit.capabilities import RunAuthorization
from src.wyrit.invocation import InvocationProtocol, VERSION
from src.wyrit.runtime import RunResult, run_javascript


class InvocationTests(unittest.TestCase):
    def setUp(self):
        require_native(self)
        self.authorization = RunAuthorization("current-run", {"fake.echo", "fake.unknown"})
        self.call = {
            "version": VERSION, "type": "call", "run_id": "current-run",
            "call_id": 1, "capability": "fake.echo", "args": {"data": 7},
        }

    def run_js(self, source, authorization=None):
        return run_javascript(
            source, run_id="current-run", authorization=authorization or self.authorization,
        )

    def fake_child(self, wire, *, replay=False):
        driver = (
            'const input = require("node:readline").createInterface({input: process.stdin});'
            'let count = 0; input.on("line", () => {'
            f'process.stdout.write({json.dumps(wire + chr(10))});'
            f'if (++count === {2 if replay else 1}) input.close(), process.stdin.destroy();'
            '});'
        )
        with patch("src.wyrit.runtime.Path.read_text", return_value=driver):
            return self.run_js("return 1;")

    def test_rejects_spoofed_envelopes(self):
        invalid = (
            {"version": True}, {"version": 2}, {"type": "call_result"},
            {"run_id": "previous-run"}, {"run_id": "another-active-run"},
            {"call_id": True}, {"call_id": 2}, {"capability": ["fake.echo"]},
            {"authorization": ["fake.echo"]},
        )
        for fields in invalid:
            with self.subTest(fields=fields):
                self.assertEqual(
                    self.fake_child(json.dumps(self.call | fields)),
                    RunResult("FAILED", reason="Invalid runtime message"),
                )

    def test_rejects_replay_after_reply(self):
        self.assertEqual(
            self.fake_child(json.dumps(self.call), replay=True),
            RunResult("FAILED", reason="Invalid runtime message"),
        )

    def test_rejects_ambiguous_and_unsupported_serialized_values(self):
        for value in (
            '{"x":1,"x":2}', 'NaN', 'Infinity', '1e999', '9007199254740993',
            '[' * 65 + 'null' + ']' * 65,
        ):
            with self.subTest(value=value):
                wire = json.dumps(self.call | {"args": "PLACEHOLDER"}).replace(
                    '"PLACEHOLDER"', value,
                )
                self.assertEqual(
                    self.fake_child(wire), RunResult("FAILED", reason="Invalid runtime message"),
                )

    def test_unknown_and_ungranted_capabilities_are_denied(self):
        for capability, grant in (
            ("fake.unknown", self.authorization),
            ("fake.echo", RunAuthorization("current-run", set())),
        ):
            with self.subTest(capability=capability):
                source = f'''
                    try {{ await tools.call({json.dumps(capability)}, null); }}
                    catch (error) {{ return error.message; }}
                    return false;
                '''
                self.assertEqual(self.run_js(source, grant), RunResult("SUCCESS", "Capability call denied"))

    def test_child_rejects_mismatched_replies(self):
        respond = InvocationProtocol.respond
        for fields in ({"call_id": 2}, {"run_id": "stale"}, {"version": 2}, {"code": "DENIED"}):
            with self.subTest(fields=fields), patch.object(
                InvocationProtocol, "respond", lambda host, message: respond(host, message) | fields,
            ):
                self.assertEqual(
                    self.run_js('return await tools.call("fake.echo", null);').status, "FAILED",
                )

    def test_serialization_failure_does_not_consume_a_call_id(self):
        self.assertEqual(
            self.run_js("""
                try { await tools.call("fake.echo", { bad: 1n }); }
                catch {
                    return await tools.call("fake.echo", {run_id: "spoofed", call_id: 99});
                }
                return false;
            """),
            RunResult("SUCCESS", {"run_id": "spoofed", "call_id": 99}),
        )
