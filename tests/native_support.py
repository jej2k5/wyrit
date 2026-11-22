"""Detect hosts where integration tests can exercise a real native sandbox."""

from functools import lru_cache
import os
import shutil
import subprocess
import sys
import time

from src.wyrit.native_sandbox import LINUX_LAUNCHER, MAC_LAUNCHER, native_command


def run_native(node, script):
    with native_command(node, ["-e", script], deadline=time.monotonic() + 10) as (command, fds):
        return subprocess.run(command, env={}, cwd="/", capture_output=True, text=True,
                              timeout=10, pass_fds=fds)


@lru_cache(maxsize=1)
def _unsupported_reason():
    node = shutil.which("node")
    if node is None:
        return "Node.js unavailable"
    if sys.platform == "linux" and not os.access(LINUX_LAUNCHER, os.X_OK):
        return "Bubblewrap unavailable"
    if sys.platform not in {"darwin", "linux"}:
        return "No supported native sandbox"
    if sys.platform == "darwin" and os.access(MAC_LAUNCHER, os.X_OK):
        check = subprocess.run(
            [MAC_LAUNCHER, "-p", '(version 1)(deny default)'
             '(allow process-exec (literal "/usr/bin/true"))'
             '(allow file-read* file-map-executable (literal "/usr/bin/true"))', "/usr/bin/true"],
            env={}, capture_output=True, text=True, timeout=5,
        )
        if check.returncode and "sandbox_apply: Operation not permitted" in check.stderr:
            return "Host refused nested sandbox_apply"
        # After a successful sandbox_apply the minimal profile may still block startup.
    check = run_native(node, 'process.stdout.write("sandbox-ready")')
    if check.returncode and sys.platform == "linux" and (
        "Operation not permitted" in check.stderr and check.stderr.startswith("bwrap:")
    ):
        return "Host refused Bubblewrap setup"
    if check.returncode or check.stdout != "sandbox-ready":
        raise AssertionError(f"Sandboxed Node failed to start ({check.returncode}): {check.stderr}")
    return None


def require_native(test):
    reason = _unsupported_reason()
    if reason:
        if os.environ.get("WYRIT_REQUIRE_NATIVE_SANDBOX") == "1":
            test.fail(reason)
        test.skipTest(reason)
