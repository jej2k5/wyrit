# Phase 11 handoff

Search results include `classification` with a value of `read` or `write`. Wyrit classifies an MCP tool as a read when its annotations declare `readOnlyHint: true` and do not indicate a destructive operation. A true `destructiveHint` takes precedence if both hints are true. An explicit `readOnlyHint: false`, missing hints, and unrecognized hints default to write. A configured `classification` takes precedence over the annotations.

`deny_writes` defaults to `true` for each downstream server. Set it to `false` on a server to permit its writes or on a single capability to permit just that write. A capability-level setting overrides the server's setting in either direction. Read calls and permitted write calls still require the run's capability grant.

```toml
[servers.inventory]
namespace = "inventory"
transport = "stdio"
command = "/path/to/inventory-mcp"
expose = ["get_item", "update_item", "delete_item"]
deny_writes = true

[servers.inventory.capabilities.get_item]
classification = "read"

[servers.inventory.capabilities.update_item]
deny_writes = false
```

Capability policy keys are the exact downstream MCP tool names. Quote names containing dots in TOML table headers. Here, `get_item` is a read and `update_item` can write. If `delete_item` is classified as a write, it remains blocked.

A policy denial rejects `tools.call` with a catchable JavaScript error. The ledger records `DENIED` with the capability ID, and no downstream call or dispatched action is created. Policy changes on reload replace the affected server slot, so a run bound to the previous slot cannot send a new call through it. The `deny_writes` setting follows MCPxy's deny-by-default convention. Wyrit has no MCPxy dependency.

Run `python3 -m unittest discover -s tests`. The policy tests verify classification, config precedence, reload behavior, ledger results, and downstream receipt over stdio MCP. The native integration test also verifies that JavaScript can catch the denial when the platform supports Wyrit's sandbox.
