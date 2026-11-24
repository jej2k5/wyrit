import json
import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import Mock, patch

from anyio.from_thread import start_blocking_portal
from mcp.shared.memory import create_connected_server_and_client_session

from native_support import require_native
from test_mcp_adapter import session_for_fixture
from src.wyrit.budgets import RunBudgets
from src.wyrit.capabilities import Capability, CapabilityRegistry
from src.wyrit.mcp_adapter import MCPAdapter
from src.wyrit.mcp_server import _execute_description, create_server
from src.wyrit.invocation import CallError
from src.wyrit.native_sandbox import SandboxUnavailable


class MCPServerTests(unittest.TestCase):
    def test_execute_description_is_deterministic_and_bounded(self):
        schema = {"type": "object", "properties": {
            "z": {"type": ["string", "null"]}, "a": {"type": "integer"},
        }, "required": ["a"]}
        caps = [Capability(f"demo.group.tool-{i}", "Line one\nline two", schema, {},
                           classification="read") for i in range(60)]
        description = _execute_description(caps)
        self.assertEqual(description, _execute_description(reversed(caps)))
        self.assertIn('demo.group.tool-0 [read] ({"a"*: integer, "z": null|string}) — Line one line two',
                      description)
        self.assertEqual(description.count("[read]"), 60)
        overflow = _execute_description([*caps, Capability("other.tool", "Another", {}, {})])
        self.assertIn("demo.group: 60\nother: 1", overflow)
        self.assertNotIn("demo.group.tool-0", overflow)
        overflow = _execute_description([Capability("demo.long", "é" * 4100, {}, {})])
        self.assertIn("demo: 1", overflow)
        many_namespaces = [Capability(f"ns-{i}-" + "x" * 200 + ".tool", "Tool", {}, {})
                           for i in range(61)]
        self.assertLessEqual(len(_execute_description(many_namespaces).encode()), 8192)

    def test_script_distinguishes_safe_read_only_call_error_fields(self):
        require_native(self)
        canary = "ghp_" + "x" * 36
        downstream = f"{canary} /private/downstream.py stack: downstream"
        caps = [Capability(f"demo.{name}", name, {}, {}, classification=classification)
                for name, classification in (("write", "write"), ("unknown", "read"), ("failed", "read"))]

        def dispatch(capability, args, *, deadline):
            code = "OUTCOME_UNKNOWN" if capability == "demo.unknown" else "TOOL_ERROR"
            error = CallError(code)
            error.args = (downstream,)
            raise error

        self.connect(CapabilityRegistry(caps), dispatch, [cap.id for cap in caps])
        result = self.tool("execute", source="""
            const errors = [];
            for (const id of ["demo.write", "demo.unknown", "demo.failed", "demo.hidden"]) {
                try { await tools.call(id, {}); }
                catch (error) {
                    errors.push({
                        isError: error instanceof Error, code: error.code, capability: error.capability,
                        reason: error.reason ?? null, message: error.message,
                        writable: Object.getOwnPropertyDescriptor(error, "code").writable,
                        configurable: Object.getOwnPropertyDescriptor(error, "code").configurable,
                        stack: error.stack ?? null,
                    });
                }
            }
            return errors;
        """)
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(
            [(row["code"], row["capability"], row["reason"], row["message"]) for row in result["value"]],
            [
                ("DENIED", "demo.write", "WRITE_POLICY", "Capability call denied"),
                ("OUTCOME_UNKNOWN", "demo.unknown", None, "Capability outcome unknown"),
                ("TOOL_ERROR", "demo.failed", None, "Capability tool reported an error"),
                ("DENIED", "demo.hidden", "NOT_AUTHORIZED", "Capability call denied"),
            ],
        )
        for row in result["value"]:
            self.assertTrue(row["isError"])
            self.assertFalse(row["writable"] or row["configurable"])
            self.assertIsNone(row["stack"])
        self.assertNotIn(canary, json.dumps(result))
        self.assertNotIn("/private/downstream.py", json.dumps(result))

    def connect(self, registry, dispatch, allowed):
        portal = self.enterContext(start_blocking_portal())
        directory = self.enterContext(TemporaryDirectory(dir=os.getcwd()))
        server = create_server(
            registry, dispatch, allowed_capability_ids=allowed,
            budgets=RunBudgets({"run_deadline": 2.0}), ledger_path=Path(directory) / "runs.jsonl",
        )
        self.session = self.enterContext(portal.wrap_async_context_manager(
            create_connected_server_and_client_session(server._mcp_server)
        ))
        self.call = portal.call

    def tool(self, name, **arguments):
        result = self.call(self.session.call_tool, name, arguments)
        self.assertFalse(result.isError, result.content)
        structured = getattr(result, "structuredContent", None)
        return structured if structured is not None else json.loads(result.content[0].text)

    def test_search_is_relevant_bounded_and_scoped(self):
        capabilities = [
            Capability(f"demo.tool-{i}", "Format a value", {}, {}, {"private": "canary"})
            for i in range(7)
        ]
        capabilities += [
            Capability("demo.private", "Format a private value", {}, {}),
            Capability("demo.other", "Increment a value", {}, {}),
        ]
        dispatch = Mock()
        self.connect(CapabilityRegistry(capabilities), dispatch,
                     [cap.id for cap in capabilities if cap.id != "demo.private"])
        tools = self.call(self.session.list_tools).tools
        self.assertEqual({tool.name for tool in tools}, {"search", "execute"})
        matches = self.tool("search", query="format")["matches"]
        self.assertEqual(len(matches), 5)
        self.assertEqual(matches[0], {
            "id": "demo.tool-0", "description": "Format a value", "input_schema": {},
            "output_schema": {}, "javascript": 'await tools.call("demo.tool-0", args)',
            "classification": "write",
        })
        self.assertEqual(self.tool("search", query="private")["matches"], [])
        self.assertEqual(self.tool("search", query=""), {"matches": []})
        dispatch.assert_not_called()

    def test_unavailable_sandbox_returns_a_run_failure_without_launch_or_dispatch(self):
        capability = Capability("demo.write", "Write a value", {}, {})
        dispatch = Mock()
        self.connect(CapabilityRegistry([capability]), dispatch, [capability.id])
        with (
            patch("src.wyrit.runtime.shutil", wraps=shutil) as paths,
            patch("src.wyrit.runtime.native_command",
                  side_effect=SandboxUnavailable("No supported native sandbox available")),
            patch("src.wyrit.runtime.subprocess.Popen") as launch,
        ):
            paths.which.return_value = "/trusted/node"
            first = self.tool("execute", source='return await tools.call("demo.write", {});')
            second = self.tool("execute", source="return 1;")
        launch.assert_not_called()
        dispatch.assert_not_called()
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(first.pop("limits")["global"]["run_deadline"], {"value": 2.0, "source": "global"})
        self.assertIsNone(first.pop("limit_hit"))
        self.assertIsNone(first.pop("ledger_error"))
        self.assertEqual(first, {
            "run_id": first["run_id"], "status": "FAILED", "value": None,
            "reason": "No supported native sandbox available",
            "actions": [], "unknown_calls": [], "cancellation_requested": False,
            "cancellation_confirmed": False,
        })

    def test_native_execution_through_a_real_downstream_mcp_session(self):
        require_native(self)
        self.enterContext(patch.dict(os.environ, {"WYRIT_MCP_TEST_CREDENTIAL": "host-test-canary"}))
        portal = self.enterContext(start_blocking_portal())
        downstream = self.enterContext(portal.wrap_async_context_manager(session_for_fixture()))
        adapter = MCPAdapter("demo", downstream, portal)
        self.connect(
            CapabilityRegistry(adapter.capabilities), adapter.dispatch,
            ["demo.increment", "demo.format", "demo.sample"],
        )
        composition = """
            const row = await tools.call("demo.increment", {value: 41});
            return await tools.call("demo.format", row);
        """
        success = self.tool("execute", source=composition)
        self.assertEqual(success["status"], "SUCCESS")
        self.assertEqual(success["value"], {"content": [{"type": "text", "text": "value=42"}]})
        self.assertEqual(success["actions"], [
            {"call_id": 1, "capability": "demo.increment", "status": "SUCCEEDED", "code": None},
            {"call_id": 2, "capability": "demo.format", "status": "SUCCEEDED", "code": None},
        ])

        failure = self.tool("execute", source='await tools.call("demo.sample", {case: "failure"});')
        self.assertEqual(failure["status"], "FAILED")
        self.assertEqual(failure["actions"], [
            {"call_id": 1, "capability": "demo.sample", "status": "FAILED", "code": "TOOL_ERROR"},
        ])
        self.assertNotIn("host-test-canary", json.dumps(failure))

        denial = self.tool("execute", source="""
            try { await tools.call("demo.stats", {}); }
            catch (error) { return error.message; }
        """)
        self.assertEqual(denial["value"], "Capability call denied")
        self.assertEqual(denial["actions"], [])

        malformed = self.tool("execute", source="const = ;")
        self.assertEqual(malformed["status"], "FAILED")
        self.assertEqual(malformed["reason"], "JavaScript execution or serialization failed")
        self.assertEqual(malformed["actions"], [])

        oversized = self.tool("execute", source="""
            await tools.call("demo.increment", {value: 1});
            return "x".repeat(17000);
        """)
        self.assertEqual(oversized["status"], "FAILED")
        self.assertEqual(oversized["reason"], "Final result too large")
        self.assertEqual(oversized["actions"], [success["actions"][0]])

        timeout = self.tool("execute", source="""
            await tools.call("demo.sample", {case: "slow"});
            await tools.call("demo.increment", {value: 0});
        """)
        self.assertEqual(timeout["status"], "TIMED_OUT")
        self.assertEqual(timeout["unknown_calls"], [1])
        self.assertEqual(timeout["actions"], [
            {"call_id": 1, "capability": "demo.sample", "status": "UNKNOWN",
             "code": "OUTCOME_UNKNOWN"},
        ])
        self.assertEqual(adapter.dispatch("demo.stats", {}, deadline=time.monotonic() + 2), {
            "increment": 2, "format": 1, "sample": 2, "stats": 1,
        })
