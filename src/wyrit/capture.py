"""Private, expiring call payloads shared by the local console and trusted hosts."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
from threading import Lock
import time
from uuid import uuid4

from .invocation import MESSAGE_BYTES, encode
from .ledger import default_ledger_path, open_ledger
from .mcp_adapter import _contains_secret


_CREDENTIAL = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"sk-[A-Za-z0-9_-]{16,}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|"
    r"""(?i:(?:token|password|api[_-]?key)\s*=\s*["'][^"']{16,}["']))"""
)


def contains_credential(value):
    if isinstance(value, str):
        return _CREDENTIAL.search(value) is not None
    if isinstance(value, dict):
        return any(contains_credential(key) or contains_credential(item) for key, item in value.items())
    if isinstance(value, list):
        return any(map(contains_credential, value))
    return False


def sidecar(ledger_path, suffix):
    path = Path(ledger_path) if ledger_path is not None else default_ledger_path()
    return path.with_name(path.name + suffix)


class CaptureStore:
    def __init__(self, ledger_path, clock=time.time):
        self.path = sidecar(ledger_path, ".capture.sqlite3")
        self.clock = clock

    @contextmanager
    def _database(self, create=False):
        if not create and not os.path.lexists(self.path):
            yield None
            return
        # open_ledger checks file type and refuses symlinks, with mode 0600.
        with open_ledger(self.path):
            db = sqlite3.connect(self.path, timeout=0.25)
            try:
                db.execute("PRAGMA secure_delete = ON")
                with db:
                    if create:
                        db.execute("""CREATE TABLE IF NOT EXISTS settings (
                            server TEXT PRIMARY KEY, seconds INTEGER, epoch TEXT)""")
                        db.execute("""CREATE TABLE IF NOT EXISTS payloads (
                            id INTEGER PRIMARY KEY, server TEXT, run TEXT,
                            created REAL, expires REAL, data BLOB)""")
                    yield db
            finally:
                db.close()

    def configure(self, server, seconds):
        if type(seconds) is not int or not 0 <= seconds <= 7 * 86400:
            raise ValueError("Capture retention must be between zero and seven days")
        server = json.dumps(server)
        with self._database(create=seconds > 0) as db:
            if db is None:
                return
            if seconds:
                db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?, ?)", (server, seconds, uuid4().hex))
                db.execute("UPDATE payloads SET expires = min(expires, created + ?) WHERE server = ?",
                           (seconds, server))
                self._purge(db)
            else:
                db.execute("DELETE FROM settings WHERE server = ?", (server,))
                db.execute("DELETE FROM payloads WHERE server = ?", (server,))

    def setting(self, server):
        return self.policies().get(tuple(server))

    def policies(self):
        with self._database() as db:
            if db is not None:
                return {tuple(json.loads(key)): (seconds, epoch)
                        for key, seconds, epoch in db.execute("SELECT server, seconds, epoch FROM settings")}
        return {}

    def save(self, server, run, setting, data, secrets, *, created=None):
        if setting is None or _contains_secret(data, secrets) or contains_credential(data):
            return
        payload = encode(data)
        if len(payload) > 3 * MESSAGE_BYTES:
            return
        seconds, epoch = setting
        server = json.dumps(server)
        now = self.clock()
        created = now if created is None else created
        expires = created + seconds
        if expires <= now:
            return
        with self._database() as db:
            if db is not None:
                db.execute("""INSERT INTO payloads (server, run, created, expires, data)
                              SELECT ?, ?, ?, ?, ? FROM settings WHERE server = ? AND epoch = ?""",
                           (server, run, created, expires, payload, server, epoch))

    def _purge(self, db):
        db.execute("DELETE FROM payloads WHERE expires <= ?", (self.clock(),))

    def purge(self):
        with self._database() as db:
            if db is not None:
                self._purge(db)

    def settings(self):
        return {key: seconds for key, (seconds, _) in self.policies().items()}

    def payloads(self, run, servers):
        keys = {json.dumps(server) for server in servers}
        with self._database() as db:
            if db is None:
                return []
            rows = db.execute("""SELECT server, data FROM payloads WHERE run = ? AND expires > ?
                                 ORDER BY id""", (run, self.clock()))
            return [json.loads(payload) for server, payload in rows if server in keys]


class RunCapture:
    """Buffer only opted-in calls and write after the runtime stops."""

    def __init__(self, run_id, source):
        self.run_id = run_id
        self.source = source
        self.policies = {}
        self.pending = []
        self.lock = Lock()

    def start(self, ledger_path):
        self.store = CaptureStore(ledger_path)
        try:
            self.policies = self.store.policies()
        except (OSError, sqlite3.Error, ValueError):
            pass

    def sent(self, key, capability, args, secrets):
        setting = self.policies.get(key)
        if setting is None:
            return None
        with self.lock:
            if self.pending is not None:
                data = {
                    "server": key[0], "capability": capability,
                    "arguments": args, "source": self.source,
                }
                self.pending.append((key, setting, data, secrets, self.store.clock()))
                return data
        return None

    def completed(self, captured, result):
        with self.lock:
            if self.pending is not None:
                captured["result"] = result

    def finish(self):
        with self.lock:
            pending, self.pending = self.pending, None
        for key, setting, data, secrets, created in pending:
            try:
                self.store.save(key, self.run_id, setting, data, secrets, created=created)
            except (OSError, sqlite3.Error, ValueError, RecursionError):
                pass
