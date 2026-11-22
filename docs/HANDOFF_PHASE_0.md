# Phase 0 handoff

## Summary

The [canonical REA investigation](research/cloudflare-code-mode-rea.md) documents generated JavaScript using two trusted broker paths under pinned local conditions. Agents uses Workers RPC to reach host tools. The MCP service mediates HTTP through an outbound handler. Wyrit adopts host mediation and defines its own security guarantees. This handoff is the only Phase 0 change.

## Evidence checks

**OBSERVED** describes behavior recorded in the local probes. **SOURCE-VERIFIED** describes facts established by inspected source. **INFERRED** describes implications drawn from evidence. **UNKNOWN** describes questions the evidence leaves open. Canonical classifications and scope govern any conflicting derived claims.

| Governed document | Check against canonical REA |
|---|---|
| [Evidence matrix](research/EVIDENCE_MATRIX.md) | §§1–8 support local JavaScript submission and host mediation, transport neutrality, and credential custody in the inspected setup. §§12 and 16 support separating execution from side effects. §§4 and 17 leave production isolation UNKNOWN. See the scope differences below. |
| [Reference architecture](REFERENCE_ARCHITECTURE.md) | §§5–8 support the broker boundary across RPC and HTTP. A disposable JS unit, a trusted Wyrit host, and the specific call interface are Wyrit decisions. |
| [Security model](SECURITY_MODEL.md) | §§8–11 and 17 motivate credential custody and ambient-authority restrictions. Module and binding probes cover specific configurations. Run-bound authorization and fail-closed enforcement are Wyrit requirements. |
| [Execution semantics](EXECUTION_SEMANTICS.md) | §§12–16 show later commits after timeout, earlier effects surviving later failure, and repeated writes. Host-owned deadlines, structured lifecycle states, admission rules and resource bounds are Wyrit requirements. |
| [Design principles](DESIGN_PRINCIPLES.md) | §§1–2 and 5–8 motivate progressive discovery, transport-neutral capabilities and host custody. The canonical policy implications retain their INFERRED classification. Excluding durable orchestration and rollback is a Wyrit v0 decision. |

The review relies on the canonical report for A1 and M1's partial static coverage and R1's 47 local probes. The linked raw captures and probe sources are absent from the tracked repository.

## Contradictions and scope differences for human review

- The matrix labels multiple-capability composition OBSERVED. R1 shows successive brokered calls and an earlier effect surviving later failure (§16). Arbitrary cross-transport composition exceeds that scope.
- The matrix labels non-cancellation on JS termination OBSERVED. R1 shows a host action committing after timeout or early return (§12). Gate behavior is SOURCE-VERIFIED. Hard termination and production downstream cancellation remain UNKNOWN.
- The matrix uses CONTRADICTED and UNSUPPORTED as verdicts. The underlying schema evidence is the OBSERVED out-of-catalog request (§16) and the INFERRED general enforcement implication (§17). Process sampling establishes a local workerd child (§4). The strength of production native isolation remains UNKNOWN.
- Wyrit v0 exposes two tools. The service also registers docs and whoami (§2, SOURCE-VERIFIED). The Agents wrapper advertised the discovered synthetic inventory (§1, OBSERVED). Progressive discovery is a Wyrit decision informed by service search and the separate source-verified connector search and describe path.
- The service permits mediated raw fetch, including a different port and path on the allowed hostname (§9, OBSERVED). Agents permits custom outbound and extra bindings (§§8–9, SOURCE-VERIFIED). The broader credential containment implication, including the response channel, remains INFERRED (§8). Wyrit specifies a narrow host call boundary and credential exclusion.
- The Agents timer permitted overrun and tampering, and the service repeated a POST after a synthetic 429 (§§12, 16, OBSERVED). The inspected retry policy has no read-only restriction (SOURCE-VERIFIED). Wyrit specifies a trusted external deadline and explicit safety before automatic non-idempotent replay.

## Security decisions

These requirements govern Wyrit. Implementation phases must verify their enforcement.

| ID | Contract |
|---|---|
| WY-SEC-001 | Generated code receives no credentials. |
| WY-SEC-002 | Generated code has no ambient network authority. |
| WY-SEC-003 | Generated code has no ambient filesystem authority. |
| WY-SEC-004 | Generated code cannot spawn arbitrary processes. |
| WY-SEC-005 | All external capability invocations pass through the trusted host. |
| WY-SEC-006 | Every invocation is authorized against its run-bound capability set. |
| WY-SEC-007 | The sandbox fails closed when a promised guarantee cannot be enforced. |
| WY-SEC-008 | Execution termination and dispatched action completion or cancellation remain separate facts. |
| WY-SEC-009 | Dispatched actions with unestablished completion retain UNKNOWN outcomes. |
| WY-SEC-010 | Automatic replay of non-idempotent actions requires an explicit safety mechanism. |

## Unresolved unknowns

- Production parity, hard quotas and termination, untested socket/module/binding paths, and native isolation strength remain UNKNOWN.
- Durable approval and replay across crashes, ambiguous remote commits, and production OAuth storage protection remain UNKNOWN.
- Live model payload shaping and result handling remain UNKNOWN. Evidence reaches the captured descriptors and client-facing results.
- Later Wyrit phases must establish platform enforcement, trusted call accounting and downstream cancellation or retry semantics for the guarantees they claim.

## Next phase

Phase 1 defines the transport-neutral Capability and CapabilityRegistry with stable IDs, descriptions, input/output schemas, adapter metadata, deterministic serialization, duplicate rejection, progressive search, describe/resolve and run-scoped authorization metadata. Discovery informs callers. Invocation authority comes from the host's run-bound capability set. [Phase 1 prompt](../prompts/phases/phase-01-capability-model.md)
