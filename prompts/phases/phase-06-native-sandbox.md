# Phase 6: Native sandbox enforcement

Implement platform backends that reinforce the portable runtime security contract: macOS sandbox-exec and Linux Bubblewrap. Detect platform support explicitly and fail closed when a promised guarantee cannot be enforced.

Create isolation probes for filesystem, network, process spawning, environment/credential leakage and unauthorized IPC. Process separation alone is not evidence of containment.

Document which guarantees each backend enforces and any platform limitations. Do not provide an unrestricted fallback under a development-mode label. Produce the phase handoff.
