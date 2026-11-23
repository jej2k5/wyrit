# Phase 16: Works with MCPxy (optional pairing)

Wyrit stands alone; MCPxy is one way to run it, not a dependency. To Wyrit, MCPxy is just another downstream MCP server, so add no MCPxy-specific code. Verify and document the pairing.

Add an example config section for an MCPxy-backed server (HTTP transport to MCPxy's `/mcp/<upstream>` endpoint, the MCPxy proxy token referenced by environment variable). Add a test with a fake MCP-over-HTTP server that behaves like MCPxy for the cases that matter: bearer authentication, a policy denial returned as JSON-RPC `-32003 policy_blocked:write_denied` surfacing in Wyrit as DENIED, and tool calls attributed in the downstream log while no credential ever reaches the Wyrit guest.

Write `docs/QUICKSTART.md`: run Wyrit standalone (config file, console, a first server), then the MCPxy pairing as an option. Produce the phase handoff with the operator check against a real MCPxy instance.
