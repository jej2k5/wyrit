# Phase 6: Native sandbox enforcement

Implement platform backends that reinforce the portable runtime security contract: macOS sandbox-exec and Linux Bubblewrap. Detect platform support explicitly and fail closed when a promised guarantee cannot be enforced.

Create isolation probes for filesystem, network, process spawning, environment/credential leakage and unauthorized IPC. Process separation alone is not evidence of containment.

Document which guarantees each backend enforces and any platform limitations. Do not provide an unrestricted fallback under a development-mode label. Produce the phase handoff.

## Operator decisions for this phase (2026-11-22)

**The gate cannot run native sandboxes.** buildctl gates already run inside a sandbox, and the host refuses a nested `sandbox_apply`, so native tests skip in the gate. That is expected, not a defect. The operator runs the native suite on a real macOS host at the checkpoint before phase 7 and does not sign that checkpoint unless it passes:

```
PYTHONPATH=src WYRIT_REQUIRE_NATIVE_SANDBOX=1 python3 -m unittest discover -s tests
```

A pass means every test runs and only Linux-only tests skip, with `test_native_policy_denies_ambient_authority` and the runtime, protocol, lifecycle and MCP suites executed. The handoff must print this command and must not claim native containment is verified by the gate.

**Linux Bubblewrap is accepted as unverified.** No eligible Linux host is available. Ship the backend, keep it fail-closed, and label it UNVERIFIED in the handoff and the guarantees document. That label is the operator's recorded risk acceptance for WY-SEC-007 on Linux; it is not a finding to resolve in this phase.

**The macOS profile must start Node.** The current Seatbelt profile aborts Node at startup (exit -6) on a real Mac. Host testing found exactly what it lacks, with Homebrew Node 26 on Apple silicon. Keep `(deny default)` and add only these:

1. **Every step of symlink resolution.** The kernel checks each intermediate path, not only the literal and the fully resolved file. `/opt/homebrew/opt/llhttp/lib/libllhttp.9.4.dylib` is reached through `/opt/homebrew/Cellar/llhttp/9.4.2/lib/libllhttp.9.4.dylib`, a symlink. For each runtime file, repeatedly resolve the parent folder (`realpath(dirname) + basename`), allow that path with `file-read* file-map-executable`, and follow the link until the target is a regular file. Cap the loop.
2. **`file-read-metadata` on every ancestor folder** of every allowed file, plus `/`.
3. **`file-read-data` on `/`, `/dev/urandom` and `/dev/random`.** Node aborts without being able to read `/`. That lets the guest list top-level folder names; record it as a known macOS limitation.
4. **`--openssl-config=/dev/null` on the macOS launch line**, before the runtime's arguments. Otherwise OpenSSL opens Homebrew's `openssl.cnf` and Node exits with "OpenSSL configuration error".
5. **`sysctl-read` for `kern.ostype`, `kern.version` and `kern.hostname`**, beside the names already allowed. The runtime calls uname, and Node asserts in `GetOSInformation` without them. `kern.hostname` exposes the host's name to the guest; record it as a known limitation.
6. **Hardware sysctls by prefix, not by name: `(sysctl-name-prefix "hw.") (sysctl-name-prefix "machdep.cpu.")`.** Node and its allocator read some hardware values by numeric MIB, which only a prefix rule matches. A list of `hw.*` names fails even when it names every `hw.` sysctl on the host: Node aborts with `LowLevelAlloc arithmetic overflow` (exit -6). Attempt 2 of this phase narrowed the rule to seven names and failed host verification that way; with the two prefixes and everything else unchanged, all 35 tests passed on the host.

With exactly these additions, all 43 tests pass on that Mac with `WYRIT_REQUIRE_NATIVE_SANDBOX=1` (only `test_linux_data_limit_survives_exec` skips). Probes from inside the guest still showed reading a home-folder file, writing to `/tmp`, spawning a child and opening a TCP connection all denied with EPERM. Don't widen further: no `(subpath "/opt/homebrew")`, no blanket `file-read*`, no blanket `sysctl-read`.
