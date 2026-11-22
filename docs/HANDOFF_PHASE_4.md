# Phase 4 handoff

The trusted Python host initializes the MCP SDK session and owns its transport and credentials. Run the async session on an AnyIO blocking portal. Create `MCPAdapter(namespace, session, portal)`, register `adapter.capabilities` in a `CapabilityRegistry`, issue a grant for the run and pass `dispatch=adapter.dispatch` to `run_javascript`. Keep the session and portal open until the run returns. The Node child receives JavaScript source and exchanges capability IDs, JSON arguments, and adapted results over the existing private pipes.

## Discovery and validation

- Discovery snapshots paginated `tools/list` results. Capability IDs are namespaced. Tool names that require escaping use the reserved `encoded-` prefix followed by the hex encoding of the original name. Dispatch always uses the exact original MCP name.
- Descriptors expose the tool description and input and output schemas. Titles, annotations, and original tool names are host adapter metadata. MCP `_meta` is discarded, and annotations grant no authority.
- Every invocation must match the run ID and the run's exact capability grant before reaching the adapter. The adapter requires JSON object arguments and validates them against the discovered schema using JSON Schema Draft 2020-12. Local references work and validation performs no external schema retrieval.

## Results and outcomes

| MCP response | Runtime behavior |
| --- | --- |
| Structured content | Return the value directly. Validate against the tool's output schema when declared and discard companion display blocks. |
| Content without a declared output schema or structured value | Return an ordered `{content: [...]}` with text and inline base64 image or audio blocks. |
| Missing or invalid structured output, malformed content, embedded resources or resource links | Return `INVALID_RESULT`. The adapter never follows a resource URI. |
| Oversized result | Return `RESULT_TOO_LARGE`. Inline binary is capped at 8 KiB decoded per invocation. Existing 64 KiB wire and 16 KiB final-value limits apply. |
| Tool result with `isError` | Return `TOOL_ERROR` with a fixed guest message. |
| MCP request, transport or timeout failure after invoking the session | Return `OUTCOME_UNKNOWN` when the guest can still receive a reply. Store the call ID in the host's `RunResult.unknown_calls` even if the run times out or the guest handles the error. |

The adapter makes one request per admitted call. A run deadline bounds the wait for MCP and cannot confirm what the server did. A persistent action ledger and downstream cancellation semantics belong to Phase 5. The SDK parses upstream frames before the adapter applies the guest result bounds, so an operator must trust the configured server and transport with host resources. Successful tool outputs are data supplied by that server. The process and VM guards are inherited from Phase 3, with native OS sandbox enforcement scheduled for Phase 6.

The end-to-end test uses actual MCP stdio requests through the SDK and composes two tools inside generated JavaScript. It checks that the guest lacks ambient network, filesystem, process, and credential access and that rejected calls do not reach MCP. Additional cases cover pagination, invalid schemas and results, binary bounds, sanitized errors, and unknown outcomes with no automatic replay.

Run `python3 -m unittest discover -s tests` from the repository root with the project dependencies installed.
