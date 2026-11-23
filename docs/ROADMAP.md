# Roadmap

Wyrit lets agents discover capabilities, compose them in JavaScript and run that code in a disposable sandbox while authority stays in the trusted host. Phases 0 to 7 built and verified that foundation (the MVP). Phases 8 to 16 answer the two questions the MVP left open: can Wyrit front real tools without anyone writing code, and does programmatic tool calling actually beat direct tool calls?

0. Evidence and security contract
1. Capability model and discovery
2. Disposable JavaScript runtime
3. Trusted invocation protocol
4. MCP adapter and authorization
5. Run and external-action lifecycle
6. Native sandbox enforcement
7. End-to-end Wyrit MVP
8. Host configuration
   Start Wyrit from a config file that declares downstream MCP servers, with secrets referenced by environment variable only.
9. Live downstream sessions
   Connect, discover, survive a server restart and reload config without restarting Wyrit.
10. Budgets for real work
   Configurable run limits and a local run ledger of every run.
11. Read and write policy
   Writes denied by default, allowed per server or capability.
12. Console, configuration
   A local console to manage servers, capabilities, policy and limits.
13. Console, monitoring and dashboard
   Health, a live run feed and per-capability and per-server views from the run ledger.
14. The measurement harness
   The same tasks run with direct tool calls and through Wyrit, measuring tokens, turns, time and correctness.
15. Linux verification
   Verify the Bubblewrap sandbox on a real Linux host, or document exactly what fails and keep Linux refused.
16. Works with MCPxy (optional pairing)
   Verify Wyrit in front of MCPxy and document both the standalone and paired setups.

Wyrit stands alone; MCPxy is an optional pairing with no coupling in code. Phases 12 and 13 come before the measurement so the benchmark runs can be watched in the console. Nothing after phase 14 is worth building unless the measurement shows a clear win; phases 15 and 16 run only on a go.

The build stops for operator approval before phases 0, 6, 7, 13, 15, 16.
The mvp milestone is phase 7.
The real-work milestone is phase 11.
The console milestone is phase 13.
The measured milestone is phase 14.
The complete milestone is phase 16.

Before phase 13, the operator: Run the console against a real MCP server; add it, expose tools, set a write policy; confirm the TOML matches; sign off the look and flow.
Before phase 15, the operator: Run bench calibrate; approve the projected cost and tasks (swap in your own if you like); run the full benchmark; read the report; go or no-go.
Before phase 16, the operator: On a Linux host with bwrap, run scripts/verify-linux.sh; native suite passes, or the gap is documented and Linux refuses.
After phase 16, the operator: Pair Wyrit with a running MCPxy; read GitHub issues through both; traffic log shows the calls, denials surface, no token in the guest. After a go, decide where production credentials live: a Wyrit secret store or MCPxy.
