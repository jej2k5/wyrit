# Phase 18: Linux verification

Make the Bubblewrap backend verifiable on a real Linux host. Add `scripts/verify-linux.sh` that checks prerequisites (bwrap present, unprivileged user namespaces allowed), runs `WYRIT_REQUIRE_NATIVE_SANDBOX=1 python3 -m unittest discover -s tests`, and prints a summary of the containment probes. Add a GitHub Actions workflow (`.github/workflows/linux-sandbox.yml`, manual trigger plus pull requests touching `src/wyrit/native_sandbox.py`) that runs it on an Ubuntu runner, with read-only permissions and no secrets.

Fix what the Linux probes reveal within the security model: no widening of the sandbox to make a probe pass. If a guarantee cannot be enforced on a host, the backend refuses to run there (fail closed) and says why. Update the guarantees in `docs/HANDOFF_PHASE_15.md`: either Linux is verified with the evidence, or the remaining gap is listed probe by probe and Linux stays refused.

The gate runs on macOS inside a sandbox and cannot run Bubblewrap, so tests here cover the script's prerequisite checks and the backend's fail-closed refusals with mocks. Produce the phase handoff with the operator steps.
