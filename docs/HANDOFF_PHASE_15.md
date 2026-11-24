# Phase 15 handoff

Small catalogs are available in `execute` on the first model turn. Each entry shows the capability ID, class, argument signature and one-line description. The complete description is limited to 8 KiB and the inline catalog to 60 capabilities. Over either limit, the description shows namespace counts and directs the agent to `search`. If the namespace summary itself exceeds 8 KiB, it shows the counts that fit and indicates that more namespaces are available. `search` continues to return the full schemas.

After a client lists tools, the live server checks its scoped catalog every 100 ms and sends `notifications/tools/list_changed` when the visible descriptors or effective write policy change. A new list request reads the latest catalog immediately. Rapid changes between checks may be coalesced.

Scripts can branch on `error.code`, `error.capability` and, for a denial, `error.reason`. `WRITE_POLICY` marks a policy denial and `NOT_AUTHORIZED` marks a call outside the available grant. The runtime preserves the public messages and existing action status and timeout handling. The guest receives fixed error codes and denial reasons, and its capability field comes from the script's call. The benchmark checker also accepts a single standalone JSON or bare Markdown fence while retaining the existing answer and call-outcome checks. Benchmark cost and timing were not rerun.

## Two-server example

This example uses the `local` and `remote` namespaces from the sample configuration. Assume the local server exposes `get_item` and `update_item`, with the local policy classifying `get_item` as read. Assume the remote server advertises both exposed tools as read-only. Tool names containing underscores keep their names in the capability ID.

```text
Run a JavaScript async function body. Use return to produce a JSON value.
Inside the script, await tools.call("capability.id", args) for a JSON result. A rejected call throws an Error with read-only code and capability fields.
Codes: DENIED, TOOL_ERROR, OUTCOME_UNKNOWN, CALL_TIMEOUT, INVALID_ARGUMENTS, RESULT_TOO_LARGE, DISPATCH_UNAVAILABLE, INVALID_RESULT.
DENIED also has reason WRITE_POLICY or NOT_AUTHORIZED.

Capabilities (* required):
local.get_item [read] ({"id"*: string}) — Get a local item
local.update_item [write] ({"id"*: string, "name"*: string}) — Rename a local item
remote.get_item [read] ({"id"*: string}) — Get a remote item
remote.list_items [read] ({"limit": integer}) — List remote items
```
