# Phase 7: End-to-end Wyrit MVP

Expose the minimal agent-facing MCP surface: `search` and `execute`. Search returns relevant capability metadata and JavaScript usage without dumping the full inventory. Execute runs JavaScript through the governed runtime and returns structured run/action outcomes.

Demonstrate a real composition across at least two capabilities through the trusted host. Exercise success, tool failure, authorization denial, timeout after dispatch, malformed generated code, output limits and unavailable sandbox enforcement.

Do not add durable workflow orchestration, transparent replay, rollback, generalized approvals, multiple runtimes or distributed control. Produce a release-oriented phase handoff and operator quickstart.
