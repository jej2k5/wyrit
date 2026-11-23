# Phase 11: Read and write policy

Classify every capability read or write. Use the downstream tool's MCP annotations when present (`readOnlyHint: true` is read; `destructiveHint` or `readOnlyHint: false` is write); let config override per capability; anything unknown is write.

Writes are denied by default. Config allows writes per server or per capability. A denied call never reaches the downstream server, is reported to the script as a denial it can handle, and is recorded DENIED in the ledger. Search results show each capability's class so an agent knows before calling. This mirrors MCPxy's `deny_writes`, so operators of both learn one rule; Wyrit has no dependency on MCPxy.

Tests: with defaults, a script that reads then writes completes the read and gets a recorded denial; allowing one capability allows exactly that one; annotations and overrides resolve as described; a denied call is never sent. Produce the phase handoff.
