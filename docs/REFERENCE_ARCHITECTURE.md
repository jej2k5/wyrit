# Reference architecture

## Goal

Wyrit lets agents discover capabilities, wire them together in generated JavaScript, and execute the composition while keeping authority in a trusted host.

```text
Agent
  | MCP: search / execute
  v
Wyrit Host
  |- Capability Registry + Discovery
  |- Run Controller
  |- Authorization + Validation
  |- Budgets + Accounting
  |- Result Adaptation
  '- Trusted Invocation Boundary
       |- MCP adapters
       |- API adapters
       '- Data adapters
  ^
  | bounded invocation/result channel
  v
Disposable restricted JavaScript execution unit
```

The host is one trust domain in v0, not a requirement for one source module. Modules may later become processes only for a demonstrated security or operational requirement.

## Public surface

v0 exposes two agent-facing tools: `search` and `execute`. Search returns only relevant capability metadata rather than the entire inventory. Execute accepts JavaScript and returns a structured run outcome.

## Runtime surface

Generated JavaScript receives computation plus:

```js
const result = await tools.call("customers.search", { region: "midwest" });
return result;
```

No ambient filesystem, network, process, package-loading, or credential authority is part of the contract.

## Non-goals for v0

Durable workflow engine, transparent replay, automatic rollback, generalized approval/resumption, multiple JS backends, distributed supervisor, and Cloudflare-specific infrastructure.
