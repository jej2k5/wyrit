# Phase 6 handoff and sandbox guarantees

Wyrit launches generated JavaScript only through a supported native backend, with the existing Node.js permissions and VM restrictions still active. Unsupported platforms, missing tools, policy setup errors, and launcher refusals fail the run before generated code can execute. The host starts each launcher with an empty environment and closes inherited descriptors other than the three standard streams and Bubblewrap's setup-only seccomp descriptor. The trusted host continues to authorize and dispatch capability calls through the run-scoped protocol and preserves external action outcomes independently of guest termination.

## macOS Seatbelt

`/usr/bin/sandbox-exec` applies a deny-default profile. It allows execution of the trusted Node binary, literal reads and executable mappings for its runtime files, and metadata reads on their ancestor directories. Runtime discovery follows intermediate symlinks and resolves Mach-O loader paths using `/usr/bin/otool`. The profile allows reads of `/dev/null`, `/dev/urandom`, and `/dev/random`. Node starts with `--openssl-config=/dev/null` so startup needs no Homebrew OpenSSL configuration access. The profile grants no ambient filesystem writes, network sockets, child creation, or external IPC access such as Mach service lookups.

Native code can list top-level names through the read allowance on `/`. It can also read the host name through `kern.hostname`, the selected kernel identity and hardware sysctl values, trusted runtime files, and granted ancestor metadata. The profile relies on a trusted, stable Node installation and its dependencies. macOS may refuse Seatbelt activation inside another sandbox.

## Linux Bubblewrap — UNVERIFIED

The backend requires x86-64 or arm64 Linux, a trusted glibc Node installation, Bubblewrap with user-namespace restrictions, libseccomp, and util-linux `prlimit`. It requests separate user, PID, IPC, UTS, network, and mount namespaces, disables nested user namespaces, and drops capabilities. The read-only root exposes the trusted Node executable, resolved ELF dependencies, and `/dev/null`. The seccomp filter blocks socket creation, process creation while permitting threads, and selected IPC and namespace operations. The launcher applies a 512 MiB hard and soft data limit before exec and ties the guest's lifetime to the launcher. Node and its runtime files remain visible inside the sandbox. The data limit bounds the data segment and eligible allocations, rather than total resident memory. [Bubblewrap manual](https://github.com/containers/bubblewrap/blob/main/bwrap.xml)

No eligible Linux host was available. The UNVERIFIED label records the operator's WY-SEC-007 risk acceptance for Linux.

## Checkpoint before phase 7

Run the full suite from the repository root on a macOS host where Seatbelt can activate.

```sh
PYTHONPATH=src WYRIT_REQUIRE_NATIVE_SANDBOX=1 python3 -m unittest discover -s tests
```

Sign the checkpoint only when the suite passes with just the Linux-only data limit test skipped. Confirm that `test_native_policy_denies_ambient_authority` and the runtime, invocation, lifecycle, and MCP suites ran. The native probe uses a trusted Node script with Node permission flags omitted, a host-file canary, a write target, an inherited-environment canary, a child launch, and live TCP and Unix socket listeners. The buildctl gate refused nested `sandbox_apply` and skipped the native-dependent tests. Its passing result provides no verification of native containment.
