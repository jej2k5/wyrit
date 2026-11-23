"""Load and validate the trusted host configuration without opening transports."""

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit

from .budgets import DEFAULTS, RunBudgets
from .capabilities import _ID
from .mcp_adapter import _TOOL_NAME


_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class ConfigError(ValueError):
    """An operator configuration error safe to report at startup."""


@dataclass(frozen=True)
class ServerConfig:
    name: str
    namespace: str
    transport: str
    expose: tuple[str, ...] | None
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    headers: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    limits: dict = field(default_factory=dict, compare=False)
    deny_writes: bool = True
    capabilities: dict = field(default_factory=dict)


@dataclass(frozen=True)
class HostConfig:
    servers: tuple[ServerConfig, ...]
    secrets: frozenset[str] = field(repr=False)
    limits: dict = field(default_factory=dict)
    ledger_path: Path | None = None

    def budgets(self):
        return RunBudgets(dict(self.limits), {s.namespace: dict(s.limits) for s in self.servers})


def _keys(table, allowed, location):
    if not isinstance(table, dict):
        raise ConfigError(f"{location}: expected a table")
    for key in table.keys() - allowed:
        raise ConfigError(f"{location}.{key!r}: unknown field")


def _limits(table, location):
    _keys(table, DEFAULTS.keys(), location)
    for key, value in table.items():
        valid_type = type(value) is int or (type(value) is float and type(DEFAULTS[key]) is float)
        if not valid_type or not 0 < value <= 2**53 - 1:
            raise ConfigError(f"{location}.{key}: expected a positive {type(DEFAULTS[key]).__name__} limit")
    return dict(table)


def _ledger_path(value):
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ConfigError("config.ledger_path: expected a nonempty path")
    try:
        return Path(value).expanduser()
    except (OSError, RuntimeError):
        raise ConfigError("config.ledger_path: could not expand path") from None


def _valid_url(value):
    if not isinstance(value, str) or re.search(r"[\s\\\x00-\x1f\x7f]|%(?![0-9a-fA-F]{2})", value):
        return False
    try:
        url = urlsplit(value)
        authority = r"(?:\[[^\]]+\]|[^:\[\]@/?#%]+)(?::[0-9]+)?"
        return (
            url.scheme in {"http", "https"} and bool(url.hostname)
            and url.username is None and url.password is None and "#" not in value
            and re.fullmatch(authority, url.netloc) is not None and url.port != 0
        )
    except ValueError:
        return False


def _require_https_for_headers(url, headers, location):
    if headers and urlsplit(url).scheme != "https":
        raise ConfigError(f"{location}.headers: credential headers require HTTPS")


def _headers(table, location, environ, secrets):
    if not isinstance(table, dict):
        raise ConfigError(f"{location}: expected a table")
    headers = {}
    names = set()
    for name, reference in table.items():
        if not _HEADER_NAME.fullmatch(name):
            raise ConfigError(f"{location}: invalid HTTP header name")
        header = f"{location}.{name}"
        if name.lower() in names:
            raise ConfigError(f"{header}: duplicate HTTP header")
        names.add(name.lower())
        if isinstance(reference, str):
            raise ConfigError(f"{header}: use an environment reference for the header value")
        _keys(reference, {"env", "prefix"}, header)
        env = reference.get("env")
        if not isinstance(env, str) or not _ENV_NAME.fullmatch(env):
            raise ConfigError(f"{header}.env: expected an environment variable name")
        secret = environ.get(env)
        if not secret:
            raise ConfigError(f"{header}.env: missing or empty environment variable {env}")
        prefix = reference.get("prefix", "")
        if not isinstance(prefix, str) or not re.fullmatch(r"[ -~]*", prefix):
            raise ConfigError(f"{header}.prefix: expected printable ASCII text")
        value = prefix + secret
        if not re.fullmatch(r"[ -~]+", value) or value != value.strip():
            raise ConfigError(f"{header}.env: invalid HTTP header value")
        headers[name] = value
        secrets.add(secret)
    return tuple(headers.items())


def _write_policy(table, location, expose):
    deny_writes = table.get("deny_writes", True)
    if type(deny_writes) is not bool:
        raise ConfigError(f"{location}.deny_writes: expected a boolean")
    policies = table.get("capabilities", {})
    if not isinstance(policies, dict):
        raise ConfigError(f"{location}.capabilities: expected a table")
    for name, policy in policies.items():
        if (not isinstance(name, str) or not _TOOL_NAME.fullmatch(name)
                or (expose is not None and name not in expose)):
            raise ConfigError(f"{location}.capabilities: expected an exposed MCP tool name")
        tool = f"{location}.capabilities.{name!r}"
        _keys(policy, {"classification", "deny_writes"}, tool)
        if "classification" in policy and policy["classification"] not in ("read", "write"):
            raise ConfigError(f"{tool}.classification: expected 'read' or 'write'")
        if "deny_writes" in policy and type(policy["deny_writes"]) is not bool:
            raise ConfigError(f"{tool}.deny_writes: expected a boolean")
    return {
        "deny_writes": deny_writes,
        "capabilities": {name: dict(policy) for name, policy in policies.items()},
    }


def _server(name, table, environ, secrets):
    location = f"servers.{name!r}"
    common = {"namespace", "transport", "expose", "limits", "deny_writes", "capabilities"}
    _keys(table, common | {"command", "args", "url", "headers"}, location)
    limits = _limits(table.get("limits", {}), f"{location}.limits")
    namespace = table.get("namespace")
    if not isinstance(namespace, str) or not _ID.fullmatch(namespace):
        raise ConfigError(f"{location}.namespace: expected a capability prefix")
    transport = table.get("transport")
    expose = table.get("expose")
    if expose == "all":
        expose = None
    elif (
        isinstance(expose, list)
        and all(isinstance(tool, str) and _TOOL_NAME.fullmatch(tool) for tool in expose)
        and len(set(expose)) == len(expose)
    ):
        expose = tuple(expose)
    else:
        raise ConfigError(f"{location}.expose: expected 'all' or a list of unique tool names")

    policy = _write_policy(table, location, expose)
    if transport == "stdio":
        _keys(table, common | {"command", "args"}, location)
        command = table.get("command")
        if not isinstance(command, str) or not command.strip() or "\0" in command:
            raise ConfigError(f"{location}.command: expected a nonempty executable name")
        args = table.get("args", [])
        if not isinstance(args, list) or any(not isinstance(arg, str) or "\0" in arg for arg in args):
            raise ConfigError(f"{location}.args: expected a list of argument strings")
        return ServerConfig(name, namespace, transport, expose, command=command,
                            args=tuple(args), limits=limits, **policy)
    if transport == "http":
        _keys(table, common | {"url", "headers"}, location)
        url = table.get("url")
        if not _valid_url(url):
            raise ConfigError(f"{location}.url: expected a valid HTTP(S) URL without userinfo or a fragment")
        header_table = table.get("headers", {})
        _require_https_for_headers(url, header_table, location)
        headers = _headers(header_table, f"{location}.headers", environ, secrets)
        return ServerConfig(name, namespace, transport, expose, url=url,
                            headers=headers, limits=limits, **policy)
    raise ConfigError(f"{location}.transport: expected 'stdio' or 'http'")


def load_config(path) -> HostConfig:
    """Read TOML and resolve header references using the current environment."""
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except OSError:
        raise ConfigError("config: could not read the TOML file") from None
    except ValueError:
        raise ConfigError("config: invalid TOML") from None
    _keys(data, {"servers", "limits", "ledger_path"}, "config")
    limits = _limits(data.get("limits", {}), "config.limits")
    path_value = data.get("ledger_path")
    ledger_path = Path(path).absolute().parent / _ledger_path(path_value) if path_value is not None else None
    tables = data.get("servers", {})
    if not isinstance(tables, dict):
        raise ConfigError("config.servers: expected a table")
    environ = dict(os.environ)
    secrets = set()
    namespaces = set()
    servers = []
    for name, table in tables.items():
        server = _server(name, table, environ, secrets)
        if server.namespace in namespaces:
            raise ConfigError(f"servers.{name!r}.namespace: duplicate namespace")
        namespaces.add(server.namespace)
        servers.append(server)
    return HostConfig(tuple(servers), frozenset(secrets), limits, ledger_path)


def validate_config(config):
    """Validate a resolved configuration before it can change live sessions."""
    if (not isinstance(config, HostConfig) or not isinstance(config.servers, tuple)
            or not isinstance(config.secrets, frozenset)
            or any(not isinstance(secret, str) or not secret for secret in config.secrets)):
        raise ConfigError("config: expected a resolved HostConfig")
    _limits(config.limits, "config.limits")
    if config.ledger_path is not None:
        if not isinstance(config.ledger_path, Path):
            raise ConfigError("config.ledger_path: expected a resolved path")
        _ledger_path(str(config.ledger_path))
    names = set()
    namespaces = set()
    for server in config.servers:
        if (not isinstance(server, ServerConfig) or not isinstance(server.name, str)
                or not isinstance(server.namespace, str)
                or not isinstance(server.args, tuple) or not isinstance(server.headers, tuple)
                or (server.expose is not None and not isinstance(server.expose, tuple))):
            raise ConfigError("config.servers: invalid resolved server")
        if server.name in names or server.namespace in namespaces:
            raise ConfigError("config.servers: duplicate server name or namespace")
        names.add(server.name)
        namespaces.add(server.namespace)
        table = {
            "namespace": server.namespace, "transport": server.transport, "limits": server.limits,
            "deny_writes": server.deny_writes, "capabilities": server.capabilities,
            "expose": "all" if server.expose is None else list(server.expose),
        }
        if server.transport == "stdio" and server.url is None and not server.headers:
            table.update(command=server.command, args=list(server.args))
        elif server.transport == "http" and server.command is None and not server.args:
            table["url"] = server.url
            header_names = set()
            for header in server.headers:
                if not isinstance(header, tuple) or len(header) != 2:
                    raise ConfigError("config.servers: invalid resolved HTTP header")
                name, value = header
                if (not isinstance(name, str) or not _HEADER_NAME.fullmatch(name)
                        or name.lower() in header_names or not isinstance(value, str)
                        or not re.fullmatch(r"[ -~]+", value) or value != value.strip()
                        or not any(value.endswith(secret) for secret in config.secrets)):
                    raise ConfigError("config.servers: invalid resolved HTTP header")
                header_names.add(name.lower())
        else:
            raise ConfigError("config.servers: invalid resolved transport")
        _server(server.name, table, {}, set())
        if server.transport == "http":
            _require_https_for_headers(server.url, server.headers, f"servers.{server.name!r}")
