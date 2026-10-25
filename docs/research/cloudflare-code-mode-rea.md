# Cloudflare Code Mode REA findings

This governed summary records the REA conclusions supplied before Wyrit implementation. The underlying investigation reported 47 local probes and source inspection. Preserve the complete raw investigation separately when importing it; do not upgrade INFERRED claims to observed facts.

## Supported conclusions

- OBSERVED: an agent can submit JavaScript through MCP and receive program results.
- OBSERVED: generated JavaScript can orchestrate multiple host capabilities.
- OBSERVED / SOURCE-VERIFIED: tools and MCP clients can remain outside the execution environment.
- SOURCE-VERIFIED: credentials can remain in trusted components; tested child bindings did not contain the outbound token.
- INFERRED: execution needs independent lifecycle control because host actions can continue after JavaScript returns or times out.
- INFERRED: MCP, APIs, and data can share one capability abstraction.

## Corrections to initial assumptions

- Architectural responsibilities do not require separate services or processes.
- Native OS sandboxing is an enforcement choice, not part of the agent-facing Code Mode contract.
- Inspected execution paths did not require MCP clients inside generated-code execution.
- Not every capability invocation is MCP.
- Terminating JavaScript does not terminate already-dispatched external actions.
- Published schemas are not enforcement by themselves.
- Process separation alone is not a security sandbox.

## Mechanisms worth preserving

Discovery, explicit invocation protocol, host-side validation, execution-scoped capabilities, deliberate result adaptation, bounded output, and separate execution/action accounting.

## Mechanisms not to copy

Cloudflare deployment-specific Worker Loader, Durable Object, Workers RPC, R2 and compatibility machinery; mutable in-realm security timers; hostname-only credential authorization; blind write retries; declarations treated as validation; unsafe thrown-value assumptions; unsupported replay/rollback promises.

## Recommended minimum architecture

One trusted host owns MCP serving, discovery, run control, authorization, validation, upstream connections, credentials, adapters, accounting and result formatting. One disposable execution unit per run receives computation plus a narrow `tools.call(...)` interface and no ambient authority. Native OS sandboxing reinforces the runtime contract.
