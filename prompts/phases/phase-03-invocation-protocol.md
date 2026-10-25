# Phase 3: Trusted invocation protocol

Define and implement a small bounded request/response protocol between the disposable runtime and trusted host. Include run_id, call_id, capability identifier, arguments, typed result/error and explicit protocol version.

The host must reject unknown capabilities, calls outside the run-bound capability set, stale/mismatched run IDs, duplicate call IDs, malformed messages, oversized messages and unsupported serialized values.

Do not make the protocol another MCP transport. Add adversarial tests for spoofing, cross-run confusion, replayed messages and serialization failures. Produce the phase handoff.
