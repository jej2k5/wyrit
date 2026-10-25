# Security model

## Trust boundary

Generated code is untrusted. The Wyrit host is trusted to hold authority, validate requests, enforce policy, dispatch capabilities, account for actions, and terminate execution units.

## Governed invariants

- WY-SEC-001: Generated code receives no credentials.
- WY-SEC-002: Generated code receives no ambient network authority.
- WY-SEC-003: Generated code receives no ambient filesystem authority.
- WY-SEC-004: Generated code cannot spawn arbitrary processes.
- WY-SEC-005: Every external capability invocation passes through the trusted Wyrit host.
- WY-SEC-006: Every invocation is authorized against the capability set bound to that run.
- WY-SEC-007: Unsupported promised sandbox guarantees fail closed.
- WY-SEC-008: Terminating JavaScript is never represented as cancellation of already-dispatched external actions.
- WY-SEC-009: Unknown external action outcomes remain UNKNOWN.
- WY-SEC-010: Non-idempotent operations are never automatically replayed without an explicit safety mechanism.

## Threats

Primary threats include credential exfiltration, ambient host access, confused-deputy routing, cross-run message confusion, stale run IDs, serialization abuse, resource exhaustion, oversized IPC, unauthorized capability invocation, and misleading cancellation semantics.

Platform-specific enforcement implements a portable security contract. It must not silently weaken that contract.
