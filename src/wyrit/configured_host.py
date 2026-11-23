"""Own configured downstream transports and sessions in the trusted host."""

from contextlib import ExitStack, asynccontextmanager, contextmanager
from datetime import timedelta
import subprocess

import anyio
from anyio.from_thread import start_blocking_portal
import httpx
from mcp import ClientSession
from mcp.client import streamable_http
from mcp.client.stdio import StdioServerParameters, stdio_client

from .capabilities import CapabilityRegistry
from .host_config import ConfigError
from .invocation import CallError
from .mcp_adapter import MCPAdapter


async def _reject_redirect(response):
    # HTTPX runs response hooks before either HTTPX or MCP can follow a redirect.
    if response.has_redirect_location:
        raise httpx.TransportError("Downstream HTTP redirects are disabled")


@asynccontextmanager
async def _http_streams(server):
    async with httpx.AsyncClient(
        follow_redirects=False, trust_env=False, timeout=httpx.Timeout(30, read=300),
        event_hooks={"response": [_reject_redirect]},
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


@asynccontextmanager
async def _session(server):
    if server.transport == "stdio":
        transport = stdio_client(
            StdioServerParameters(command=server.command, args=list(server.args)),
            errlog=subprocess.DEVNULL,
        )
    else:
        transport = _http_streams(server)
    async with transport as (read, write), ClientSession(read, write) as session:
        with anyio.fail_after(5):
            await session.initialize()
        yield session


@contextmanager
def configured_capabilities(config):
    """Yield a registry and dispatcher while all configured sessions are open."""
    with ExitStack() as stack:
        portal = stack.enter_context(start_blocking_portal())
        capabilities = []
        dispatchers = {}
        for server in config.servers:
            try:
                session = stack.enter_context(portal.wrap_async_context_manager(_session(server)))
                adapter = MCPAdapter(
                    server.namespace, session, portal,
                    exposed_tools=server.expose, secrets=config.secrets,
                )
            except Exception:
                raise ConfigError(
                    f"servers.{server.name!r}.transport/expose: downstream connection or discovery failed"
                ) from None
            for capability in adapter.capabilities:
                capabilities.append(capability)
                dispatchers[capability.id] = adapter.dispatch

        def dispatch(capability_id, args, *, deadline):
            try:
                downstream = dispatchers[capability_id]
            except KeyError:
                raise CallError("DENIED", dispatched=False) from None
            return downstream(capability_id, args, deadline=deadline)

        yield CapabilityRegistry(capabilities), dispatch
