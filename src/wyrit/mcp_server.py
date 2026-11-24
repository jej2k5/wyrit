"""The agent-facing MCP tools over a host-configured capability set."""

import argparse
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import asdict
import json
import logging
from typing import Any
from uuid import uuid4

from anyio import BrokenResourceError, ClosedResourceError, create_task_group, sleep, to_thread
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from jsonschema.exceptions import ValidationError

from .capabilities import Capability, CapabilityRegistry, RunAuthorization
from .capture import RunCapture
from .configured_host import ConfiguredHost, configured_host
from .host_config import ConfigError, load_config
from .invocation import CallError, _check_value
from .mcp_adapter import _validator
from .runtime import run_javascript


_DESCRIPTION_BYTES = 8 * 1024
_CATALOG_LIMIT = 60
_EXECUTE_DESCRIPTION = (
    "Run a JavaScript async function body. Use return to produce a JSON value.\n"
    'Inside the script, await tools.call("capability.id", args) for a JSON result. '
    "A rejected call throws an Error with read-only code and capability fields.\n"
    "Codes: DENIED, TOOL_ERROR, OUTCOME_UNKNOWN, CALL_TIMEOUT, INVALID_ARGUMENTS, "
    "RESULT_TOO_LARGE, DISPATCH_UNAVAILABLE, INVALID_RESULT.\n"
    "DENIED also has reason WRITE_POLICY or NOT_AUTHORIZED."
)
_JSON_TYPES = {"array", "boolean", "integer", "null", "number", "object", "string"}


def _schema_type(schema):
    if schema is False:
        return "never"
    kind = schema.get("type") if isinstance(schema, dict) else None
    if isinstance(kind, str) and kind in _JSON_TYPES:
        return kind
    if isinstance(kind, list):
        return "|".join(sorted({item for item in kind
                                if isinstance(item, str) and item in _JSON_TYPES})) or "any"
    return "any"


def _signature(schema):
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    required = schema.get("required", []) if isinstance(schema, dict) else []
    if isinstance(properties, dict) and isinstance(required, list):
        required = {name for name in required if isinstance(name, str)}
        names = properties.keys() | required
        if names:
            return "{" + ", ".join(
                f"{json.dumps(name, ensure_ascii=False)}{'*' if name in required else ''}: "
                f"{_schema_type(properties.get(name))}" for name in sorted(names)
            ) + "}"
    return "{}" if _schema_type(schema) == "object" else _schema_type(schema)


def _execute_description(capabilities):
    capabilities = sorted(capabilities, key=lambda cap: cap.id)
    if not capabilities:
        return _EXECUTE_DESCRIPTION + "\n\nNo capabilities are currently exposed."
    if len(capabilities) <= _CATALOG_LIMIT:
        description = _EXECUTE_DESCRIPTION + "\n\nCapabilities (* required):"
        for cap in capabilities:
            description += (
                f"\n{cap.id} [{cap.classification}] ({_signature(cap.input_schema)}) "
                f"— {' '.join(cap.description.split())}"
            )
            encoded = description.encode("utf-8", errors="replace")
            if len(encoded) > _DESCRIPTION_BYTES:
                break
        else:
            return encoded.decode("utf-8")

    counts = Counter(cap.id.rpartition(".")[0] or "(unqualified)" for cap in capabilities)
    description = _EXECUTE_DESCRIPTION + "\n\nUse search to discover capabilities. Namespace counts:"
    overflow = "\nAdditional namespaces available through search."
    omitted = False
    for namespace, count in sorted(counts.items()):
        line = f"\n{namespace}: {count}"
        if len((description + line + overflow).encode("utf-8")) > _DESCRIPTION_BYTES:
            omitted = True
        else:
            description += line
    return description + overflow if omitted else description


def _brief(text, limit=256):
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _recipe_description(description, recipes):
    if not recipes:
        return description
    heading = "\n\nRecipes (* required):"
    overflow = "\nUse search to discover more recipes."
    lines = [
        f"\n{recipe['name']} [{_recipe_class(recipe)}] ({_brief(_signature(recipe['input_schema']))}) "
        f"— {_brief(' '.join(recipe['description'].split()))}"
        for recipe in recipes
    ]
    # Reserve space for recipe discovery even when the capability catalog is full.
    available = min(_DESCRIPTION_BYTES * 3 // 4, _DESCRIPTION_BYTES - len((heading + overflow).encode()))
    if len(description.encode()) > available:
        description = description.encode()[:available].decode(errors="ignore").rsplit("\n", 1)[0]
    description += heading
    for line in lines:
        if len((description + line + overflow).encode()) > _DESCRIPTION_BYTES:
            return description + overflow
        description += line
    return description


def _recipe_class(recipe):
    return "write" if any(record["class"] == "write"
                          for record in recipe["capabilities"].values()) else "read"


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

    @server.tool(description=_EXECUTE_DESCRIPTION if live else _execute_description(
        visible.resolve(name) for name in visible.ids
    ))
    async def execute(source: str) -> dict[str, Any]:
        """Run a JavaScript async function body. Use return to produce a JSON value."""
        return await execute_run(source)

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
            return await execute_run("", name, arguments)

    async def execute_run(source, name=None, arguments=None):
        run_id = uuid4().hex
        capture = RunCapture(run_id, source) if live else None
        recipe_metadata = None
        if name is not None:
            try:
                ids, run_dispatch, run_budgets, run_ledger, recipe = await to_thread.run_sync(
                    lambda: registry.bind_run(capture_run=capture, recipe_name=name, allowed=allowed),
                )
            except ConfigError as error:
                raise ToolError(str(error)) from None
            try:
                _check_value(arguments)
                _validator(recipe["input_schema"]).validate(arguments)
            except (ValidationError, ValueError, RecursionError):
                raise ToolError("INVALID_ARGUMENTS: arguments do not match the recipe schema.") from None
            except Exception:
                raise ToolError("INVALID_ARGUMENTS: recipe schema could not be evaluated.") from None
            source = recipe["source"]
            recipe_metadata = {"recipe": name, "source_digest": recipe["approval"]["source_digest"]}
        elif live:
            ids, run_dispatch, run_budgets, run_ledger = registry.bind_run(
                include_settings=True, capture_run=capture,
            )
            ids = ids if allowed is None else allowed.intersection(ids)
        else:
            ids, run_dispatch = allowed, dispatch_read if dispatch is not None else None
            run_budgets, run_ledger = budgets, ledger_path
        authorization = RunAuthorization(run_id, ids)

        def run():
            if capture is not None:
                capture.start(run_ledger)
            return run_javascript(
                source, run_id=run_id, authorization=authorization, dispatch=run_dispatch,
                budgets=run_budgets, ledger_path=run_ledger, tracks_send=live, _capture=capture,
                recipe=recipe_metadata, arguments=arguments,
            )

        result = asdict(await to_thread.run_sync(run))
        return {
            "run_id": run_id,
            **result,
            "actions": list(result["actions"]),
            "unknown_calls": list(result["unknown_calls"]),
        }

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
