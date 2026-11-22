import os
import unittest
from unittest.mock import patch

from src.wyrit.capabilities import Capability, CapabilityRegistry
from src.wyrit.runtime import ECHO_CAPABILITY, RunResult, run_javascript


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        registry = CapabilityRegistry([Capability(ECHO_CAPABILITY, "Echo", True, True)])
        self.granted = registry.authorization_for("run", [ECHO_CAPABILITY])
        self.denied = registry.authorization_for("run", [])

    def run_js(self, source, **options):
        return run_javascript(source, **({"run_id": "run", "authorization": self.granted} | options))

    def test_echo_and_disposable_context(self):
        self.assertEqual(
            self.run_js("""
                globalThis.marker = true;
                const first = await tools.call("fake.echo", {words: ["hello", null]});
                return await tools.call("fake.echo", first);
            """),
            RunResult("SUCCESS", {"words": ["hello", None]}),
        )
        self.assertEqual(self.run_js("return typeof marker;"), RunResult("SUCCESS", "undefined"))

    def test_missing_and_cross_run_grants_fail(self):
        source = 'return await tools.call("fake.echo", {});'
        self.assertEqual(self.run_js(source, authorization=self.denied).status, "FAILED")
        self.assertEqual(self.run_js(source, run_id="another").reason, "Invalid run authorization")

    def test_deadline_covers_sync_and_async_loops(self):
        for source in ("while (true) {}", "while (true) { await 0; }",
                       "await new Promise(() => {});"):
            with self.subTest(source=source):
                self.assertEqual(self.run_js(source, timeout=0.25).status, "TIMED_OUT")

    def test_ambient_authority_and_constructor_attempts(self):
        with patch.dict(os.environ, {"WYrit_TEST_SECRET": "credential-canary"}):
            result = self.run_js("""
                const probes = [
                    () => process.env.WYrit_TEST_SECRET,
                    () => require("node:fs").readFileSync("/etc/passwd", "utf8"),
                    () => fetch("http://127.0.0.1/"),
                    () => new WebSocket("ws://127.0.0.1/"),
                    () => require("node:child_process").spawnSync("sh"),
                    () => require("lodash"),
                    () => globalThis.constructor.constructor("return process")(),
                    () => tools.call.constructor("return process")(),
                    () => eval("process"),
                    () => WebAssembly.compile(new Uint8Array([0,97,115,109,1,0,0,0])),
                ];
                const denied = [];
                for (const probe of probes) {
                    try { await probe(); denied.push(false); }
                    catch { denied.push(true); }
                }
                for (const specifier of ["node:fs", "node:net", "node:child_process", "lodash"]) {
                    try { await import(specifier); denied.push(false); }
                    catch (error) { denied.push(error.constructor === Error); }
                }
                return denied;
            """)
        self.assertEqual(result, RunResult("SUCCESS", [True] * 14))

    def test_malformed_results_fail(self):
        for source in (
            "return undefined;", "return NaN;", "return 1n;", "return () => 1;",
            "return new Map();", "return { x: undefined };", "return [, 1];",
            "return { get x() { return 1; } };",
            "const x = {}; x.self = x; return x;", "return {",
        ):
            with self.subTest(source=source):
                self.assertEqual(self.run_js(source).status, "FAILED")

    def test_size_and_call_limits(self):
        self.assertEqual(self.run_js(" " * 70000).reason, "Source message too large")
        self.assertEqual(self.run_js("return 'x'.repeat(17000);").reason, "Final result too large")
        for source in (
            "return '💡'.repeat(20000);",
            'return await tools.call("fake.echo", "x".repeat(70000));',
            'return await tools.call("fake.echo", "é".repeat(12000));',
            'for (let i = 0; i < 40; i++) await tools.call("fake.echo", null); return 1;',
            'for (let i = 0; i < 20; i++) await tools.call("fake.echo", "x".repeat(20000)); return 1;',
        ):
            with self.subTest(source=source):
                self.assertEqual(self.run_js(source).status, "FAILED")

    def test_parent_rejects_invalid_child_protocol_and_stderr_flood(self):
        for driver, reason in (
            ('process.stdin.once("data", () => process.stdout.write("[]\\n"));', "Invalid runtime message"),
            ('process.stdin.once("data", () => process.stdout.write("x".repeat(70000)));', "Runtime message too large"),
            ('process.stdin.once("data", () => process.stderr.write("x".repeat(9000)));', "Runtime stderr limit exceeded"),
        ):
            with self.subTest(reason=reason), patch("src.wyrit.runtime.Path.read_text", return_value=driver):
                self.assertEqual(self.run_js("return 1;"), RunResult("FAILED", reason=reason))

    def test_missing_runtime_fails_closed(self):
        with patch("src.wyrit.runtime.shutil.which", return_value=None):
            self.assertEqual(self.run_js("return 1;").reason, "Node.js unavailable")
