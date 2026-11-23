from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from bench.__main__ import ROOT, main
from bench.agent import run_agent
from bench.clients import github_snapshot
from bench.tasks import check, github_fixture, load_tasks
from native_support import require_native
from src.wyrit.native_sandbox import SandboxUnavailable


SETTINGS = {"model": "test-model", "max_turns": 5, "max_tokens": 100}
TASKS = {task["id"]: task for task in load_tasks(ROOT / "tasks")}


def calls(*tools):
    return {
        "content": [{"type": "tool_use", "id": f"call_{index}", "name": name, "input": arguments}
                    for index, (name, arguments) in enumerate(tools)],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }


def answer(value, *, input_tokens=19):
    return {"content": [{"type": "text", "text": json.dumps(value)}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": input_tokens, "output_tokens": 3}}


class BenchTests(unittest.TestCase):
    def test_direct_dependent_chain_and_usage(self):
        task = TASKS["dependent-chain"]

        def model(**request):
            messages = request["messages"]
            self.assertEqual(request["model"], "test-model")
            self.assertEqual({tool["name"] for tool in request["tools"]},
                             {"list-projects", "project-detail"})
            if len(messages) == 1:
                return calls(("list-projects", {}))
            results = messages[-1]["content"]
            if len(messages) == 3:
                listed = json.loads(results[0]["content"])["projects"]
                return calls(*[("project-detail", {"detail_key": row["detail_key"]})
                               for row in listed if (row["region"], row["status"]) == ("north", "active")])
            self.assertEqual([block["tool_use_id"] for block in results], ["call_0", "call_1"])
            return answer({"project_count": len(results),
                           "billable_minutes": sum(json.loads(block["content"])["billable_minutes"]
                                                   for block in results)})

        row = run_agent(task, "direct", model, SETTINGS)
        self.assertTrue(row["correct"])
        self.assertEqual([row[key] for key in ("input_tokens", "output_tokens", "model_turns",
                                              "tool_calls", "downstream_calls")], [41, 17, 3, 3, 3])
        self.assertGreater(row["wall_seconds"], 0)
        self.assertFalse(check(task, task["expected"], [], []))

    def test_direct_error_and_denial_are_observed(self):
        task = TASKS["failure-and-denial"]

        def model(**request):
            if len(request["messages"]) == 1:
                return calls(("read-status", {}), ("write-maintenance", {}))
            read, write = request["messages"][-1]["content"]
            self.assertTrue(read["is_error"] and write["is_error"])
            self.assertEqual(json.loads(read["content"])["code"], "TOOL_ERROR")
            self.assertEqual(json.loads(write["content"])["code"], "DENIED")
            return answer({"read": "failed", "write": "denied"})

        row = run_agent(task, "direct", model, SETTINGS)
        self.assertTrue(row["correct"])
        self.assertEqual(row["downstream_calls"], 1)

    def test_wyrit_search_reaches_the_same_fixture_with_no_network(self):
        task = TASKS["failure-and-denial"]

        def model(**request):
            self.assertEqual({tool["name"] for tool in request["tools"]}, {"search", "execute"})
            if len(request["messages"]) == 1:
                return calls(("search", {"query": "maintenance"}))
            result = json.loads(request["messages"][-1]["content"][0]["content"])
            self.assertEqual(result["matches"][0]["id"], "bench.write-maintenance")
            return answer(task["expected"])

        row = run_agent(task, "wyrit", model, SETTINGS)
        self.assertFalse(row["correct"])
        self.assertEqual(row["tool_calls"], 1)
        self.assertEqual(row["downstream_calls"], 0)

    def test_wyrit_execute_with_real_sandbox_and_fixture(self):
        require_native(self)
        task = TASKS["failure-and-denial"]
        source = """
        const outcomes = {};
        for (const [key, id] of [["read", "bench.read-status"], ["write", "bench.write-maintenance"]]) {
            try { await tools.call(id, {}); outcomes[key] = "succeeded"; }
            catch (e) {
                outcomes[key] = e.message === "Capability call denied" ? "denied" : "failed";
            }
        }
        return outcomes;
        """

        def model(**request):
            if len(request["messages"]) == 1:
                return calls(("execute", {"source": source}))
            result = json.loads(request["messages"][-1]["content"][0]["content"])
            self.assertEqual(result["status"], "SUCCESS")
            return answer(result["value"])

        row = run_agent(task, "wyrit", model, SETTINGS)
        self.assertTrue(row["correct"])
        self.assertEqual(row["execution_statuses"], ["SUCCESS"])
        self.assertEqual(row["downstream_calls"], 1)

    def test_wyrit_execute_fails_closed_without_native_sandbox(self):
        task = TASKS["failure-and-denial"]

        def model(**request):
            if len(request["messages"]) == 1:
                return calls(("execute", {"source": 'return await tools.call("bench.read-status", {});'}))
            result = json.loads(request["messages"][-1]["content"][0]["content"])
            self.assertEqual(result["status"], "FAILED")
            self.assertEqual(result["reason"], "Native sandbox unavailable")
            return answer(task["expected"])

        with (
            patch("src.wyrit.runtime.shutil.which", return_value="/trusted/node"),
            patch("src.wyrit.runtime.native_command",
                  side_effect=SandboxUnavailable("Native sandbox unavailable")),
            patch("src.wyrit.runtime.subprocess.Popen") as launch,
        ):
            row = run_agent(task, "wyrit", model, SETTINGS)
        launch.assert_not_called()
        self.assertFalse(row["correct"])
        self.assertEqual(row["execution_statuses"], ["FAILED"])
        self.assertEqual(row["downstream_calls"], 0)

    def test_calibration_gate_and_reports(self):
        model = Mock(side_effect=lambda **req: answer(
            [], input_tokens=30 if "execute" in {tool["name"] for tool in req["tools"]} else 10,
        ))
        output = Path(self.enterContext(TemporaryDirectory(dir=os.getcwd())))
        options = ["--output", str(output), "--repetitions", "2"]
        captured = self.enterContext(redirect_stdout(StringIO()))
        self.enterContext(redirect_stderr(StringIO()))
        network = self.enterContext(patch("bench.clients.HTTPSConnection"))
        snapshot = Mock()
        inject = {"model_client": model, "env": {}, "snapshot_loader": snapshot}
        with self.assertRaises(SystemExit) as blocked:
            main(["run", *options], **inject)
        self.assertEqual(blocked.exception.code, 2)
        self.assertEqual(main(["run", *options, "--calibration-id", "0" * 64], **inject), 1)
        model.assert_not_called()
        self.assertEqual(main(["calibrate", *options], **inject), 0)
        self.assertEqual(model.call_count, 6)
        snapshot.assert_not_called()
        network.assert_not_called()
        calibration, = (output / "calibrations").glob("*.json")
        record = json.loads(calibration.read_text())
        self.assertAlmostEqual(record["projected_cost_usd"], 0.00126)
        self.assertIn("Projected cost for 12 runs", captured.getvalue())
        self.assertIn("GITHUB_TOKEN is unset", captured.getvalue())
        self.assertEqual(main(["run", "--output", str(output), "--repetitions", "4",
                               "--calibration-id", calibration.stem], **inject), 1)
        self.assertEqual(main(["run", *options, "--calibration-id", calibration.stem],
                              **{**inject, "env": {"GITHUB_TOKEN": "test-token"}}), 1)
        snapshot.assert_not_called()
        self.assertEqual(model.call_count, 6)
        self.assertEqual(main(["run", *options, "--calibration-id", calibration.stem], **inject), 0)
        report_path, = output.glob("run-*/report.json")
        report = json.loads(report_path.read_text())
        self.assertEqual(len(report["runs"]), 12)
        self.assertEqual(len(report["per_task"]), 3)
        overall = report["overall"]
        self.assertIn("input_tokens", overall["wyrit_losses"])
        self.assertEqual(overall["wyrit_minus_direct"]["input_tokens"], 20)
        self.assertEqual(overall["approaches"]["wyrit"]["metrics"]["input_tokens"]["variance"], 0)
        markdown = report_path.with_suffix(".md").read_text()
        self.assertIn("## failure-and-denial", markdown)
        self.assertIn("Wyrit trails on input_tokens", markdown)

    def test_github_fixture_gets_fixed_read_only_data_and_checks_each_issue(self):
        with patch("bench.clients.request_json", side_effect=[
            [{"number": 1, "pull_request": {}}, {"number": 2}, {"number": 5}],
            [{"body": "TEST!"}, {"body": "nothing here"}],
            [{"body": "test test"}],
        ]) as request:
            snapshot = github_snapshot("test-token")
        self.assertEqual(list(snapshot), [2, 5])
        for call in request.call_args_list:
            self.assertEqual(call.args[:2], ("api.github.com", "GET"))
            self.assertTrue(call.args[2].startswith("/repos/jej2k5/mcpxy-test/issues"))
        task = github_fixture(TASKS["github-comments"], snapshot)
        required = task["required_requests"]
        self.assertTrue(check(task, {"count": 2}, required, []))
        self.assertFalse(check(task, {"count": 2}, required[:-1], []))
        self.assertFalse(check(task, {"count": 3}, required, []))
