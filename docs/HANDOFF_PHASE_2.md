# Phase 2 handoff

Call `wyrit.runtime.run_javascript(source, run_id=..., authorization=..., timeout=...)` from the trusted parent. Supply JavaScript as an async function body. The runner returns a `RunResult` with status `SUCCESS`, `FAILED`, or `TIMED_OUT`. Only successful runs carry a JSON value. Failures carry a fixed diagnostic.

Create the grant with Phase 1's `CapabilityRegistry.authorization_for`. The runtime checks the trusted run ID before launch and checks the grant for every request. The generated function may await `tools.call("fake.echo", value)`. The Python parent implements that endpoint as an in-memory JSON echo. It supports one outstanding call per run. This endpoint performs no external actions. Downstream action recording, cancellation, unknown outcomes, and replay policy belong to later phases.

The default deadline is two seconds. The parent uses a monotonic clock and kills and reaps the child on failure or timeout. Each launched run starts a fresh Node.js process with an empty environment. The parent limits each wire payload to 64 KiB, the JSON-encoded result to 16 KiB, child stdout to 256 KiB total, child stderr to 8 KiB total, and requests to 32. The guest also bounds serialization depth and rejects malformed values such as cycles, sparse arrays, and non-finite numbers. Guest stdout and stderr have no exposed API. The child's stdout carries protocol messages.

The runner targets Unix systems with selectable pipes and a Node.js binary that supports the required flags. It was verified with Node.js 25.8.0. Missing Node.js and startup failures fail closed. The guest receives standard JavaScript computation and the asynchronous capability interface. The context disables string code generation, WebAssembly code generation, and module loading. The child also enables Node.js permissions without grants.

The VM context and Node.js permissions provide language and runtime restrictions. Native OS sandbox enforcement belongs to Phase 6. This runner relies on the integrity of Node.js and V8 and has no OS-enforced memory quota. The fake endpoint can be exercised without credentials or network access.

Run `python3 -m unittest discover -s tests` from the repository root.
