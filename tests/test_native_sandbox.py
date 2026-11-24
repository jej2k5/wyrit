from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from native_support import require_native, run_native
from src.wyrit.capabilities import RunAuthorization
from src.wyrit.native_sandbox import (
    LINUX_DATA_BYTES, SandboxUnavailable, _export_linux_filter, _file_chain, _mac_profile,
    native_command,
)
from src.wyrit.runtime import run_javascript


class NativeSandboxTests(unittest.TestCase):
    def test_verifier_refuses_wrong_platform_and_missing_bubblewrap(self):
        script = Path(__file__).resolve().parents[1] / "scripts/verify-linux.sh"
        with tempfile.TemporaryDirectory() as directory:
            tools = Path(directory)
            (tools / "dirname").symlink_to(shutil.which("dirname"))
            uname = tools / "uname"
            uname.write_text('#!/bin/sh\nprintf "%s\\n" "$FAKE_KERNEL"\n')
            uname.chmod(0o755)
            for kernel, reason in (
                ("Darwin", "a Linux host is required"),
                ("Linux", "Bubblewrap is required"),
            ):
                with self.subTest(kernel=kernel):
                    result = subprocess.run(
                        ["/bin/sh", str(script)], env={"PATH": directory, "FAKE_KERNEL": kernel},
                        capture_output=True, text=True, timeout=5,
                    )
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(reason, result.stderr)
                    self.assertIn("Containment probes: NOT RUN", result.stderr)

    def test_linux_without_opt_in_never_launches_node(self):
        with patch.dict(os.environ), patch(
            "src.wyrit.native_sandbox.sys.platform", "linux",
        ), patch("src.wyrit.native_sandbox.platform.machine", return_value="x86_64"), patch(
            "src.wyrit.runtime.shutil.which", return_value="/node",
        ), patch("src.wyrit.runtime.subprocess.Popen") as launch:
            os.environ.pop("WYRIT_ENABLE_LINUX_SANDBOX", None)
            result = run_javascript("return 1;", run_id="native",
                                    authorization=RunAuthorization("native", set()))
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.reason, "Linux native sandbox is not verified for this environment")
        launch.assert_not_called()

    def test_linux_refuses_setuid_launcher_before_inspecting_node(self):
        with patch.dict(os.environ, {"WYRIT_ENABLE_LINUX_SANDBOX": "1"}), patch(
            "src.wyrit.native_sandbox.sys.platform", "linux",
        ), patch(
            "src.wyrit.native_sandbox.platform.machine", return_value="x86_64",
        ), patch("src.wyrit.native_sandbox.os.access", return_value=True), patch(
            "src.wyrit.native_sandbox.os.stat", return_value=os.stat_result(
                (stat.S_ISUID, 0, 0, 0, 0, 0, 0, 0, 0, 0),
            ),
        ), patch("src.wyrit.native_sandbox._linux_runtime_files") as inspect:
            with self.assertRaisesRegex(SandboxUnavailable, "non-setuid Bubblewrap"):
                with native_command("/node", [], deadline=0):
                    self.fail("unexpected launch command")
        inspect.assert_not_called()

    def test_linux_refuses_missing_seccomp_library(self):
        with patch("src.wyrit.native_sandbox.ctypes.CDLL", side_effect=OSError):
            with self.assertRaisesRegex(SandboxUnavailable, "libseccomp.so.2 is required"):
                _export_linux_filter(-1)

    def test_linux_namespace_rejection_reports_reason_without_running_guest(self):
        launch = subprocess.Popen

        @contextmanager
        def rejected(*args, **kwargs):
            yield ["/usr/bin/prlimit"], ()

        def reject(command, **kwargs):
            self.assertEqual(command, ["/usr/bin/prlimit"])
            return launch([sys.executable, "-c",
                           'import sys; sys.exit("bwrap: Creating new namespace failed: Operation not permitted")'],
                          **kwargs)

        with patch("src.wyrit.runtime.native_command", rejected), patch(
            "src.wyrit.runtime.shutil.which", return_value="/node",
        ), patch("src.wyrit.runtime.subprocess.Popen", side_effect=reject):
            result = run_javascript('return await tools.call("fake.echo", 1);', run_id="native",
                                    authorization=RunAuthorization("native", {"fake.echo"}))
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.reason, "Bubblewrap could not establish required Linux namespaces")
        self.assertEqual(result.actions, ())

    def test_macos_profile_limits_sysctl_reads_to_kernel_names_and_hardware_prefixes(self):
        with patch("src.wyrit.native_sandbox._mac_runtime_files", return_value={"/node"}), patch(
            "src.wyrit.native_sandbox._file_chain", return_value=("/node",),
        ):
            profile = _mac_profile("/node", deadline=0)

        self.assertIn("(deny default)", profile.splitlines())
        self.assertEqual(
            [line for line in profile.splitlines() if "(allow sysctl-read" in line],
            ['(allow sysctl-read (sysctl-name "kern.osrelease" "kern.osversion" "kern.ostype" "kern.version" "kern.hostname") (sysctl-name-prefix "hw.") (sysctl-name-prefix "machdep.cpu."))'],
        )

    def test_missing_runtime_fails_closed(self):
        with patch("src.wyrit.runtime.shutil.which", return_value=None):
            result = run_javascript("return 1;", run_id="native",
                                    authorization=RunAuthorization("native", set()))
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.reason, "Node.js unavailable")

    def test_unsupported_platform_never_launches_node(self):
        with patch("src.wyrit.native_sandbox.sys.platform", "unsupported"), patch(
            "src.wyrit.runtime.shutil.which", return_value="/node",
        ), patch("src.wyrit.runtime.subprocess.Popen") as launch:
            result = run_javascript("return 1;", run_id="native",
                                    authorization=RunAuthorization("native", set()))
        self.assertEqual(result.status, "FAILED")
        self.assertIn("No supported native sandbox", result.reason)
        launch.assert_not_called()

    def test_launcher_rejection_never_runs_guest(self):
        @contextmanager
        def rejected(*args, **kwargs):
            yield [sys.executable, "-c", "raise SystemExit(1)"], ()

        with patch("src.wyrit.runtime.native_command", rejected), patch(
            "src.wyrit.runtime.shutil.which", return_value="/node",
        ):
            result = run_javascript('return await tools.call("fake.echo", 1);', run_id="native",
                                    authorization=RunAuthorization("native", {"fake.echo"}))
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.actions, ())

    def test_runtime_symlink_resolution_keeps_intermediate_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cellar = root / "Cellar"
            cellar.mkdir()
            (root / "opt").symlink_to(cellar)
            (cellar / "lib.so").write_text("fixture")
            (cellar / "lib.1.so").symlink_to("lib.so")
            self.assertEqual(_file_chain(str(root / "opt/lib.1.so")), tuple(map(str, (
                root / "opt/lib.1.so", cellar / "lib.1.so", cellar / "lib.so",
            ))))

    def test_native_policy_denies_ambient_authority(self):
        require_native(self)
        node = shutil.which("node")
        # Live endpoints make absence of a listener distinguishable from a denial.
        with tempfile.TemporaryDirectory(dir=Path.home()) as home, tempfile.TemporaryDirectory(
            dir="/tmp",
        ) as temp, socket.socket() as tcp, socket.socket(socket.AF_UNIX) as ipc:
            secret = Path(home, "credential")
            secret.write_text("filesystem-credential-canary")
            output = Path(temp, "must-not-exist")
            address = str(Path(temp, "ipc"))
            tcp.bind(("127.0.0.1", 0))
            tcp.listen()
            ipc.bind(address)
            ipc.listen()
            values = json.dumps([str(secret), str(output), tcp.getsockname()[1], address])
            script = """
                const fs = require("node:fs");
                const net = require("node:net");
                const {spawnSync} = require("node:child_process");
                const [secret, output, port, path] = """ + values + """;
                function attempt(operation) {
                    try { operation(); return "ALLOWED"; }
                    catch (error) { return error.code; }
                }
                function connect(options) {
                    return new Promise(resolve => {
                        const connection = net.createConnection(options);
                        connection.once("error", error => resolve(error.code));
                        connection.once("connect", () => {
                            connection.destroy();
                            resolve("ALLOWED");
                        });
                    });
                }
                (async () => {
                    const executable = process.platform === "linux" ? "/node" : process.execPath;
                    const spawn = spawnSync(executable, ["-e", "0"]);
                    console.log(JSON.stringify({
                        read: attempt(() => fs.readFileSync(secret)),
                        write: attempt(() => fs.writeFileSync(output, "unexpected")),
                        spawn: spawn.error?.code ?? "ALLOWED",
                        tcp: await connect({host: "127.0.0.1", port}),
                        ipc: await connect({path}),
                        env: process.env,
                    }));
                })().catch(error => { console.error(error); process.exitCode = 1; });
            """
            with patch.dict(os.environ, {"WYRIT_TEST_CREDENTIAL": "environment-canary"}):
                # This trusted probe has no Node permission flags. It exercises native policy.
                result = run_native(node, script)
            self.assertEqual(result.returncode, 0, result.stderr)
            observed = json.loads(result.stdout)
            denied = {"EPERM", "EACCES"}
            outcomes = {
                "read": observed["read"] in denied | {"ENOENT"},
                "write": observed["write"] in denied | {"ENOENT", "EROFS"} and not output.exists(),
                # Bubblewrap re-sets PWD to the sandbox root after --clearenv; nothing else may appear.
                "env": observed["env"] in ({}, {"PWD": "/"}),
                "spawn": observed["spawn"] in denied,
                "tcp": observed["tcp"] in denied,
                "ipc": observed["ipc"] in denied | {"ENOENT"},
            }
            for probe, passed in outcomes.items():
                print(f"\nContainment probe {probe}: {'PASS' if passed else 'FAIL'}", file=sys.stderr)
                with self.subTest(probe=probe):
                    self.assertTrue(passed, "Native policy did not produce the expected denial")

    @unittest.skipUnless(sys.platform == "linux", "Linux-only data limit")
    def test_linux_data_limit_survives_exec(self):
        require_native(self)
        node = shutil.which("node")
        script = """
            const limits = process.report.getReport().userLimits;
            console.log(JSON.stringify(limits.data_seg_size_bytes ?? limits.data_seg_size_kbytes));
        """
        result = run_native(node, script)
        self.assertEqual(result.returncode, 0, result.stderr)
        limit = LINUX_DATA_BYTES
        self.assertEqual(json.loads(result.stdout), {"soft": limit, "hard": limit})
