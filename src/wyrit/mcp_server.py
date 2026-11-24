"""The agent-facing MCP tools over a host-configured capability set."""

import argparse
from contextlib import asynccontextmanager
import json
import logging
from typing import Any

from anyio import BrokenResourceError, ClosedResourceError, create_task_group, sleep, to_thread
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from .capabilities import Capability, CapabilityRegistry
from .capture import RunCapture
from .catalog import _EXECUTE_DESCRIPTION, _execute_description, _recipe_class, _recipe_description
from .configured_host import ConfiguredHost, configured_host
from .host_config import ConfigError, load_config
from .embed import Host, RunContext, _RunBinding
from .invocation import CallError


def create_server(registry, dispatch=None, *, allowed_capability_ids=None,
                  budgets=None, ledger_path=None):
    """Build a server for one trust scope using an operator-selected allowlist."""
    live = isinstance(registry, ConfiguredHost)
    allowed = None if allowed_capability_ids is None else frozenset(allowed_capability_ids)
    if not live:
        if allowed is None:
            raise ValueError("Static servers require an explicit capability allowlist")
        visible = CapabilityRegistry(registry.resolve(name) for name in allowed)

    def dispatch_read(capability_id, args, *, deadline):
        if visible.resolve(capability_id).classification == "write":
            raise CallError("DENIED", dispatched=False, audit_reason="WRITE_POLICY")
        return dispatch(capability_id, args, deadline=deadline)

    def catalog_provider(_context):
        if live:
            return (cap for cap, _ in registry.catalog(allowed))
        return (visible.resolve(name) for name in visible.ids)

    class ProxyHost(Host):
        async def _bind_run(self, context, run_id, source, recipe_name):
            if not live:
                return _RunBinding(allowed, dispatch_read if dispatch is not None else None,
                                   budgets, ledger_path=ledger_path)

            capture = RunCapture(run_id, source)
            if recipe_name is not None:
                ids, route, limits, path, recipe = await to_thread.run_sync(
                    lambda: registry.bind_run(capture_run=capture, recipe_name=recipe_name,
                                              allowed=allowed),
                )
            else:
                ids, route, limits, path = registry.bind_run(
                    include_settings=True, capture_run=capture,
                )
                ids = ids if allowed is None else allowed.intersection(ids)
                recipe = None
            return _RunBinding(ids, route, limits, ledger_path=path, tracks_send=True,
                               capture=capture, recipe=recipe)

    # Configured transports mark the send boundary at downstream admission.
    host = ProxyHost(catalog_provider=catalog_provider)
    proxy_context = RunContext("mcp", "mcp", "mcp")

    @asynccontextmanager
    async def lifespan(_server):
        async with create_task_group() as tasks:
            try:
                yield {"tasks": tasks, "watching": False}
            finally:
                tasks.cancel_scope.cancel()

    def runnable_recipes():
        return [recipe for recipe in registry.recipes(allowed) if recipe["state"] == "runnable"]

    def tool_catalog():
        catalog = registry.catalog(allowed)
        description = _recipe_description(
            _execute_description(cap for cap, _ in catalog), runnable_recipes(),
        )
        return catalog, description, registry.recipes_enabled

    async def watch_catalog(session, previous):
        while True:
            await sleep(0.1)
            current = await to_thread.run_sync(tool_catalog)
            if current != previous:
                previous = current
                try:
                    await session.send_tool_list_changed()
                except (BrokenResourceError, ClosedResourceError):
                    return

    class WyritMCP(FastMCP):
        async def list_tools(self):
            tools = await super().list_tools()
            if live:
                snapshot = await to_thread.run_sync(tool_catalog)
                try:
                    context = self._mcp_server.request_context
                except LookupError:
                    pass
                else:
                    state = context.lifespan_context
                    if not state["watching"]:
                        state["watching"] = True
                        state["tasks"].start_soon(watch_catalog, context.session, snapshot)
                _, description, recipes_enabled = snapshot
                if not recipes_enabled:
                    tools = [tool for tool in tools if tool.name not in {"run_recipe", "propose_recipe"}]
                for tool in tools:
                    if tool.name == "execute":
                        tool.description = description
            return tools

    server = WyritMCP("wyrit", lifespan=lifespan) if live else FastMCP("wyrit")
    if live:
        get_capabilities = server._mcp_server.get_capabilities

        def advertise_changes(notification_options, experimental_capabilities):
            capabilities = get_capabilities(notification_options, experimental_capabilities)
            capabilities.tools.listChanged = True
            return capabilities

        server._mcp_server.get_capabilities = advertise_changes

    @server.tool()
    async def search(query: str) -> dict[str, Any]:
        """Find up to five relevant capabilities with schemas and JavaScript usage."""
        if live:
            descriptors, unavailable = registry.search(query, allowed=allowed)
        else:
            descriptors = [visible.describe(match["id"]) for match in visible.search(query, limit=5)]
            unavailable = []
        response = {"matches": [
            {**item, "javascript": "await tools.call(" + json.dumps(item["id"])
             + ", args)"}
            for item in descriptors
        ]}
        if live:
            terms = query.casefold().split()
            recipes = await to_thread.run_sync(runnable_recipes)
            for recipe in [item for item in recipes if terms and all(
                term in (item["name"] + " " + item["description"]).casefold() for term in terms
            )][:5]:
                response["matches"].append({
                    "id": recipe["name"], "name": recipe["name"], "kind": "recipe",
                    "description": recipe["description"], "classification": _recipe_class(recipe),
                    "input_schema": recipe["input_schema"], "tool": "run_recipe",
                })
        if unavailable:
            response["unavailable"] = unavailable
        return response

    @server.tool(description=_EXECUTE_DESCRIPTION if live else host.catalog_text(proxy_context))
    async def execute(source: str) -> dict[str, Any]:
        """Run a JavaScript async function body. Use return to produce a JSON value."""
        return await host.execute(source, proxy_context)

    if live:
        @server.tool()
        async def propose_recipe(name: str, description: str, input_schema: dict[str, Any],
                                 source: str) -> dict[str, Any]:
            """Save a new pending recipe for operator review."""
            try:
                return await to_thread.run_sync(
                    registry.propose_recipe, name, description, input_schema, source, allowed,
                )
            except ConfigError as error:
                raise ToolError(str(error)) from None
            except OSError:
                raise ToolError("Could not save recipe.") from None

        @server.tool()
        async def run_recipe(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            """Run an approved recipe with validated arguments. Undeclared calls fail with RECIPE_UNDECLARED."""
            try:
                return await host.run_recipe(name, arguments, proxy_context)
            except ConfigError as error:
                raise ToolError(str(error)) from None

    return server


def _run_demo():
    """Serve a pair of local demonstration capabilities over stdio."""
    row = {
        "type": "object",
        "properties": {"value": {"type": "integer"}},
        "required": ["value"],
        "additionalProperties": False,
    }
    capabilities = (
        Capability("demo.increment", "Add one to an integer value", row, row, classification="read"),
        Capability("demo.format", "Format an integer value as text", row, {"type": "string"},
                   classification="read"),
    )

    def dispatch(capability_id, args, *, deadline):
        if not isinstance(args, dict) or args.keys() != {"value"} or type(args["value"]) is not int:
            raise CallError("INVALID_ARGUMENTS", dispatched=False)
        if capability_id == "demo.increment":
            return {"value": args["value"] + 1}
        if capability_id == "demo.format":
            return f'value={args["value"]}'
        raise CallError("DENIED", dispatched=False)

    create_server(
        CapabilityRegistry(capabilities), dispatch,
        allowed_capability_ids=[capability.id for capability in capabilities],
    ).run(transport="stdio")


def main():
    parser = argparse.ArgumentParser(description="Serve Wyrit's MCP tools over stdio.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--config", metavar="PATH", help="load downstream server configuration from TOML")
    mode.add_argument("--demo", action="store_true", help="serve the local demonstration capabilities")
    args = parser.parse_args()
    if args.demo:
        _run_demo()
        return

    # SDK diagnostics can include raw downstream messages and HTTP metadata.
    previous_level = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        config = load_config(args.config)
        with configured_host(config) as host:
            server = create_server(host)
            server.run(transport="stdio")
    except ConfigError as exc:
        parser.exit(2, f"wyrit: {exc}\n")
    except Exception:
        parser.exit(1, "wyrit: configured host failed\n")
    finally:
        logging.disable(previous_level)


if __name__ == "__main__":
    main()
