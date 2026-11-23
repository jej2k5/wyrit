# Phase 9: Live downstream sessions

The trusted host owns each configured server's session: connect at startup, discover tools through `MCPAdapter`, and keep a per-server health record (connected, connecting, down with reason, last change). One server failing to connect makes only its capabilities unavailable, with the reason in search results; Wyrit and the other servers keep working.

Reconnect with bounded backoff after a drop. Refresh a server's capabilities when its tool list changes (on `tools/list_changed` where supported, and on reconnect). Add `reload(config)` that applies a validated config to the running host without a restart: start new servers, stop removed ones, reconnect changed ones, leave unchanged ones alone. A config that fails validation leaves the running host untouched.

An action in flight when its server drops is recorded UNKNOWN, never SUCCEEDED and never retried automatically (EXECUTION_SEMANTICS). Tests use fake stdio servers that can be killed and restarted: recovery without a Wyrit restart, UNKNOWN for the in-flight call, reload adding and removing a server, and a broken reload leaving the old config running. Produce the phase handoff.
