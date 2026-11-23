# Phase 16: Works with MCPxy (optional pairing)

Wyrit stands alone; MCPxy is one way to run it, not a dependency. To Wyrit, MCPxy is just another downstream MCP server, so add no MCPxy-specific code. Verify and document the pairing.

Add an example config section for an MCPxy-backed server (HTTP transport to MCPxy's `/mcp/<upstream>` endpoint, the MCPxy proxy token referenced by environment variable). Add a test with a fake MCP-over-HTTP server that behaves like MCPxy for the cases that matter: bearer authentication, a policy denial returned as JSON-RPC `-32003 policy_blocked:write_denied` surfacing in Wyrit as DENIED, and tool calls attributed in the downstream log while no credential ever reaches the Wyrit guest.

Write `docs/QUICKSTART.md`: run Wyrit standalone (config file, console, a first server), then the MCPxy pairing as an option. Produce the phase handoff with the operator check against a real MCPxy instance.


**Test secrets (operator note, 2026-11-23):** buildctl refuses any changed file containing a credential-shaped literal (`ghp_…`, `sk-…`, JWT-shaped strings, or `token = "<long literal>"`-style assignments), even a fake one, and fails the phase. Build fake secrets at runtime instead, for example `FAKE_TOKEN = "ghp_" + "x" * 36` or `"s" + "k-" + "test" * 6`, and assert on that variable; never write the assembled value into a source, fixture or snapshot file. Phase 8 attempt 1 failed this way in `tests/test_host_config.py`.
