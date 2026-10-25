# Design principles

1. Trust boundaries over service diagrams.
2. Capabilities over transports. MCP is an adapter, not the core model.
3. Authority stays in the trusted host.
4. Generated code gets computation plus a narrow call interface.
5. Discovery is progressive. Do not flood model context with the full tool catalog.
6. Runtime validation and authorization are distinct from schema display.
7. Execution termination and external-action completion are different facts.
8. Ambiguity is represented, not hidden.
9. Platform enforcement implements a portable contract and fails closed.
10. Start small. Do not build replay, rollback, workflow orchestration or distributed control until evidence requires them.
