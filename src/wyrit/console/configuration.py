"""Edit unresolved TOML and apply validated configuration to the live host."""

from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
import tomllib
from threading import Lock

from ..host_config import ConfigError, load_config, validate_config
from ..mcp_adapter import _contains_secret


def _literal(value):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False).replace(chr(127), r"\u007f")
    if isinstance(value, list):
        return "[" + ", ".join(map(_literal, value)) + "]"
    if type(value) in (bool, int, float):
        return json.dumps(value)
    raise ConfigError("TOML values must be strings, numbers, booleans, lists or tables.")


def _toml(data):
    lines = []

    def table(path, values):
        if path:
            lines.append("[" + ".".join(map(_literal, path)) + "]")
        for key, value in values.items():
            if not isinstance(value, dict):
                lines.append(f"{_literal(key)} = {_literal(value)}")
        lines.append("")
        for key, value in values.items():
            if isinstance(value, dict):
                table((*path, key), value)

    table((), data)
    return "\n".join(lines).lstrip("\n").encode()


def _stage(path, content):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".wyrit-", delete=False) as stream:
        staged = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            staged.unlink()
            raise
    return staged


class Configuration:
    def __init__(self, path, host, change_token):
        self.path = Path(path).resolve()
        self.host = host
        self.lock = Lock()
        self.secrets = {change_token}

    def _read(self):
        content = self.path.read_bytes()
        try:
            data = tomllib.loads(content.decode())
        except ValueError:
            raise ConfigError("Fix invalid TOML in the configuration file.") from None
        self._remember_secrets(data)
        return content, data

    def _remember_secrets(self, data):
        servers = data.get("servers")
        if not isinstance(servers, dict):
            return
        for server in servers.values():
            headers = server.get("headers") if isinstance(server, dict) else None
            if not isinstance(headers, dict):
                continue
            for reference in headers.values():
                env = reference.get("env") if isinstance(reference, dict) else None
                if isinstance(env, str) and os.environ.get(env):
                    self.secrets.add(os.environ[env])

    def state(self):
        content, data = self._read()
        config = load_config(self.path)
        inventory = self.host.inventory()
        servers = []
        for server in config.servers:
            settings = data["servers"][server.name]
            health, tools = inventory.get(server.name, (None, ()))
            definitions = {cap.adapter_metadata["tool_name"]: cap for cap in tools}
            names = sorted(definitions.keys() | set(server.expose or ()) | set(server.exclude))
            servers.append({
                "name": server.name,
                "settings": settings,
                "health": health.status if health else ("pending" if server.enabled else "disabled"),
                "reason": health.reason if health else None,
                "headers": {name: bool(os.environ.get(ref["env"]))
                            for name, ref in settings.get("headers", {}).items()},
                "tools": [
                    dict(definitions[name].to_dict() if name in definitions else {},
                         name=name, exposed=name not in server.exclude
                         and (server.expose is None or name in server.expose))
                    for name in names
                ],
            })
        return {
            "servers": servers,
            "limits": config.budgets().report(),
            "overrides": config.limits,
            "revision": sha256(content).hexdigest(),
        }

    def apply(self, request):
        original, data = self._read()
        action = request.get("action")
        if action == "reload":
            self.host.reload(self.path)
            return {"message": "Configuration reloaded."}
        revision = request.get("revision")
        if revision is not None and revision != sha256(original).hexdigest():
            raise ConfigError("The file changed since this page loaded. Reload the file and reopen the editor.")
        name = request.get("name")
        servers = data.setdefault("servers", {})
        if not isinstance(servers, dict):
            raise ConfigError("Fix the servers table in the configuration file.")
        if action == "limits":
            if name is None:
                data["limits"] = request.get("limits")
            else:
                self._server(servers, name)["limits"] = request.get("limits")
        elif action in {"save", "test"}:
            if not isinstance(name, str) or not name.strip():
                raise ConfigError("Enter a server name.")
            previous = request.get("previous")
            if name in servers and previous != name:
                raise ConfigError("A server with that name already exists.")
            if previous is not None and previous != name:
                self._server(servers, previous)
                del servers[previous]
            servers[name] = request.get("settings")
        else:
            server = self._server(servers, name)
            self._edit_server(action, request, name, servers, server)

        self._remember_secrets(data)
        if _contains_secret(data, self.secrets) or _contains_secret(original.decode(), self.secrets):
            raise ConfigError("Remove literal credentials and use environment references.")
        if action == "test":
            server = self._server(servers, name)
            if server.get("enabled") is False:
                server["enabled"] = True
        candidate = _stage(self.path, _toml(data))
        try:
            config = load_config(candidate)
            validate_config(config)
            if action == "test":
                server = next(server for server in config.servers if server.name == name)
                try:
                    tools = self.host.test_connection(server, config.secrets)
                except Exception:
                    raise ConfigError("Connection test failed. Check the transport, credentials and exposed tool names.") from None
                return {"message": f"Connection succeeded. Discovered {len(tools)} tools."}

            if self.path.read_bytes() != original:
                raise ConfigError("The file changed during the edit. Refresh and try again.")
            backup = self.path.with_name(self.path.name + ".bak")
            staged_backup = _stage(self.path, original)
            try:
                os.replace(staged_backup, backup)
                os.replace(candidate, self.path)
            finally:
                staged_backup.unlink(missing_ok=True)
            try:
                self.host.reload(config)
            except Exception:
                rollback = _stage(self.path, original)
                try:
                    os.replace(rollback, self.path)
                finally:
                    rollback.unlink(missing_ok=True)
                self.host.reload(self.path)
                raise
            return {"message": "Configuration saved."}
        finally:
            candidate.unlink(missing_ok=True)

    @staticmethod
    def _server(servers, name):
        if not isinstance(name, str) or not isinstance(servers.get(name), dict):
            raise ConfigError("Select an existing server.")
        return servers[name]

    def _edit_server(self, action, request, name, servers, server):
        if action == "remove":
            del servers[name]
        elif action == "enabled":
            server["enabled"] = request.get("enabled")
        elif action == "policy":
            tool = request.get("tool")
            denied = request.get("deny_writes")
            if tool is None:
                server["deny_writes"] = denied
            elif isinstance(tool, str):
                policies = server.setdefault("capabilities", {})
                policy = policies.setdefault(tool, {})
                if denied is None:
                    policy.pop("deny_writes", None)
                    if not policy:
                        del policies[tool]
                else:
                    policy["deny_writes"] = denied
            else:
                raise ConfigError("Select a capability.")
        elif action == "exposure":
            tool, exposed = request.get("tool"), request.get("exposed")
            if not isinstance(tool, str) or type(exposed) is not bool:
                raise ConfigError("Select a capability and whether to expose it.")
            current = server["expose"]
            excluded = set(server.get("exclude", []))
            if exposed:
                excluded.discard(tool)
                if current != "all":
                    server["expose"] = sorted(set(current) | {tool})
            else:
                excluded.add(tool)
            if excluded:
                server["exclude"] = sorted(excluded)
            else:
                server.pop("exclude", None)
        else:
            raise ConfigError("Choose a supported configuration action.")
