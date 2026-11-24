# Embed Wyrit in a Python host

Install the project and run the example from the repository root. Execution requires Node.js with permission support and a supported native sandbox.

```sh
python -m pip install -e .
python examples/embed/main.py
```

The example runs the same JavaScript for two users. Each sees orders from their own tenant. The user without reporting access receives `DENIED` when the script asks for the total.

## Host and run context

Import `Capability`, `CapabilityError`, `Host`, and `RunContext` from `wyrit.embed`.

```python
from wyrit.embed import Capability, CapabilityError, Host, RunContext

lookup = Capability(
    "records.lookup",
    "Look up a visible record",
    {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
        "additionalProperties": False,
    },
    classification="read",
)


async def dispatch(capability_id, arguments, context):
    if context.attributes.get("role") != "reader":
        raise CapabilityError("DENIED", "Record access requires the reader role")
    return {"id": arguments["id"], "state": "ready"}


host = Host([lookup], dispatch)
context = RunContext("audit-reference", "user-17", "tenant-3", {"role": "reader"})
description = host.catalog_text(context)
# In an async function:
# result = await host.execute('return await tools.call("records.lookup", {id: "r1"});', context)
```

Pass a fixed iterable of `Capability` specs or a synchronous `catalog_provider(context)` that returns the capabilities visible to that context. A provider can be passed as the first argument or as a keyword. Each execution takes a fresh catalog snapshot and pins its grant to that snapshot. `catalog_text(context)` renders a bounded phase 15 description. For catalogs summarized by namespace, the embedding application can offer discovery using its own provider.

A capability supplies an ID, description, input schema, and `classification` of `"read"` or `"write"`. The default classification is `"write"`. An optional output schema defaults to accepting any JSON value. The host validates arguments before dispatch and validates returned values before sending them to the guest. Embedded hosts use the read-only write policy. Calls to write capabilities fail with `DENIED` and `WRITE_POLICY` before invoking dispatch.

Build each `RunContext` from authenticated host identity. Supply an opaque `context_id` for joining audit records, a string `principal`, a string `tenant`, and optional JSON-like `attributes`. Attribute mappings and sequences are copied into immutable mappings and tuples. The provider and dispatch receive the context object in trusted Python. The sandbox receives the script, run protocol messages, and recipe arguments when applicable. Audit records carry the context ID.

## Dispatch and execution

Dispatch is an async callable receiving `(capability_id, arguments, context)`. It runs on the caller's event loop and may overlap with dispatches from other runs. Apply current user and tenant authorization in dispatch before performing the operation. Use cancellation-cooperative async I/O. A deadline can cancel the local await while the external outcome remains unknown.

Return JSON-compatible values or raise `CapabilityError(code, reason)`. Supported codes are `DENIED`, `TOOL_ERROR`, `OUTCOME_UNKNOWN`, `CALL_TIMEOUT`, `INVALID_ARGUMENTS`, `RESULT_TOO_LARGE`, `DISPATCH_UNAVAILABLE`, and `INVALID_RESULT`. An explicit reason is guest-visible as `error.reason` and appears in the action audit record. Use a public reason suitable for both audiences. Other dispatch exceptions become `TOOL_ERROR` with a fixed message. Raise `OUTCOME_UNKNOWN` when a request may have taken effect and its outcome cannot be established. Wyrit makes each dispatch attempt once.

`await host.execute(source, context)` returns the MCP execution result shape, including `run_id`, `status`, `value`, `actions`, `unknown_calls`, and budget metadata. Successful runs have status `SUCCESS`. Each execution starts a fresh sandbox and has independent budget accounting. Pass `budgets=RunBudgets(...)` from `wyrit.budgets` to override run and capability limits.

## Recipes and audit

Pass `recipes` as a path to a directory of Wyrit recipe files. Use the existing proposal and operator approval workflow to prepare recipes. `await host.run_recipe(name, arguments, context)` checks approval, validates arguments, and verifies that the required capabilities match the current scoped catalog. It then runs with only the approved capability declarations. Recipe admission errors raise `ValueError`. Admitted recipe executions return the same result shape as `execute`.

By default, the JSONL ledger uses the existing state directory and stores action audit records inside the run record. Pass a synchronous `ledger_sink(record)` to send records to a host audit store. The sink runs on a trusted worker thread at run completion and can be called concurrently across runs. For each run it receives a record with `type="run"` followed by records with `type="action"` in call order. All records include `run_id` and `context_id`. If a sink raises, the result includes a generic `ledger_error`. Records already accepted by the sink remain in the host audit store.
