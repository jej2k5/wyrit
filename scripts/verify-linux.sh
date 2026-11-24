#!/bin/sh
set -eu
cd "$(dirname "$0")/.."

case "${1:-}" in
    ''|--readiness-only) ;;
    *) echo 'Usage: verify-linux.sh [--readiness-only]' >&2; exit 2 ;;
esac

refuse() {
    printf 'Linux verification refused: %s\n' "$1" >&2
    printf '%s\n' 'Containment probes: NOT RUN (prerequisites failed).' >&2
    exit 1
}

[ "$(uname -s)" = Linux ] || refuse 'a Linux host is required'
command -v bwrap >/dev/null 2>&1 && [ -x /usr/bin/bwrap ] ||
    refuse 'Bubblewrap is required at /usr/bin/bwrap and on PATH'
[ -x /usr/bin/prlimit ] || refuse 'prlimit is required at /usr/bin/prlimit'
command -v node >/dev/null 2>&1 || refuse 'Node.js is required'
command -v python3 >/dev/null 2>&1 || refuse 'python3 is required'
[ "$(id -u)" -ne 0 ] || refuse 'run as a non-root user to verify unprivileged namespaces'

# Exercise the real launcher because AppArmor can grant userns per executable.
if ! WYRIT_ENABLE_LINUX_SANDBOX=1 PYTHONPATH=tests python3 - <<'PY'
import shutil
import subprocess
import sys

from native_support import run_native
from src.wyrit.native_sandbox import SandboxUnavailable

try:
    result = run_native(shutil.which("node"), 'process.stdout.write("sandbox-ready")')
except SandboxUnavailable as error:
    sys.exit(str(error))
except (TimeoutError, subprocess.TimeoutExpired):
    sys.exit("Bubblewrap readiness check timed out")
if result.returncode or result.stdout != "sandbox-ready":
    sys.exit(f"Bubblewrap readiness failed (exit {result.returncode}): {result.stderr.strip()}")
PY
then
    refuse 'the required Bubblewrap isolation, including unprivileged user namespaces, could not be established'
fi
printf '%s\n' 'Containment probe readiness: PASS'
[ "${1:-}" != --readiness-only ] || exit 0

log=$(mktemp)
trap 'rm -f "$log"' EXIT
status=0
WYRIT_ENABLE_LINUX_SANDBOX=1 WYRIT_REQUIRE_NATIVE_SANDBOX=1 \
    python3 -m unittest discover -s tests -v >"$log" 2>&1 || status=$?
cat "$log"

printf '\nContainment probe results\n'
report() {
    result=$(sed -n "s/^$1 (test_native_sandbox.NativeSandboxTests\.$1) \.\.\. //p" "$log")
    printf '  %s: %s\n' "$2" "${result:-NO RESULT}"
}
report_ambient() {
    result=$(sed -n "s/^Containment probe $1: //p" "$log")
    printf '  %s: %s\n' "$2" "${result:-NO RESULT}"
}
report_ambient read 'Host file read'
report_ambient write 'Host file write'
report_ambient env 'Host environment'
report_ambient spawn 'Process spawn'
report_ambient tcp 'Host TCP'
report_ambient ipc 'Host Unix IPC'
report test_linux_data_limit_survives_exec 'Linux hard and soft data segment limits'
exit "$status"
