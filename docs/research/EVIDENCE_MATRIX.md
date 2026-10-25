# Evidence matrix

| Claim | Evidence class | Wyrit decision |
|---|---|---|
| Agent can submit JavaScript through MCP | OBSERVED | Expose a minimal execute surface |
| JavaScript can compose multiple capabilities | OBSERVED | Keep composition inside the execution unit |
| Tool/MCP clients can stay outside execution | OBSERVED / SOURCE-VERIFIED | All upstream authority remains in trusted host |
| Credentials can stay outside execution | SOURCE-VERIFIED | Never inject credentials into generated code |
| JS termination does not cancel dispatched work | OBSERVED | Separate run and action lifecycle state |
| Every capability need not be MCP | SOURCE-VERIFIED | Define transport-neutral Capability |
| Schemas alone enforce authorization | CONTRADICTED | Validate and authorize every host invocation |
| Process separation alone is a sandbox | UNSUPPORTED | Apply explicit authority restrictions and native enforcement |
| Native OS sandbox is required by Code Mode | UNKNOWN | Treat native sandbox as Wyrit enforcement, not compatibility semantics |

Evidence labels are immutable unless new evidence is added. Reviewer must reject claims that silently convert INFERRED or UNKNOWN into OBSERVED/SOURCE-VERIFIED.
