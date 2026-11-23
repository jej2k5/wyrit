# Phase 13: Console, monitoring and dashboard

Extend the phase 12 console with what agents are doing, read only from the phase 10 run ledger and the phase 9 health records. Never derive state the host does not record. Same security and design rules as phase 12.

- **Health strip:** servers connected, capabilities exposed, runs in the last hour, and what needs attention (a server down, failures or denials above the recent norm).
- **Live run feed:** runs as they happen, newest first, append-only and pausable: each action in order with outcome glyph and word (SUCCEEDED, FAILED, DENIED, UNKNOWN), duration and size. UNKNOWN is called out with a note that the downstream system may need checking.
- **Per-capability view:** calls, success, failure, denial and unknown rates, and p50/p95 latency over a chosen window (last hour, day, week).
- **Per-server view:** connection history, reconnects and calls lost to drops.
- **Budgets and policy:** which limits runs hit and which writes were denied.
- **Capture, off by default:** an operator may turn on argument and result capture per server with a retention limit; the console shows clearly when capture is on and purges expired entries. Generated code is captured only under the same switch.
Every number links to, or can be traced to, the ledger lines behind it. Polling is cheap: read the ledger incrementally rather than rereading it whole.

Tests (offline): from a synthetic ledger and health history, each view's numbers match a hand computation; percentiles are correct; capture stays off by default and purges on schedule; the feed survives a ledger file rotation. Produce the phase handoff.


**Test secrets (operator note, 2026-11-23):** buildctl refuses any changed file containing a credential-shaped literal (`ghp_…`, `sk-…`, JWT-shaped strings, or `token = "<long literal>"`-style assignments), even a fake one, and fails the phase. Build fake secrets at runtime instead, for example `FAKE_TOKEN = "ghp_" + "x" * 36` or `"s" + "k-" + "test" * 6`, and assert on that variable; never write the assembled value into a source, fixture or snapshot file. Phase 8 attempt 1 failed this way in `tests/test_host_config.py`.

**Phase 12 review fixes (operator, 2026-11-23):** the operator tried the phase 12 console against GitHub's hosted MCP server and found two gaps; fix both in this phase.
1. **Write TOML a person would write.** The console's canonical writer quotes every key (`["servers"."mcpxy_test"]`, `"namespace" = ...`) and emits empty tables (`[servers.x.limits]`, `[limits]`, empty `capabilities`). Emit bare keys wherever TOML allows them, quote only keys that need it, and omit empty tables. Prefer inline tables for header references (`Authorization = { env = "GITHUB_TOKEN", prefix = "Bearer " }`), matching `wyrit.example.toml`. Test: a console-written file for a server with headers, a capability override and default limits equals, as text, the hand-written equivalent, and still loads to the same config.
2. **No raw JSON for header references.** Replace the "Header references (JSON object)" textarea with repeatable rows of three fields (header name, environment variable name, optional prefix) with add and remove buttons, showing whether each variable is set, never its value. Keep the same validation and the rule that a literal credential-like value is refused. Arguments and exposure may stay as they are.
