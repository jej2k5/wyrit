"""Build native launch commands for a trusted Node installation."""

from contextlib import contextmanager
import ctypes
import errno
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys
import tempfile
import time


class SandboxUnavailable(Exception):
    pass


MAC_LAUNCHER = "/usr/bin/sandbox-exec"
LINUX_LAUNCHER = "/usr/bin/bwrap"
LINUX_DATA_BYTES = 512 * 1024 * 1024
_MACHO_COMMANDS = {}


def _inspect(command, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    try:
        return subprocess.check_output(
            command, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, cwd="/",
            stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True, timeout=remaining,
        )
    except subprocess.TimeoutExpired as error:
        raise TimeoutError from error
    except (OSError, subprocess.CalledProcessError, UnicodeError) as error:
        raise SandboxUnavailable("Could not inspect trusted Node installation") from error


def _file_chain(path):
    paths = [os.path.normpath(path)]
    for _ in range(40):
        parent, name = os.path.split(path)
        path = os.path.join(os.path.realpath(parent, strict=True), name)
        paths.append(path)
        mode = os.lstat(path).st_mode
        if stat.S_ISREG(mode):
            return tuple(dict.fromkeys(paths))
        if not stat.S_ISLNK(mode):
            break
        path = os.path.join(os.path.dirname(path), os.readlink(path))
    raise SandboxUnavailable("Invalid runtime file symlink chain")


def _mac_runtime_files(node, deadline):
    files = set(_file_chain("/usr/lib/dyld"))
    executable = _file_chain(node)[-1]
    pending = [(node, ())]
    visited = set()
    while pending:
        path, inherited = pending.pop()
        if path.startswith(("/usr/lib/", "/System/Library/")):
            # Apple system libraries can reside solely in the dyld shared cache.
            files.update(_file_chain(path) if os.path.exists(path) else (path,))
            continue
        chain = _file_chain(path)
        files.update(chain)
        image = chain[-1]
        if image in visited:
            continue
        visited.add(image)
        if len(visited) > 256:
            raise SandboxUnavailable("Too many Node runtime dependencies")
        identity = os.stat(image)
        key = (image, identity.st_dev, identity.st_ino, identity.st_mtime_ns,
               identity.st_ctime_ns, identity.st_size)
        commands = _MACHO_COMMANDS.get(key)
        if commands is None:
            commands = _inspect(["/usr/bin/otool", "-l", image], deadline)
            if len(_MACHO_COMMANDS) >= 256:
                _MACHO_COMMANDS.clear()
            _MACHO_COMMANDS[key] = commands

        def expand(value):
            return (value.replace("@loader_path", os.path.dirname(image))
                    .replace("@executable_path", os.path.dirname(executable)))

        rpaths = tuple(expand(value) for value in re.findall(
            r"cmd LC_RPATH\n\s+cmdsize \d+\n\s+path (.+) \(offset \d+\)", commands,
        )) + inherited
        for value in re.findall(
            r"cmd LC_(?:LOAD|LOAD_WEAK|REEXPORT|LOAD_UPWARD|LAZY_LOAD)_DYLIB\n"
            r"\s+cmdsize \d+\n\s+name (.+) \(offset \d+\)", commands,
        ):
            path = expand(value)
            if path.startswith("@rpath/"):
                candidates = (os.path.join(directory, path[7:]) for directory in rpaths)
                path = next((candidate for candidate in candidates if os.path.isfile(candidate)), "")
            if not os.path.isabs(path):
                raise SandboxUnavailable("Could not resolve Node runtime library")
            pending.append((os.path.normpath(path), rpaths))
    return files


def _literals(paths):
    return " ".join(f"(literal {json.dumps(path, ensure_ascii=False)})" for path in sorted(paths))


def _mac_profile(node, deadline):
    files = _mac_runtime_files(node, deadline)
    ancestors = {"/", "/dev"}
    for path in files:
        ancestors.update(str(parent) for parent in Path(path).parents)
    return "\n".join((
        "(version 1)",
        "(deny default)",
        f"(allow process-exec {_literals([_file_chain(node)[-1]])})",
        f"(allow file-read* file-map-executable {_literals(files)})",
        f"(allow file-read-metadata {_literals(ancestors)})",
        '(allow file-read-data (literal "/") (literal "/dev/null")'
        ' (literal "/dev/urandom") (literal "/dev/random"))',
        '(allow sysctl-read (sysctl-name "kern.osrelease" "kern.osversion" "kern.ostype" "kern.version" "kern.hostname") (sysctl-name-prefix "hw.") (sysctl-name-prefix "machdep.cpu."))',
    ))


def _linux_runtime_files(node, deadline):
    output = _inspect(["/usr/bin/ldd", node], deadline)
    files = {}
    interpreter = None
    for line in output.splitlines():
        entry = line.strip().split(" => ")[-1].rsplit(" (0x", 1)[0]
        if entry.startswith("/"):
            files[entry] = _file_chain(entry)[-1]
            if " => " not in line and os.path.basename(entry).startswith("ld-linux-"):
                interpreter = entry
        elif not entry.startswith("linux-vdso.so"):
            raise SandboxUnavailable("Could not resolve Node ELF dependency")
    if interpreter is None:
        raise SandboxUnavailable("A glibc Node ELF interpreter is required")
    return interpreter, files


class _ArgComparison(ctypes.Structure):
    _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_int),
                ("mask", ctypes.c_uint64), ("value", ctypes.c_uint64)]


def _export_linux_filter(fd):
    try:
        seccomp = ctypes.CDLL("libseccomp.so.2")
    except OSError as error:
        raise SandboxUnavailable("libseccomp.so.2 is required for Linux syscall isolation") from error
    init = seccomp.seccomp_init
    init.argtypes = [ctypes.c_uint32]
    init.restype = ctypes.c_void_p
    release = seccomp.seccomp_release
    release.argtypes = [ctypes.c_void_p]
    release.restype = None
    resolve = seccomp.seccomp_syscall_resolve_name
    resolve.argtypes = [ctypes.c_char_p]
    rule = seccomp.seccomp_rule_add_array
    rule.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint,
                     ctypes.POINTER(_ArgComparison)]
    export = seccomp.seccomp_export_bpf
    export.argtypes = [ctypes.c_void_p, ctypes.c_int]
    context = init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not context:
        raise SandboxUnavailable("Could not initialize Linux syscall policy")

    def deny(name, error=errno.EPERM, comparison=None):
        number = resolve(name.encode("ascii"))
        if number < 0 or rule(context, 0x00050000 | error, number,
                              int(comparison is not None), comparison) != 0:
            raise SandboxUnavailable(f"Could not restrict Linux syscall {name}")

    try:
        for name in (
            "socket", "socketpair", "unshare", "setns", "ptrace", "process_vm_readv",
            "process_vm_writev", "pidfd_getfd", "io_uring_setup", "io_uring_enter",
            "io_uring_register", "msgget", "semget", "shmget", "mq_open", "keyctl",
            "add_key", "request_key",
        ):
            deny(name)
        if platform.machine() == "x86_64":
            deny("fork")
            deny("vfork")
        deny("clone3", error=errno.ENOSYS)  # libc falls back to clone for threads.
        deny("clone", comparison=_ArgComparison(0, 7, 0x10000, 0))  # MASKED_EQ, !CLONE_THREAD
        if export(context, fd) != 0:
            raise SandboxUnavailable("Could not export Linux syscall policy")
    finally:
        release(context)


@contextmanager
def native_command(node, arguments, *, deadline):
    """Yield a sandbox command and the descriptors needed by its launcher."""
    try:
        node = os.path.abspath(node)
        if sys.platform == "darwin" and os.access(MAC_LAUNCHER, os.X_OK):
            yield [MAC_LAUNCHER, "-p", _mac_profile(node, deadline), os.path.realpath(node),
                   "--openssl-config=/dev/null", *arguments], ()
            return
        if sys.platform == "linux" and platform.machine() in {"x86_64", "aarch64"}:
            if os.environ.get("WYRIT_ENABLE_LINUX_SANDBOX") != "1":
                raise SandboxUnavailable("Linux native sandbox is not verified for this environment")
            if not all(os.access(path, os.X_OK) for path in (LINUX_LAUNCHER, "/usr/bin/prlimit")):
                raise SandboxUnavailable("Bubblewrap and prlimit are required")
            if os.stat(LINUX_LAUNCHER).st_mode & stat.S_ISUID:
                raise SandboxUnavailable("A non-setuid Bubblewrap is required for Linux user namespace isolation")
            interpreter, files = _linux_runtime_files(os.path.realpath(node), deadline)
            libraries = ":".join(sorted({os.path.dirname(path) for path in files}))
            with tempfile.TemporaryFile() as policy:
                _export_linux_filter(policy.fileno())
                policy.seek(0)
                command = [
                    "/usr/bin/prlimit", f"--data={LINUX_DATA_BYTES}:{LINUX_DATA_BYTES}",
                    "--", LINUX_LAUNCHER, "--unshare-user", "--unshare-ipc", "--unshare-pid",
                    "--unshare-net", "--unshare-uts", "--hostname", "wyrit", "--disable-userns",
                    "--assert-userns-disabled", "--die-with-parent", "--new-session",
                    "--clearenv", "--cap-drop", "ALL", "--ro-bind", os.path.realpath(node), "/node",
                    "--dev-bind", "/dev/null", "/dev/null",
                ]
                for target, source in sorted(files.items()):
                    command.extend(("--ro-bind", source, target))
                command.extend(("--remount-ro", "/", "--chdir", "/", "--seccomp",
                                str(policy.fileno()), "--", interpreter, "--library-path",
                                # No JIT: newer kernels count V8's code-range reservation against
                                # the data limit, and guest scripts never need generated code.
                                libraries, "/node", "--jitless", *arguments))
                yield command, (policy.fileno(),)
            return
    except TimeoutError:
        raise
    except OSError as error:
        raise SandboxUnavailable("Could not prepare or launch native sandbox") from error
    raise SandboxUnavailable("No supported native sandbox available")
