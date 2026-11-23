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
