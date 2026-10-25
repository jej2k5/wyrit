# Phase 0: Evidence and security contract

Do not implement product code.

Treat `docs/research/cloudflare-code-mode-rea.md` as the canonical complete REA investigation. Validate the governed evidence matrix, reference architecture, security model, execution semantics and design principles against it. Preserve its evidence labels and do not upgrade INFERRED or UNKNOWN claims without new evidence.

Resolve contradictions by documenting them, never by silently rewriting evidence. If a derived governance document conflicts with the canonical REA investigation, the canonical evidence wins and the discrepancy must be surfaced for human review.

Produce `docs/HANDOFF_PHASE_0.md` covering summary, evidence checks, decisions, contradictions, unresolved unknowns and next phase. No product implementation and no architecture expansion beyond supported evidence are allowed.

Acceptance intent: the repository must clearly distinguish OBSERVED, SOURCE-VERIFIED, INFERRED and UNKNOWN claims and preserve WY-SEC-001 through WY-SEC-010.
