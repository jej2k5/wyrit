# Phase 19 Linux sandbox verification

Linux host and Docker containment remain unverified. The local gate ran on macOS, where the Linux verifier refused before launch. The Docker CLI could not connect to a Linux daemon. Unittest ran 121 tests with 84 passes and 37 skips. Mock tests cover default refusal before subprocess launch, a wrong OS, missing Bubblewrap, a setuid launcher, missing libseccomp, and a launcher that rejects namespace setup before reading guest input.

| Probe | Linux host | Docker defaults |
| --- | --- | --- |
| Trusted Node startup after Bubblewrap namespace creation and seccomp installation | Pending | Pending |
| Read a host file containing a credential canary | Pending | Pending |
| Write a file to host temporary storage | Pending | Pending |
| See a credential canary in the process environment | Pending | Pending |
| Spawn another process | Pending | Pending |
| Connect to a live host TCP listener | Pending | Pending |
| Connect to a live host Unix socket | Pending | Pending |
| Retain both the soft and hard 512 MiB data segment limits after exec | Pending | Pending |
| Full runtime suite through the native launcher | Pending | Pending |

Wyrit refuses Linux execution by default. After every probe and the native-required suite pass in the deployment environment, set `WYRIT_ENABLE_LINUX_SANDBOX=1` in the trusted Wyrit host process environment. The verifiers use this opt-in for trusted tests. The guest receives an empty environment. Repeat verification after changes to the kernel, runtime, launcher or container policies before enabling the backend on that configuration.

Wyrit requires non-setuid Bubblewrap, prlimit, glibc Node and libseccomp. Bubblewrap must create the required namespaces, disable further user namespace creation, apply the filesystem policy and load the syscall filter before launching Node. A launcher failure surfaces a bounded public reason and produces no guest outcome even with the opt-in.

## Linux host

On Ubuntu, install `bubblewrap`, `libseccomp2`, `util-linux`, `python3-venv` and a trusted glibc build of Node.js 26. Use a regular account.

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install .
scripts/verify-linux.sh
```

Save the complete output, OS and kernel version, architecture, and Bubblewrap and Node versions with the deployment evidence. The verifier first starts a trusted Node sentinel through the actual backend to exercise unprivileged namespace setup in the correct AppArmor context. It then requires native execution in the full suite and prints the containment test results. On GitHub, run **Linux sandbox** manually to collect Ubuntu runner evidence. Pull requests that touch the backend also trigger it.

Use `readiness` for the startup row and `Host file read`, `Host file write`, `Host environment`, `Process spawn`, `Host TCP` and `Host Unix IPC` for the corresponding containment rows. Use `Linux hard and soft data segment limits` for the limit row and the unittest result for the full-suite row. A missing probe result leaves that row pending.

## Docker

Run `scripts/verify-linux-container.sh` against a Linux Docker daemon. The verifier builds the Bookworm-based image, runs as UID 10001 with Docker defaults, and preserves the exit status. Record the daemon version, kernel, security options, image ID and full probe output.

If the default run fails, the script reports the active seccomp state, AppArmor or LSM label, visible namespace limits, and the result of a bare user namespace probe. A zero namespace limit or disabled unprivileged namespace setting identifies a kernel blocker. For a controlled comparison, prepare the following inputs before running the verifier. It still starts with Docker defaults and uses each supplied diagnostic policy separately.

- Set `WYRIT_DOCKER_DEFAULT_SECCOMP` to the exact effective default seccomp JSON for the daemon. The script appends the vendored rules for the exact Bubblewrap namespace clone flags and for `unshare(CLONE_NEWUSER)`. These rules assume the supported amd64 or arm64 clone ABI. They leave later operations, including mounts, subject to the daemon baseline.
- On an AppArmor host, copy the effective default container profile source using the same ABI and rules. Rename the profile and its matching peer labels to `wyrit-readiness`, insert the vendored `examples/container/apparmor-readiness.rules` inside the profile, and load that copy on the daemon host with `sudo apparmor_parser -a /path/to/wyrit-readiness`. Set `WYRIT_DOCKER_APPARMOR_DIAGNOSTIC=1`. This comparison tests only the AppArmor user namespace permission and retains mount restrictions. If the baseline already permits user namespaces, the comparison adds no permission.

The reruns use only the trusted readiness sentinel. A successful rerun identifies a single change sufficient for readiness and leaves the default run's failure status intact. If the default sentinel passed and a later test failed, the failure is in the suite. If neither diagnostic passes, multiple restrictions or a restriction outside the tested rules may be present. Correlate the UTC start time with the **Docker host's** kernel audit records, for example with `sudo journalctl -k --since '5 minutes ago'`. A matching AppArmor record can identify the profile and operation. A matching seccomp record can identify the syscall and architecture. [Docker seccomp documentation](https://docs.docker.com/engine/security/seccomp/) and [Docker AppArmor documentation](https://docs.docker.com/engine/security/apparmor/)

No Docker override is verified. For a future review, scope each change to the blocker confirmed on that daemon and rerun the full verifier after each policy change.

| Confirmed blocker | Least-privilege review scope and exposure |
| --- | --- |
| Seccomp | Keep the installed Docker default profile as the baseline. A container-specific exception would expose the allowed namespace or mount syscall and argument combinations to every process in that container, including the trusted host process before Wyrit installs its inner filter. |
| AppArmor | Keep enforcement and review a dedicated profile for the verification container or an executable transition specific to Bubblewrap. Each added user namespace or mount permission increases the kernel operations available to processes covered by that rule. |
| User namespace policy or quota | Use a host that permits the required unprivileged user namespaces for Bubblewrap. Namespace permission gives its subjects access to additional kernel namespace code. A larger quota also permits more namespace resource consumption. Prefer a dedicated worker or VM if the current host policy cannot grant access at the needed scope. |

If the default deployment fails and no narrowly scoped policy passes the suite, run Wyrit on a separately verified Linux worker or VM and keep the Docker application as its client. Use the trusted host integration for authorized external calls. A Docker deployment that cannot establish the inner sandbox remains refused.
