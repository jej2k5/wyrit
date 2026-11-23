# Phase 9 handoff

Wyrit's configured host opens one MCP session per server and discovers its exposed tools through `MCPAdapter`. Startup attempts run concurrently. A connection or discovery failure affects that server's capabilities. `host.health` reports each server's `connected`, `connecting`, or `down` state, a sanitized reason when available, and the UNIX timestamp of the last transition. Agent-facing search includes relevant server failures in an `unavailable` field.

The host detects stdio EOF and checks HTTP liveness with MCP pings. Reconnection backs off from 250 ms to a maximum of 2 seconds between attempts. The host discovers tools on each connection and again when it receives `notifications/tools/list_changed`. The first discovery requires all explicitly exposed tools to exist. Subsequent discovery can publish the remaining exposed tools after a tool disappears.

## Trusted host API

```python
from wyrit.configured_host import configured_host
from wyrit.mcp_server import create_server

with configured_host("wyrit.toml") as host:
    server = create_server(host)
    # An operator can call this while the server is running.
    host.reload("replacement.toml")
    print(host.health)
```

`reload` also accepts a resolved `HostConfig`. It validates the complete input before publishing changes, reuses unchanged sessions, stops removed sessions, restarts changed sessions, and waits for the first connection attempt on new and changed servers. Invalid input raises `ConfigError` before changing the running host. Reload is a trusted Python API available to the process hosting Wyrit.

## Execution semantics

Each execution takes a fresh run-scoped grant from the current live routes. Dispatch checks the route again at admission. In-flight calls interrupted by session loss return `OUTCOME_UNKNOWN` and are recorded as `UNKNOWN`. Reconnection restores discovery and service for future calls. The host never automatically replays an interrupted action.

## Verification

Run `python3 -m unittest discover -s tests`. The stdio integration tests kill a downstream process after it accepts a call, verify the action record and absence of a replay, and confirm recovery through the same agent-facing MCP session. They also cover notification-driven discovery, startup failure isolation, adding, removing and changing servers, and rejection of an invalid reload.
