"""Own configured downstream transports and sessions in the trusted host."""

from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, replace
from datetime import timedelta
from os import PathLike
import subprocess
from sqlite3 import Error as StorageError
from threading import Lock
import time
from uuid import uuid4

import anyio
from anyio.from_thread import start_blocking_portal
import httpx
from mcp import ClientSession, types
from mcp.client import streamable_http
from mcp.client.stdio import StdioServerParameters, stdio_client

from .budgets import RunBudgets
from .capabilities import CapabilityRegistry, _TOKEN
from .capture import CaptureStore, sidecar
from .host_config import ConfigError, ServerConfig, load_config, validate_config
from .invocation import CallError
from .ledger import append_record, default_ledger_path, open_ledger
from .mcp_adapter import MCPAdapter, _contains_secret
from .recipes import Recipes


async def _check_http_response(response):
    # HTTPX runs response hooks before either HTTPX or MCP can follow a redirect.
    if response.has_redirect_location:
        raise httpx.TransportError("Downstream HTTP redirects are disabled")
    if response.request.method == "POST" and response.status_code == 404:
        raise httpx.TransportError("Downstream HTTP endpoint or session is unavailable")


@asynccontextmanager
async def _http_streams(server):
    async with httpx.AsyncClient(
        follow_redirects=False, trust_env=False, timeout=httpx.Timeout(30, read=300),
        event_hooks={"response": [_check_http_response]},
    ) as client:
        headers = dict(server.headers)
        if hasattr(streamable_http, "streamable_http_client"):
            client.headers.update(headers)
            async with streamable_http.streamable_http_client(server.url, http_client=client) as streams:
                yield streams[:2]
            return

        transport = streamable_http.StreamableHTTPTransport(
            server.url, headers, timedelta(seconds=30), timedelta(seconds=300),
        )
        incoming, read = anyio.create_memory_object_stream(0)
        write, outgoing = anyio.create_memory_object_stream(0)
        async with incoming, read, write, outgoing, anyio.create_task_group() as tasks:
            def start_get():
                tasks.start_soon(transport.handle_get_stream, client, incoming)

            tasks.start_soon(transport.post_writer, client, outgoing, incoming, write, start_get)
            try:
                yield read, write
            finally:
                with anyio.move_on_after(2, shield=True):
                    await transport.terminate_session(client)
                tasks.cancel_scope.cancel()


class _Connection:
    """Make EOF visible to the supervisor and outstanding requests."""

    def __init__(self, read):
        self.read = read
        self.closed = False
        self.changed = anyio.Event()
        self.pending = set()

    async def __aenter__(self):
        await self.read.__aenter__()
        return self

    async def __aexit__(self, *exc):
        self.close()
        return await self.read.__aexit__(*exc)

    async def __aiter__(self):
        try:
            async for message in self.read:
                yield message
        finally:
            self.close()

    def close(self):
        self.closed = True
        self.changed.set()
        for scope in self.pending:
            scope.cancel()

    async def incoming(self, message):
        if isinstance(message, types.ServerNotification):
            if isinstance(message.root, types.ToolListChangedNotification):
                self.changed.set()
        elif isinstance(message, httpx.HTTPError):
            self.close()

    async def send_request(self, *args):
        return await self.request(self.session.send_request, *args)

    async def request(self, send, *args):
        if self.closed:
            raise CallError("DISPATCH_UNAVAILABLE", dispatched=False)
        with anyio.CancelScope() as scope:
            self.pending.add(scope)
            try:
                result = await send(*args)
                if not self.closed:
                    return result
            finally:
                self.pending.remove(scope)
        raise CallError("OUTCOME_UNKNOWN", audit_reason="CONNECTION_DROP")


@asynccontextmanager
async def _session(server):
    if server.transport == "stdio":
        transport = stdio_client(
            StdioServerParameters(command=server.command, args=list(server.args)),
            errlog=subprocess.DEVNULL,
        )
    else:
        transport = _http_streams(server)
    async with transport as (read, write):
        connection = _Connection(read)
        async with ClientSession(connection, write, message_handler=connection.incoming) as session:
            connection.session = session
            try:
                with anyio.fail_after(5):
                    await connection.request(session.initialize)
                yield connection
            finally:
                connection.close()


@dataclass(frozen=True)
class ServerHealth:
    status: str
    reason: str | None
    last_change: float


class _Server:
    def __init__(self, config: ServerConfig):
        self.config = replace(config, capabilities={
            name: dict(policy) for name, policy in config.capabilities.items()
        })
        self.health = ServerHealth("connecting", None, time.time())
        self.adapter = None
        self.generation = 0
        self.known = None
        self.discovered = ()
        self.session_id = uuid4().hex
        self.health_record = None


class ConfiguredHost:
    """Supervise configured sessions inside the trusted host's portal."""

    def __init__(self, portal):
        self._portal = portal
        self._lock = Lock()
        self._reload_lock = Lock()
        self._servers = {}
        self._secrets = frozenset()
        self._closed = False
        self._budgets = RunBudgets()
        self._ledger_path = None
        self._capture_paths = set()
        self._recipes = Recipes()
        self.monitor_id = uuid4().hex
        self.health_record_error = False

    @property
    def ledger_path(self):
        with self._lock:
            return self._ledger_path or default_ledger_path()

    def _purge_captures(self):
        with self._lock:
            paths = tuple(self._capture_paths)
        for path in paths:
            try:
                CaptureStore(path).purge()
            except (OSError, StorageError):
                pass

    def _record_health(self, slot, *, removed=False):
        name = slot.config.name
        if _contains_secret([name, slot.config.namespace], self._secrets):
            return
        record = {
            "host": self.monitor_id, "session": slot.session_id, "server": name,
            "namespace": slot.config.namespace, "status": "removed" if removed else slot.health.status,
            "reason": None if removed else slot.health.reason, "generation": slot.generation,
            "capabilities": len(self._safe_capabilities(slot.known.values())) if slot.adapter else 0,
        }
        if record == slot.health_record:
            return
        reconnect = (record["status"] == "connected" and slot.generation > 1
                     and slot.health_record is not None
                     and slot.health_record["generation"] != slot.generation)
        try:
            with open_ledger(sidecar(self._ledger_path, ".health.jsonl")) as ledger:
                append_record(ledger, {**record, "at": time.time(), "reconnect": reconnect})
        except OSError:
            self.health_record_error = True
        else:
            slot.health_record = record

    @property
    def health(self):
        with self._lock:
            return {name: slot.health for name, slot in self._servers.items()}

    def _update(self, slot, status, reason=None, adapter=None):
        with self._lock:
            if (status, reason) != (slot.health.status, slot.health.reason):
                slot.health = ServerHealth(status, reason, time.time())
            if adapter is not None:
                if slot.adapter is None:
                    slot.generation += 1
                slot.known = {cap.id: cap for cap in adapter.capabilities}
                slot.discovered = adapter.discovered
            slot.adapter = adapter
            if self._servers.get(slot.config.name) is slot:
                self._record_health(slot)

    async def _refresh(self, slot, connection, ready):
        server = slot.config
        connection_secrets = self._secrets | frozenset(value for _, value in server.headers)
        while not connection.closed:
            connection.changed = anyio.Event()
            adapter = await MCPAdapter.discover(
                server.namespace, connection, self._portal, exposed_tools=server.selected_tools,
                excluded_tools=server.exclude,
                secrets=lambda: connection_secrets | self._secrets, require_all=slot.known is None,
                deny_writes=server.deny_writes, capability_policies=server.capabilities,
            )
            if connection.closed:
                break
            self._update(slot, "connected", adapter=adapter)
            ready.set()
            while not connection.changed.is_set():
                if server.transport == "stdio":
                    await connection.changed.wait()
                    break
                # Stateless HTTP may have no persistent stream to report EOF.
                with anyio.move_on_after(5) as wait:
                    await connection.changed.wait()
                if wait.cancel_called:
                    with anyio.fail_after(2):
                        await connection.send_request(
                            types.ClientRequest(types.PingRequest()), types.EmptyResult,
                        )

    async def _supervise(self, slot, *, task_status):
        ready = anyio.Event()
        with anyio.CancelScope() as scope:
            task_status.started((scope, ready))
            delay = 0.25
            while True:
                reason = "Downstream connection failed"
                try:
                    async with _session(slot.config) as connection:
                        reason = "Downstream tool discovery failed"
                        try:
                            await self._refresh(slot, connection, ready)
                        finally:
                            connected = slot.health.status == "connected"
                            if connected:
                                reason = "Downstream session lost or tool refresh failed"
                                delay = 0.25
                            self._update(slot, "down", reason)
                except Exception:
                    # Transport and SDK exceptions can contain credentials.
                    pass
                finally:
                    self._update(slot, "down", reason)
                    ready.set()
                await anyio.sleep(delay)
                delay = min(2, delay * 2)
                self._update(slot, "connecting", reason)

    def reload(self, config):
        """Apply a validated HostConfig or load and validate a TOML path."""
        if isinstance(config, (str, PathLike)):
            config = load_config(config)
        validate_config(config)
        with self._reload_lock:
            if self._closed:
                raise RuntimeError("Configured host is closed")
            with self._lock:
                old = self._servers
                new = {}
                for server in config.servers:
                    if not server.enabled:
                        continue
                    slot = old.get(server.name)
                    new[server.name] = slot if slot and slot.config == server else _Server(server)
                self._servers = new
                self._secrets = config.secrets
                if self._recipes.directory != config.recipes_path:
                    self._recipes = Recipes(config.recipes_path)
                self._budgets = config.budgets()
                moved = self._ledger_path != config.ledger_path
                for name, slot in old.items():
                    if moved or new.get(name) is not slot:
                        self._record_health(slot, removed=True)
                self._ledger_path = config.ledger_path
                self._capture_paths.add(config.ledger_path or default_ledger_path())
                for slot in new.values():
                    if moved:
                        slot.health_record = None
                    self._record_health(slot)
            self._stop(slot for name, slot in old.items() if new.get(name) is not slot)
            ready = []
            for name, slot in new.items():
                if old.get(name) is not slot:
                    slot.task, (slot.scope, started) = self._portal.start_task(self._supervise, slot)
                    ready.append(started)
            for started in ready:
                self._portal.call(started.wait)
            self.recipes()

    def inventory(self):
        with self._lock:
            return {
                name: (slot.health, self._safe_capabilities(slot.discovered))
                for name, slot in self._servers.items()
            }

    def test_connection(self, server, secrets):
        async def discover():
            with anyio.fail_after(15):
                async with _session(server) as connection:
                    adapter = await MCPAdapter.discover(
                        server.namespace, connection, self._portal, exposed_tools=server.selected_tools,
                        excluded_tools=server.exclude,
                        secrets=lambda: secrets | self._secrets | {value for _, value in server.headers},
                        require_all=True, deny_writes=server.deny_writes,
                        capability_policies=server.capabilities,
                    )
                    return adapter.discovered

        return self._portal.call(discover)

    def _stop(self, slots):
        slots = tuple(slots)
        for slot in slots:
            self._portal.call(slot.scope.cancel)
        for slot in slots:
            slot.task.result()

    def close(self):
        with self._reload_lock:
            with self._lock:
                slots = tuple(self._servers.values())
                for slot in slots:
                    self._record_health(slot, removed=True)
                self._servers = {}
                self._closed = True
            self._stop(slots)

    def _safe_capabilities(self, capabilities):
        if not self._secrets:
            return capabilities
        return tuple(capability for capability in capabilities
                     if not _contains_secret(capability.to_dict(), self._secrets))

    def _routes(self):
        return {
            capability.id: (capability, slot, slot.adapter, slot.generation)
            for slot in self._servers.values() if slot.adapter is not None
            for capability in self._safe_capabilities(slot.known.values())
        }

    @property
    def registry(self):
        with self._lock:
            return CapabilityRegistry(route[0] for route in self._routes().values())

    def catalog(self, allowed):
        """Snapshot the agent-visible descriptors and effective write policy."""
        with self._lock:
            catalog = []
            for key, (capability, slot, _, _) in sorted(self._routes().items()):
                if allowed is not None and key not in allowed:
                    continue
                denied = False
                if capability.classification == "write":
                    name = capability.adapter_metadata["tool_name"]
                    policy = slot.config.capabilities.get(name, {})
                    denied = policy.get("deny_writes", slot.config.deny_writes)
                catalog.append((capability, denied))
            return tuple(catalog)

    @property
    def recipes_enabled(self):
        with self._lock:
            return self._recipes.directory is not None

    @staticmethod
    def _recipe_capabilities(routes, allowed=None):
        return {key: route[0] for key, route in routes.items()
                if allowed is None or key in allowed}

    def recipes(self, allowed=None, *, details=False):
        with self._lock:
            capabilities = self._recipe_capabilities(self._routes(), allowed)
            return self._recipes.entries(capabilities, self._secrets, details=details)

    def propose_recipe(self, name, description, input_schema, source, allowed):
        with self._lock:
            capabilities = self._recipe_capabilities(self._routes(), allowed)
            return self._recipes.propose(name, description, input_schema, source,
                                         capabilities, self._secrets)

    def change_recipe(self, action, name, revision, operator, console_secrets):
        with self._lock:
            capabilities = self._recipe_capabilities(self._routes())
            self._recipes.change(action, name, revision, operator, capabilities,
                                 self._secrets | console_secrets)

    @property
    def ids(self):
        with self._lock:
            return tuple(self._routes())

    def search(self, query, *, limit=5, allowed=None):
        with self._lock:
            routes = self._routes()
            visible = CapabilityRegistry(route[0] for key, route in routes.items()
                                         if allowed is None or key in allowed)
            terms = set(_TOKEN.findall(query.casefold()))
            unavailable = []
            for name, slot in self._servers.items():
                if slot.adapter is not None or allowed is not None:
                    continue
                namespace = slot.config.namespace
                if _contains_secret([name, namespace], self._secrets):
                    continue
                known = CapabilityRegistry(self._safe_capabilities((slot.known or {}).values()))
                words = set(_TOKEN.findall(f"{name.casefold()} {namespace}"))
                if slot.config.expose is not None:
                    words.update(_TOKEN.findall(" ".join(slot.config.expose).casefold()))
                if terms & words or known.search(query, limit=1):
                    unavailable.append({
                        "server": name, "namespace": namespace, "status": slot.health.status,
                        "reason": slot.health.reason, "last_change": slot.health.last_change,
                    })
        matches = [visible.describe(match["id"]) for match in visible.search(query, limit=limit)]
        return matches, unavailable[:limit]

    def dispatch(self, capability_id, args, *, deadline):
        _, dispatch = self.bind_run()
        return dispatch(capability_id, args, deadline=deadline)

    def bind_run(self, *, include_settings=False, capture_run=None,
                 recipe_name=None, allowed=None):
        """Pin a run to the capabilities and connections present at admission."""
        with self._lock:
            routes = self._routes()
            budgets, ledger_path = self._budgets, self._ledger_path
            secrets = self._secrets
            if recipe_name is not None:
                capabilities = self._recipe_capabilities(routes, allowed)
                recipe = self._recipes.get(recipe_name, capabilities, secrets)
                if recipe is None:
                    raise ConfigError("Recipe does not exist.")
                if recipe["state"] != "runnable":
                    reason = recipe["reason"] or "Approval is required."
                    raise ConfigError(f"Recipe is {recipe['state']}. {reason}")
                if capture_run is not None:
                    capture_run.source = recipe["source"]
            if capture_run is not None:
                capture_run.routes = {
                    name: (slot.config.name, slot.config.namespace)
                    for name, (_, slot, _, _) in routes.items()
                }
                capture_run.secrets.update(secrets)
                capture_run.current_secrets = lambda: self._secrets

        def dispatch(capability_id, args, *, deadline, on_send=None):
            if capability_id not in routes:
                raise CallError("DENIED", dispatched=False)
            capability, slot, adapter, generation = routes[capability_id]
            key = (slot.config.name, slot.config.namespace)
            captured = None

            def admit():
                nonlocal captured
                with self._lock:
                    if (self._servers.get(slot.config.name) is not slot or slot.adapter is None
                            or slot.generation != generation or slot.known.get(capability_id) != capability
                            or not self._safe_capabilities((capability,))):
                        raise CallError("DISPATCH_UNAVAILABLE", dispatched=False)
                    if on_send is not None:
                        on_send()
                    if capture_run is not None:
                        captured = capture_run.sent(key, capability_id, args, secrets | self._secrets)

            try:
                result = adapter.dispatch(capability_id, args, deadline=deadline, admit=admit)
            except CallError as error:
                if error.audit_reason == "CONNECTION_DROP":
                    error.audit_server = slot.config.name
                raise
            if captured is not None:
                capture_run.completed(captured, result)
            return result

        if recipe_name is not None:
            return tuple(recipe['capabilities']), dispatch, budgets, ledger_path, recipe
        if include_settings:
            return tuple(routes), dispatch, budgets, ledger_path
        return tuple(routes), dispatch


@contextmanager
def configured_host(config):
    with start_blocking_portal() as portal:
        host = ConfiguredHost(portal)

        async def expire_capture():
            while True:
                await anyio.to_thread.run_sync(host._purge_captures)
                await anyio.sleep(1)

        expiry = None
        try:
            host.reload(config)
            expiry = portal.start_task_soon(expire_capture)
            yield host
        finally:
            if expiry is not None:
                expiry.cancel()
            host.close()


@contextmanager
def configured_capabilities(config):
    """Yield an initial registry snapshot and the live dispatcher."""
    with configured_host(config) as host:
        yield host.registry, host.dispatch
