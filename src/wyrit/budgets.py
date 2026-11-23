"""Trusted execution limits and their configuration provenance."""

from dataclasses import asdict, dataclass, field


DEFAULTS = {
    "run_deadline": 10.0,
    "call_timeout": 5.0,
    "max_calls": 32,
    "max_output_bytes": 16 * 1024,
}


@dataclass(frozen=True)
class Limit:
    value: int | float
    source: str


@dataclass(frozen=True)
class RunBudgets:
    global_limits: dict = field(default_factory=dict)
    server_limits: dict = field(default_factory=dict)

    def effective(self, namespace=None):
        overrides = self.server_limits.get(namespace, {})
        return {
            key: Limit(overrides[key], "server") if key in overrides else
            Limit(self.global_limits[key], "global") if key in self.global_limits else
            Limit(default, "default")
            for key, default in DEFAULTS.items()
        }

    def report(self):
        def values(namespace=None):
            return {name: asdict(limit) for name, limit in self.effective(namespace).items()}

        return {"global": values(), "servers": {name: values(name) for name in self.server_limits}}


class LimitExceeded(Exception):
    def __init__(self, name, limit, reason, namespace=None):
        super().__init__(reason)
        self.detail = {"name": name, **asdict(limit)}
        if namespace is not None:
            self.detail["namespace"] = namespace
