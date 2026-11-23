# Phase 10: Budgets for real work

The fixed two-second run deadline suits the demo, not network tools. Make limits configurable in `wyrit.toml`, globally and per server, with safe defaults: run deadline, per-capability call timeout, maximum calls per run, maximum output size. Report the effective value and where it came from (default, global, server).

A run that exceeds a limit stops with a structured reason naming the limit; calls already sent keep their recorded outcomes. Per-capability timeouts end only that call (UNKNOWN if it was sent) and let the script handle the error.

Every run appends one JSON line to a local run ledger (path configurable, default under the user's state directory, file mode 0600): run ID, start and end time, outcome, limit hit if any, and for each action the capability, outcome (SUCCEEDED, FAILED, DENIED, UNKNOWN), duration and result size. Arguments, results and the generated code are not stored. Add a small reader (`read_ledger`) that later phases reuse. Tests cover each limit, per-server overrides, the ledger contents and that no argument or result value is ever written. Produce the phase handoff.
