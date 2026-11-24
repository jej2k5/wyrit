# Phase 16 handoff — Reviewed recipes

Set a top-level `recipes_path = "recipes"` in Wyrit's TOML. Relative paths are resolved against that file. Each recipe is stored in `<name>.json` in the directory. MCP clients discover `propose_recipe` and `run_recipe` when this directory is configured.

Propose a recipe with its name, description, argument schema, and JavaScript function body. Wyrit parses the body and records the schema digest and class for each literal `tools.call` target. The proposal is pending and an existing name is refused. Open **Recipes** in the loopback console, review the source, schema, and capabilities, then enter an operator label and approve. Approval and revocation preserve the prior file in `<name>.json.bak`. The console also keeps the last approved version for comparison with subsequent hand edits.

An approved recipe appears in the `execute` catalog and in `search` when the approval digest and all capability fingerprints match. Call `run_recipe` with the recipe name and an arguments object. The guest receives that object as `args` and gets the same result envelope as `execute`. The host checks file content and current capability exposure at admission. Calls outside the declared set get `RECIPE_UNDECLARED`. Runs share the same sandbox, budget enforcement, and write policy as `execute`. The ledger identifies the recipe and source digest, and the console reports run count, success rate, and nearest-rank p50 and p95 elapsed time.

For hand-authored files, `capabilities` maps each capability ID to `{"schema_digest": "...", "class": "read"}`, with `class` set to either `read` or `write`. Digests use SHA-256 over ASCII-escaped JSON with sorted keys and compact separators. The capability digest hashes its input schema. The approval digest hashes the JSON array `[source, input_schema, capabilities]`. Leave `approval` absent for review in the console. Approval records `approved_at` with a timezone and `approved_by` as entered by the operator.

## Worked example

The fan-out lists tickets for a project, fetches each ticket in sequence, and returns the ticket count and total points. See [ticket-count-and-sum.json](../examples/recipes/ticket-count-and-sum.json) for a pending recipe. The example expects `tickets.list` to accept `{project}` and return `{ids}`, and `tickets.get` to accept `{id}` and return `{points}`. Copy the file into the configured directory and approve it after reviewing the current tool definitions, or submit its name, description, schema, and source using `propose_recipe` to have Wyrit derive the fingerprints. For the 30-call fan-out, the listing returns 29 IDs. To run it after approval, send the following arguments to the `run_recipe` MCP tool.

```json
{"name":"ticket-count-and-sum","arguments":{"project":"sample-project"}}
```

Proposal parsing uses the Acorn parser bundled with the supported Node runtime through its internal module name. A Node release that changes this module path will require a parser integration update. Proposals and console approvals fail closed in that case.
