# Phase 4: MCP adapter and authorization

Implement MCP as one adapter behind the Capability abstraction. The trusted host owns MCP connections and credentials. The disposable runtime must have no direct MCP connectivity.

Map discovery metadata, schemas, arguments, structured/text content, bounded binary handling and errors deliberately. Validate arguments and authorize every invocation at the trusted boundary.

Prove end to end that generated JavaScript can compose multiple MCP-backed capabilities while the runtime itself holds no MCP credential or connection. Add negative authorization and result-adaptation tests. Produce the phase handoff.
