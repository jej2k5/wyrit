# Phase 5: Run and external-action lifecycle

Implement separate run and action state machines exactly as governed by EXECUTION_SEMANTICS.md. Record dispatch before relying on downstream completion. Preserve completed/dispatched action records after later JavaScript failure, timeout or cancellation.

Build deterministic tests where an external action is dispatched and the JavaScript run then times out. The result must preserve the run timeout while reporting the action as UNKNOWN when completion cannot be established.

Never equate sandbox termination with downstream cancellation. Never automatically retry a non-idempotent action without an explicit safety mechanism. Produce the phase handoff.
