from threading import BoundedSemaphore, Event
import unittest
from unittest.mock import patch
from native_support import require_native

from src.wyrit.capabilities import RunAuthorization
from src.wyrit.invocation import ActionRecord, CallError
from src.wyrit.runtime import run_javascript


class LifecycleTests(unittest.TestCase):
    capability = "fake.write"

    def setUp(self):
        require_native(self)

    def run_js(self, source, dispatch, **options):
        return run_javascript(
            source, run_id="run", authorization=RunAuthorization("run", {self.capability}),
            dispatch=dispatch, **options,
        )

    def test_deadline_while_dispatched_action_cannot_complete(self):
        release = Event()
        finished = Event()
        calls = []

        def dispatch(capability, args, *, deadline):
            calls.append((capability, args))
            release.wait()
            finished.set()
            return True

        try:
            result = self.run_js('await tools.call("fake.write", 42);', dispatch, timeout=1)
            self.assertEqual(result.status, "TIMED_OUT")
            self.assertEqual(calls, [(self.capability, 42)])
            self.assertFalse(finished.is_set())
            self.assertEqual(result.unknown_calls, (1,))
            self.assertFalse(result.cancellation_requested)
            self.assertFalse(result.cancellation_confirmed)
        finally:
            release.set()
        self.assertTrue(finished.wait(1))
        self.assertEqual(result.actions, (
            ActionRecord(1, self.capability, "UNKNOWN", "OUTCOME_UNKNOWN"),
        ))

    def test_action_outcomes_survive_later_javascript_failure(self):
        def dispatch(capability, args, *, deadline):
            if args == "error":
                raise CallError("TOOL_ERROR")
            return True

        result = self.run_js("""
            await tools.call("fake.write", "ok");
            try { await tools.call("fake.write", "error"); } catch {}
            throw new Error("later failure");
        """, dispatch)
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.actions, (
            ActionRecord(1, self.capability, "SUCCEEDED"),
            ActionRecord(2, self.capability, "FAILED", "TOOL_ERROR"),
        ))

    def test_dispatch_capacity_stays_reserved_after_run_cancellation(self):
        cancel = Event()
        release = Event()
        slots = BoundedSemaphore(1)
        calls = []

        def dispatch(capability, args, *, deadline):
            calls.append(args)
            cancel.set()
            release.wait()
            return True

        source = """
            try { return await tools.call("fake.write", 42); }
            catch (error) { return error.message; }
        """
        try:
            with patch("src.wyrit.runtime._DISPATCH_SLOTS", slots):
                first = self.run_js(source, dispatch, cancel_event=cancel)
                second = self.run_js(source, dispatch)
            self.assertEqual(first.status, "CANCELLED")
            self.assertEqual(first.unknown_calls, (1,))
            self.assertEqual(second.status, "SUCCESS")
            self.assertEqual(second.value, "Capability dispatch unavailable")
            self.assertEqual(second.actions, ())
            self.assertEqual(calls, [42])
        finally:
            release.set()

    def test_cancellation_preserves_completed_and_pending_actions(self):
        cancel = Event()
        release = Event()
        calls = []

        def dispatch(capability, args, *, deadline):
            calls.append(args)
            if args == 2:
                cancel.set()
                release.wait()
            return True

        try:
            result = self.run_js("""
                await tools.call("fake.write", 1);
                await tools.call("fake.write", 2);
                await tools.call("fake.write", 3);
            """, dispatch, cancel_event=cancel)
        finally:
            release.set()
        self.assertEqual(calls, [1, 2])
        self.assertEqual(result.status, "CANCELLED")
        self.assertTrue(result.cancellation_requested)
        self.assertTrue(result.cancellation_confirmed)
        self.assertEqual(result.actions, (
            ActionRecord(1, self.capability, "SUCCEEDED"),
            ActionRecord(2, self.capability, "UNKNOWN", "OUTCOME_UNKNOWN"),
        ))
