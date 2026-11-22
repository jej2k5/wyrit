# Phase 3 handoff

The trusted Python runner and disposable Node.js child exchange version 1 JSON objects over private pipes, one UTF-8 line per message. Every envelope carries `version`, `run_id`, and `type`. The caller supplies a unique run ID and matching Phase 1 grant. Each process and its host protocol state serve one run.

| Direction | Type | Additional fields |
| --- | --- | --- |
| Host to child | `start` | `source`, `messageBytes` |
| Child to host | `call` | `call_id`, `capability`, `args` |
| Host to child | `call_result` | `call_id`, `result` |
| Host to child | `call_error` | `call_id`, `code` |
| Child to host | `result` | `result` |
| Child to host | `error` | `code` |

Calls start at ID 1 and increase by one up to 32. Denials consume an ID. The child accepts a reply only when it matches the pending call, run, version, and response shape. The host terminates the run on invalid requests, including stale or mismatched run IDs, duplicate and out-of-order IDs, and malformed envelopes. An unknown or ungranted capability returns `DENIED`. A reply that exceeds the wire budget returns `RESULT_TOO_LARGE`. Child execution and serialization failures use `EXECUTION_FAILED`.

The host enforces a 64 KiB payload limit per line, a 16 KiB encoded final value limit, 256 KiB total stdout, and 8 KiB total stderr. Wire values are JSON with unique object keys, nesting depth at most 64 (envelope at depth zero), and finite numbers with magnitude at most JavaScript's maximum safe integer. The guest snapshots plain objects and dense arrays and rejects unsupported values before sending a request.

The only dispatch target is the in-memory `fake.echo` endpoint. The protocol uses no MCP messages and performs no external actions or automatic retries. Phase 4 can attach MCP behind trusted dispatch. Phase 5 owns action lifecycle and must preserve unknown external outcomes without automatically replaying non-idempotent actions. Phase 6 owns native OS sandbox enforcement.

Run `python3 -m unittest discover -s tests` from the repository root.
