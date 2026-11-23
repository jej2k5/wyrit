from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from bench.__main__ import ROOT, main
from bench.agent import JSON_INSTRUCTION, SYSTEM
from bench.claude_code import COST_LABEL, run_claude
from bench.tasks import load_tasks
from native_support import require_native


FAKE_CLAUDE = r"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

args = sys.argv[1:]
path = Path(args[args.index("--mcp-config") + 1])
servers = json.loads(path.read_text())["mcpServers"]
capture = Path(os.environ["FAKE_CAPTURE"])
with capture.open("a") as stream:
    stream.write(json.dumps({
        "args": args, "servers": servers, "cwd": str(Path.cwd()), "empty": not list(Path.cwd().iterdir()),
        "oauth_preserved": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") == "test-oauth",
        "api_key_present": "ANTHROPIC_API_KEY" in os.environ,
        "github_token_present": "GITHUB_TOKEN" in os.environ,
    }) + "\n")

mode = os.environ.get("FAKE_MODE")
server, = servers
tools = ["read-status", "write-maintenance"] if server == "bench" else ["search", "execute"]
blocks = [{"type": "tool_use", "id": f"tool_{name}", "name": f"mcp__{server}__{name}"}
          for name in tools]
print(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "text", "text": "Using tools"}, *blocks,
]}}), flush=True)
print(json.dumps({"type": "user", "message": {"content": [
    {"type": "tool_result", "tool_use_id": "tool_read-status", "content": "done"},
]}}), flush=True)

if mode == "timeout":
    child_code = (
        "import fcntl, pathlib, sys, time; "
        "lock = pathlib.Path(sys.argv[1]).open('w'); fcntl.flock(lock, fcntl.LOCK_EX); "
        "lock.write('locked'); lock.flush(); time.sleep(60)"
    )
    subprocess.Popen([sys.executable, "-c", child_code, str(capture.with_suffix(".lock"))])
    time.sleep(60)
elif mode == "exit":
    print("login required", file=sys.stderr)
    sys.exit(7)
elif mode in {"recorded", "malformed-ledger"}:
    ledger = Path(os.environ["FAKE_LEDGER"]).read_text()
    if mode == "malformed-ledger":
        ledger = "invalid json\n" + ledger + "{}\n"
    (path.parent / "runs.jsonl").write_text(ledger)
    (path.parent / "requests.jsonl").write_text(json.dumps({
        "request": {"tool": "read-status", "arguments": {}},
    }) + "\n")
elif mode in {"mcp", "mcp-native"}:
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def exercise():
        async with stdio_client(StdioServerParameters(**servers[server])) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                discovered = {tool.name for tool in (await session.list_tools()).tools}
                assert discovered == set(tools), discovered
                if server == "bench":
                    read = await session.call_tool("read-status", {})
                    write = await session.call_tool("write-maintenance", {})
                    assert read.isError and "Fixture tool failed" in read.content[0].text
                    assert write.isError and "DENIED" in write.content[0].text
                else:
                    response = await session.call_tool("search", {"query": "maintenance"})
                    result = getattr(response, "structuredContent", None) or json.loads(response.content[0].text)
                    assert result["matches"][0]["id"] == "bench.write-maintenance", result
                    if mode == "mcp-native":
                        response = await session.call_tool("execute", {"source": '''
                            for (const name of ["read-status", "write-maintenance"]) {
                                try { await tools.call("bench." + name, {}); } catch (_) {}
                            }
                            return null;
                        '''})
                        result = getattr(response, "structuredContent", None) or json.loads(response.content[0].text)
                        assert result["status"] == "SUCCESS", result

    anyio.run(exercise)

print(json.dumps({
    "type": "result", "num_turns": 3, "duration_ms": 2040, "total_cost_usd": 0.0125,
    "usage": {"input_tokens": 11, "output_tokens": 7,
              "cache_creation_input_tokens": 3, "cache_read_input_tokens": 5},
    "is_error": mode == "limit",
    "result": "Subscription usage limit reached" if mode == "limit"
              else json.dumps({"read": "failed", "write": "denied"}),
}))
"""


@unittest.skipUnless(os.name == "posix", "Claude Code benchmark uses POSIX process groups")
class ClaudeCodeTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        executable = self.directory / "claude"
        executable.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE}", encoding="utf-8")
        executable.chmod(0o700)
        self.capture = self.directory / "calls.jsonl"
        self.env = {**os.environ, "PATH": str(self.directory) + os.pathsep + os.defpath,
                    "FAKE_CAPTURE": str(self.capture), "ANTHROPIC_API_KEY": "test-key",
                    "GITHUB_TOKEN": "test-token", "CLAUDE_CODE_OAUTH_TOKEN": "test-oauth"}
        self.task = next(task for task in load_tasks(ROOT / "tasks")
                         if task["id"] == "failure-and-denial")
        self.settings = {"model": "test-model", "timeout_seconds": 15}

    def calls(self):
        return [json.loads(line) for line in self.capture.read_text().splitlines()]

    def test_exact_command_metrics_and_real_stdio_tools_for_both_approaches(self):
        for approach in ("direct", "wyrit"):
            with self.subTest(approach=approach):
                row = run_claude(self.task, approach, self.settings, {**self.env, "FAKE_MODE": "mcp"})
                self.assertIsNone(row["error"])
                record = self.calls()[-1]
                args = record["args"]
                self.assertEqual(args, [
                    "-p", self.task["prompt"] + JSON_INSTRUCTION, "--output-format", "stream-json",
                    "--verbose", "--model", "test-model", "--tools", "", "--strict-mcp-config",
                    "--mcp-config", str(Path(record["cwd"]).parent / "mcp.json"),
                    "--allowedTools", "mcp__*", "--permission-mode", "dontAsk",
                    "--no-session-persistence", "--setting-sources", "", "--disable-slash-commands",
                    "--append-system-prompt", SYSTEM[approach],
                ])
                self.assertTrue(record["empty"])
                self.assertFalse(Path(record["cwd"]).exists())
                self.assertFalse(record["api_key_present"])
                self.assertTrue(record["oauth_preserved"])
                self.assertFalse(record["github_token_present"])
                server = "bench" if approach == "direct" else "wyrit"
                self.assertEqual(set(record["servers"]), {server})
                command = record["servers"][server]
                self.assertEqual(command["command"], sys.executable)
                if approach == "wyrit":
                    self.assertEqual(command["args"][:3], ["-m", "wyrit.mcp_server", "--config"])
                else:
                    self.assertEqual(Path(command["args"][0]).name, "fixture_stdio.py")
                self.assertEqual([row[key] for key in (
                    "input_tokens", "output_tokens", "cache_creation_input_tokens",
                    "cache_read_input_tokens", "total_tokens", "model_turns", "tool_calls",
                    "wall_seconds", "cost_usd",
                )], [11, 7, 3, 5, 26, 3, 2, 2.04, 0.0125])
                # The Wyrit fake only searches in this test. Its guessed answer fails.
                self.assertEqual(row["correct"], approach == "direct")
                self.assertEqual(row["downstream_calls"], 1 if approach == "direct" else 0)

    def test_wyrit_recorded_ledger_scores_and_reports_malformed_lines(self):
        fixture = Path(__file__).resolve().with_name("fixtures") / "bench-runs.jsonl"
        for mode in ("recorded", "malformed-ledger"):
            with self.subTest(mode=mode):
                env = {**self.env, "FAKE_MODE": mode, "FAKE_LEDGER": str(fixture)}
                row = run_claude(self.task, "wyrit", self.settings, env)
                self.assertEqual(row["execution_statuses"], ["SUCCESS"])
                self.assertEqual(row["tool_outcomes"], [
                    *self.task["required_outcomes"], {"tool": None, "outcome": "DENIED"},
                    {"tool": None, "outcome": "DENIED"},
                ])
                self.assertEqual(row["total_tokens"], 26)
                self.assertEqual(row["downstream_calls"], 1)
                if mode == "recorded":
                    self.assertIsNone(row["error"])
                    self.assertTrue(row["correct"])
                else:
                    self.assertIn("Malformed runs.jsonl at line 1 (2 invalid records)", row["error"])
                    self.assertTrue(row["is_error"])
                    self.assertFalse(row["correct"])

    def test_wyrit_checker_uses_real_action_outcomes(self):
        require_native(self)
        env = {**self.env, "PATH": str(self.directory) + os.pathsep + os.environ["PATH"],
               "FAKE_MODE": "mcp-native"}
        row = run_claude(self.task, "wyrit", self.settings, env)
        self.assertIsNone(row["error"])
        self.assertEqual(row["execution_statuses"], ["SUCCESS"])
        self.assertEqual(row["downstream_calls"], 1)
        self.assertTrue(row["correct"])

    def test_timeout_kills_descendant_and_keeps_observed_tool_calls(self):
        import fcntl

        row = run_claude(self.task, "direct", {**self.settings, "timeout_seconds": 1},
                         {**self.env, "FAKE_MODE": "timeout"})
        self.assertIn("timed out after 1 seconds", row["error"])
        self.assertTrue(row["is_error"])
        self.assertFalse(row["correct"])
        self.assertEqual(row["tool_calls"], 2)
        with self.capture.with_suffix(".lock").open() as lock:
            self.assertEqual(lock.read(), "locked")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_no_retry_for_subscription_limit_or_nonzero_exit(self):
        for mode, reason in (("limit", "Subscription usage limit reached"),
                             ("exit", "exited with status 7 (login required)")):
            with self.subTest(mode=mode):
                row = run_claude(self.task, "direct", self.settings, {**self.env, "FAKE_MODE": mode})
                self.assertIn(reason, row["error"])
                self.assertTrue(row["is_error"])
                self.assertFalse(row["correct"])
                self.assertEqual(row["total_tokens"], 26 if mode == "limit" else 0)
        self.assertEqual(len(self.calls()), 2)

    def test_default_projection_and_authorization_include_driver_and_timeout(self):
        suite = self.directory / "tasks"
        suite.mkdir()
        (suite / "task.json").write_text(json.dumps(self.task))
        output = self.directory / "results"
        options = ["--output", str(output), "--tasks", str(suite), "--repetitions", "2"]
        stdout = self.enterContext(redirect_stdout(StringIO()))
        self.enterContext(redirect_stderr(StringIO()))
        network = self.enterContext(patch("bench.clients.HTTPSConnection"))

        with self.assertRaises(SystemExit) as missing:
            main(["run", *options], env=self.env)
        self.assertEqual(missing.exception.code, 2)
        self.assertFalse(self.capture.exists())
        env = {**self.env, "FAKE_MODE": "limit"}
        self.assertEqual(main(["calibrate", *options], env=env), 0)
        projection_path, = (output / "calibrations").glob("*.json")
        projection = json.loads(projection_path.read_text())
        self.assertEqual(projection["plan"]["driver"], "claude-code")
        self.assertEqual(projection["plan"]["settings"]["timeout_seconds"], 600)
        self.assertEqual(len(projection["runs"]), 2)
        self.assertAlmostEqual(projection["projected_cost_usd"], 0.05)
        self.assertEqual(projection["projected_total_tokens"], 104)
        self.assertAlmostEqual(projection["projected_wall_seconds"], 8.16)
        self.assertIn(COST_LABEL, stdout.getvalue())
        self.assertIn("104 total tokens", stdout.getvalue())

        run = ["run", *options, "--calibration-id", projection_path.stem]
        self.assertEqual(main([*run, "--timeout", "1"], env=env), 1)
        self.assertEqual(main([*run, "--driver", "api"], env=env), 1)
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(main(run, env=env), 0)
        self.assertEqual(len(self.calls()), 6)
        report_path, = output.glob("run-*/report.json")
        report = json.loads(report_path.read_text())
        self.assertEqual(report["driver"], "claude-code")
        self.assertEqual(report["cost_label"], COST_LABEL)
        self.assertEqual(len(report["runs"]), 4)
        self.assertIn("Subscription usage limit reached", report["runs"][0]["error"])
        markdown = report_path.with_suffix(".md").read_text()
        self.assertIn("fixed instructions", markdown)
        self.assertIn("cache_read_input_tokens", markdown)
        self.assertIn("Run errors", markdown)
        network.assert_not_called()
