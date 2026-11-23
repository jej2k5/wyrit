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


DEFAULT_SOURCE_SECONDS = 7 * 86400


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
                        db.execute("""CREATE TABLE IF NOT EXISTS source_settings (
                            server TEXT PRIMARY KEY, seconds INTEGER, epoch TEXT)""")
                        db.execute("""CREATE TABLE IF NOT EXISTS sources (
                            run TEXT PRIMARY KEY, created REAL, expires REAL, source TEXT, servers TEXT)""")
                        db.execute("CREATE INDEX IF NOT EXISTS sources_expiry ON sources (expires)")
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

    def configure_source(self, server, seconds):
        if type(seconds) is not int or not 0 <= seconds <= DEFAULT_SOURCE_SECONDS:
            raise ValueError("Source retention must be between zero and seven days")
        server = json.dumps(server)
        with self._database(create=True) as db:
            db.execute("INSERT OR REPLACE INTO source_settings VALUES (?, ?, ?)", (server, seconds, uuid4().hex))
            scope = "WHERE EXISTS (SELECT 1 FROM json_each(sources.servers) WHERE value = ?)"
            if seconds:
                db.execute("UPDATE sources SET expires = min(expires, created + ?) " + scope, (seconds, server))
            else:
                db.execute("DELETE FROM sources " + scope, (server,))
                db.execute("""UPDATE payloads
                              SET data = CAST(json_remove(CAST(data AS TEXT), '$.source') AS BLOB)
                              WHERE server = ? AND json_type(CAST(data AS TEXT), '$.source') IS NOT NULL""",
                           (server,))
            self._purge(db)

    @staticmethod
    def _has_sources(db):
        return db.execute("SELECT 1 FROM sqlite_master WHERE name = 'sources' AND type = 'table'").fetchone()

    def source_policies(self):
        with self._database() as db:
            if db is not None and self._has_sources(db):
                return {tuple(json.loads(key)): (seconds, epoch)
                        for key, seconds, epoch in db.execute("SELECT server, seconds, epoch FROM source_settings")}
        return {}

    def save_source(self, run, source, servers, policies):
        default = (DEFAULT_SOURCE_SECONDS, None)
        with self._database(create=True) as db:
            db.execute("BEGIN IMMEDIATE")
            retention = DEFAULT_SOURCE_SECONDS
            for server in servers:
                policy = db.execute("SELECT seconds, epoch FROM source_settings WHERE server = ?",
                                    (json.dumps(server),)).fetchone() or default
                if policy != policies.get(server, default) or not policy[0]:
                    return
                retention = min(retention, policy[0])
            now = self.clock()
            db.execute("INSERT INTO sources VALUES (?, ?, ?, ?, ?)",
                       (run, now, now + retention, source, json.dumps(sorted(map(json.dumps, servers)))))

    def sources(self, runs, *, preview=False):
        if not runs:
            return {}
        now = self.clock()
        result = {}
        with self._database() as db:
            if db is None or not self._has_sources(db):
                return result
            column = "substr(source, 1, 240)" if preview else "source"
            for start in range(0, len(runs), 500):
                batch = runs[start:start + 500]
                slots = ", ".join("?" for _ in batch)
                for run, source, expires in db.execute(
                        f"SELECT run, {column}, expires FROM sources WHERE expires > ? AND run IN ({slots})",
                        (now, *batch)):
                    first_line = source.partition("\n")[0].removesuffix("\r")
                    result[run] = {
                        "text": first_line if preview else source,
                        "remaining_seconds": expires - now,
                    }
        return result

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
        now = self.clock()
        db.execute("DELETE FROM payloads WHERE expires <= ?", (now,))
        if self._has_sources(db):
            db.execute("DELETE FROM sources WHERE expires <= ?", (now,))

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
            payloads = []
            for server, payload in rows:
                if server in keys:
                    data = json.loads(payload)
                    data.pop("source", None)
                    if data:
                        payloads.append(data)
            return payloads


class RunCapture:
    """Keep source separate from opted-in call payloads."""

    def __init__(self, run_id, source):
        self.run_id = run_id
        self.source = source
        self.policies = {}
        self.source_policies = {}
        self.routes = {}
        self.current_secrets = lambda: ()
        self.secrets = set()
        self.pending = []
        self.lock = Lock()

    def start(self, ledger_path):
        self.store = CaptureStore(ledger_path)
        try:
            self.policies = self.store.policies()
            self.source_policies = self.store.source_policies()
        except (OSError, sqlite3.Error, ValueError):
            pass

    def sent(self, key, capability, args, secrets):
        with self.lock:
            if self.pending is None:
                return None
            self.secrets.update(secrets)
            setting = self.policies.get(key)
            if setting is not None:
                data = {
                    "server": key[0], "capability": capability,
                    "arguments": args,
                }
                self.pending.append((key, setting, data, self.store.clock()))
                return data
        return None

    def completed(self, captured, result):
        with self.lock:
            if self.pending is not None:
                captured["result"] = result

    def finish(self, actions=()):
        with self.lock:
            pending, self.pending = self.pending, None
        secrets = self.secrets.union(self.current_secrets())
        if _contains_secret(self.source, secrets) or contains_credential(self.source):
            return "withheld"
        for key, setting, data, created in pending:
            try:
                self.store.save(key, self.run_id, setting, data, secrets, created=created)
            except (OSError, sqlite3.Error, ValueError, RecursionError):
                pass
        try:
            if len(self.source.encode()) <= MESSAGE_BYTES:
                servers = {self.routes[action["capability"]] for action in actions
                           if action.get("capability") in self.routes}
                self.store.save_source(self.run_id, self.source, servers, self.source_policies)
        except (OSError, sqlite3.Error, ValueError):
            pass
        return None
