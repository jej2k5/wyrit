"""Append-only local run metadata."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat


def default_ledger_path():
    state = os.environ.get("XDG_STATE_HOME")
    root = Path(state) if state and Path(state).is_absolute() else Path.home() / ".local" / "state"
    return root / "wyrit" / "runs.jsonl"


@contextmanager
def open_ledger(path):
    path = Path(path) if path is not None else default_ledger_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("Run ledger must be a regular file")
        os.fchmod(fd, 0o600)
        yield fd
    finally:
        os.close(fd)


def append_record(fd, record):
    remaining = memoryview(json.dumps(record, separators=(",", ":")).encode() + b"\n")
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        offset = os.lseek(fd, 0, os.SEEK_END)
        try:
            while remaining:
                written = os.write(fd, remaining)
                if not written:
                    raise OSError("Run ledger write made no progress")
                remaining = remaining[written:]
        except OSError:
            os.ftruncate(fd, offset)
            raise
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)


def read_ledger(path=None):
    """Yield records in append order. An absent ledger contains no records."""
    try:
        stream = (Path(path) if path is not None else default_ledger_path()).open("rb")
    except FileNotFoundError:
        return
    with stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
        try:
            end = os.fstat(stream.fileno()).st_size
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        while stream.tell() < end:
            yield json.loads(stream.readline(end - stream.tell()))
