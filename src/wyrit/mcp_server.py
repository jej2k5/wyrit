"""The agent-facing MCP tools over a host-configured capability set."""

import argparse
from dataclasses import asdict
import json
import logging
from typing import Any
from uuid import uuid4

from anyio import to_thread
from mcp.server.fastmcp import FastMCP

from .capabilities import Capability, CapabilityRegistry, RunAuthorization
from .configured_host import ConfiguredHost, configured_host
from .host_config import ConfigError, load_config
from .invocation import CallError
from .runtime import run_javascript


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
            raise CallError("DENIED", dispatched=False)
        return dispatch(capability_id, args, deadline=deadline)

    server = FastMCP("wyrit")

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
        if unavailable:
            response["unavailable"] = unavailable
        return response

    @server.tool()
    async def execute(source: str) -> dict[str, Any]:
        """Run a JavaScript async function body. Use return to produce a JSON value."""
        run_id = uuid4().hex
        if live:
            ids, run_dispatch, run_budgets, run_ledger = registry.bind_run(include_settings=True)
            ids = ids if allowed is None else allowed.intersection(ids)
        else:
            ids, run_dispatch = allowed, dispatch_read if dispatch is not None else None
            run_budgets, run_ledger = budgets, ledger_path
        authorization = RunAuthorization(run_id, ids)

        def run():
            return run_javascript(
                source, run_id=run_id, authorization=authorization, dispatch=run_dispatch,
                budgets=run_budgets, ledger_path=run_ledger, tracks_send=live,
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
