# Phase 1: Capability model and discovery

Implement the smallest transport-neutral Capability and CapabilityRegistry model. Support stable identifiers, descriptions, input/output schemas, adapter metadata, deterministic serialization, duplicate rejection, search, describe/resolve and run-scoped authorization metadata.

MCP must not be embedded into the core model. Search must support progressive discovery rather than requiring the entire inventory in model context.

Add focused unit tests for stable IDs, duplicate handling, deterministic results, schema handling, search relevance mechanics and authorization metadata. Produce the phase handoff.
