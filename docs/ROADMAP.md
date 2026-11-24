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
15. Lean discovery and clear outcomes
   List capabilities in the execute description so small tasks skip the search turn; give scripts error codes they can branch on.
16. Reviewed recipes
   Parameterized scripts an operator approves once and agents run by name; stale or edited recipes stop running until re-approved.
17. Measure again
   Rerun the benchmark with a larger fan-out and a third approach, Wyrit with recipes.
18. Embeddable host
   A Python API so a host application brings its own capabilities, dispatch and per-run identity (principal, tenant), and Wyrit supplies the sandbox, budgets, outcome codes, recipes and ledger records. The MCP server becomes one client of it.
19. Linux verification
   Verify the Bubblewrap sandbox on a real Linux host and inside a Docker container, or document exactly what fails and keep Linux refused.
20. Works with MCPxy (optional pairing)
   Verify Wyrit in front of MCPxy and document both the standalone and paired setups.

Wyrit stands alone; MCPxy is an optional pairing with no coupling in code. Phases 12 and 13 come before the measurement so the benchmark runs can be watched in the console. The phase 14 benchmark (2026-11-23) showed Wyrit winning on heavy work (a 30-call fan-out at half the cost of direct tool calls, and 4.3x cheaper and 3.3x faster with a finished script) and losing on small tasks (34 to 65% dearer, about twice as slow). The operator chose to re-plan around that: phases 15 to 17 act on the finding and measure again; phases 18 to 20 run only if phase 17 holds up. Phase 18 makes Wyrit embeddable in products that own identity and audit (first intended user: the HONA data agent, which runs every tool on behalf of the signed-in user); that makes phase 19's container verification a gate rather than a nice-to-have.

The build stops for operator approval before phases 0, 6, 7, 13, 18, 20.
The mvp milestone is phase 7.
The real-work milestone is phase 11.
The console milestone is phase 13.
The measured milestone is phase 14.
The lean milestone is phase 15.
The recipes milestone is phase 16.
The proven milestone is phase 17.
The embeddable milestone is phase 18.
The complete milestone is phase 20.

Before phase 13, the operator: Run the console against a real MCP server; add it, expose tools, set a write policy; confirm the TOML matches; sign off the look and flow.
Before phase 18, the operator: Run bench calibrate and approve the projected usage; run the full benchmark (three approaches); go if Wyrit is within about 10% of direct on small tasks and recipes keep at least a 3x cost win on the fan-outs, otherwise stop and rethink.
Before phase 20, the operator: On a Linux host with bwrap, run scripts/verify-linux.sh and scripts/verify-linux-container.sh; native suite passes, or the gap is documented and Linux refuses.
After phase 20, the operator: Pair Wyrit with a running MCPxy; read GitHub issues through both; traffic log shows the calls, denials surface, no token in the guest. After a go, decide where production credentials live: a Wyrit secret store or MCPxy.
