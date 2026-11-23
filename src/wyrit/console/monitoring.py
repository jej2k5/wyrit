"""Incremental, read-only projections of trusted host records."""

from collections import Counter, defaultdict
import fcntl
import json
import math
import os
from pathlib import Path
import stat
import time
from uuid import uuid4

from ..capture import sidecar


WINDOWS = {"hour": 3600, "day": 86400, "week": 604800}
OUTCOMES = ("SUCCEEDED", "FAILED", "DENIED", "UNKNOWN")


class _Segment:
    def __init__(self, stream):
        self.stream = stream
        self.cursor = 0
        self.line = 0
        self.anchor = b""

    def read(self):
        stream = self.stream
        fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
        try:
            end = os.fstat(stream.fileno()).st_size
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.seek(max(0, self.cursor - len(self.anchor)))
        if end < self.cursor or stream.read(len(self.anchor)) != self.anchor:
            self.cursor, self.line, self.anchor = 0, 0, b""
        stream.seek(self.cursor)
        while self.cursor < end:
            raw = stream.readline(end - self.cursor)
            if not raw.endswith(b"\n"):
                break
            self.cursor += len(raw)
            self.line += 1
            self.anchor = (self.anchor + raw)[-128:]
            try:
                record = json.loads(raw)
            except (ValueError, RecursionError):
                continue
            if isinstance(record, dict):
                yield self.line, record


class LedgerTail:
    def __init__(self, path, prefix):
        self.path = Path(path)
        self.prefix = prefix
        self.segments = {}
        self.rows = []

    def poll(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            fd = None
        if fd is not None:
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise OSError("Monitoring requires a regular ledger")
                identity = (info.st_dev, info.st_ino)
                if identity not in self.segments:
                    self.segments[identity] = _Segment(os.fdopen(os.dup(fd), "rb"))
        for segment in self.segments.values():
            for line, record in segment.read():
                self.rows.append({
                    "ref": f"{self.prefix}{len(self.rows) + 1}",
                    "file": self.path.name,
                    "inode": os.fstat(segment.stream.fileno()).st_ino,
                    "line": line,
                    "record": record,
                })
        return self.rows

    def close(self):
        for segment in self.segments.values():
            segment.stream.close()


def percentile(values, fraction):
    """Nearest rank on observed durations, including single-sample windows."""
    return sorted(values)[math.ceil(fraction * len(values)) - 1] if values else None


def summarize(runs, history, host, now, window):
    selected = [row for row in runs if now - WINDOWS[window] < row["record"]["ended_at"] <= now]
    hour = [row for row in runs if now - 3600 < row["record"]["ended_at"] <= now]
    baseline = [row for row in runs if now - 86400 < row["record"]["ended_at"] <= now - 3600]
    latest = {}
    servers = defaultdict(lambda: {"history": [], "reconnects": [], "drops": []})
    for row in history:
        event = row["record"]
        name = event["server"]
        if event["host"] == host:
            latest[name] = row
        if not now - WINDOWS[window] < event["at"] <= now:
            continue
        server = servers[name]
        server["history"].append(row)
        if event.get("reconnect") is True:
            server["reconnects"].append(row["ref"])
    active = {name: row for name, row in latest.items() if row["record"]["status"] != "removed"}
    connected = [row for row in active.values() if row["record"]["status"] == "connected"]
    attention = [{"label": f"{name} is down", "refs": [row["ref"]]}
                 for name, row in active.items() if row["record"]["status"] == "down"]

    def tally(rows):
        counts = Counter()
        refs = defaultdict(set)
        for row in rows:
            for action in row["record"]["actions"]:
                outcome = action["outcome"]
                counts[outcome] += 1
                refs[outcome].add(row["ref"])
        return counts, refs

    current, current_refs = tally(hour)
    recent, recent_refs = tally(baseline)
    for outcome in ("FAILED", "DENIED"):
        if (current[outcome] and sum(recent.values())
                and current[outcome] * sum(recent.values()) > recent[outcome] * sum(current.values())):
            attention.append({
                "label": f"{outcome} action rate above the previous 23 hours",
                "refs": sorted(set().union(*current_refs.values(), *recent_refs.values())),
            })

    capabilities = defaultdict(list)
    limits, denials = [], []
    for row in selected:
        run, ref = row["record"], row["ref"]
        hits = run.get("limit_hits")
        if hits is None:
            hits = [run["limit_hit"]] if run.get("limit_hit") is not None else []
        limits.extend({"ref": ref, **hit} for hit in hits)
        for index, action in enumerate(run["actions"], 1):
            capability = action.get("capability")
            if capability is not None:
                capabilities[capability].append((ref, action))
            detail = {"ref": ref, "call": action.get("call_id", index), "capability": capability}
            if action.get("reason") == "WRITE_POLICY" and action["outcome"] == "DENIED":
                denials.append(detail)
            if action.get("reason") == "CONNECTION_DROP" and action["outcome"] == "UNKNOWN":
                if action.get("server") is not None:
                    servers[action["server"]]["drops"].append(detail)
    stats = []
    for capability, actions in sorted(capabilities.items()):
        counts = Counter(action["outcome"] for _, action in actions)
        durations = [action["duration_seconds"] for _, action in actions
                     if action.get("duration_seconds") is not None]
        stats.append({
            "capability": capability, "calls": len(actions),
            "rates": {outcome: counts[outcome] / len(actions) for outcome in OUTCOMES},
            "p50": percentile(durations, 0.50), "p95": percentile(durations, 0.95),
            "refs": list(dict.fromkeys(ref for ref, _ in actions)),
        })
    return {
        "health": {
            "connected": len(connected),
            "exposed": sum(row["record"]["capabilities"] for row in connected),
            "refs": [row["ref"] for row in active.values()],
            "runs": len(hour), "run_refs": [row["ref"] for row in hour], "attention": attention,
        },
        "capabilities": stats, "servers": servers, "limits": limits, "denials": denials,
    }


class Monitoring:
    def __init__(self, path):
        self.epoch = uuid4().hex
        self.path = path
        self.runs = LedgerTail(path, "r")
        self.health = LedgerTail(sidecar(path, ".health.jsonl"), "h")

    def close(self):
        self.runs.close()
        self.health.close()

    def snapshot(self, host, window, after, epoch, *, paused=False, now=None):
        runs = self.runs.poll()
        if epoch != self.epoch or after > len(runs):
            after = max(0, len(runs) - 50)
        return {
            **summarize(runs, self.health.poll(), host, time.time() if now is None else now, window),
            "epoch": self.epoch,
            # Initial view is bounded. Older records remain reachable by their source references.
            "feed": [] if paused else sorted(
                reversed(runs[after:]), key=lambda row: row["record"]["ended_at"], reverse=True,
            ),
            "cursor": len(runs),
        }

    def source(self, ref):
        tail = self.runs if ref.startswith("r") else self.health
        rows = tail.poll()
        index = int(ref[1:]) - 1
        return rows[index] if 0 <= index < len(rows) else None
