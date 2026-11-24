"""Set up local MCP servers and reviewed fixture recipes."""

import json
from pathlib import Path
import sys

from src.wyrit.capabilities import Capability
from src.wyrit.recipes import Recipes

from .tasks import EMPTY_INPUT_SCHEMA, canonical


ROOT = Path(__file__).resolve().parent.parent


def approve_fixture_recipe(directory, task):
    definition = task["recipe"]
    capabilities = {}
    for name, tool in task["tools"].items():
        capability = Capability(
            f"bench.{name}", tool["description"], tool.get("input_schema", EMPTY_INPUT_SCHEMA),
            True, classification="write" if tool.get("write", False) else "read",
        )
        capabilities[capability.id] = capability
    recipes = Recipes(directory)
    recipes.propose(**definition, capabilities=capabilities, secrets=())
    name = definition["name"]
    pending = recipes.get(name, capabilities, ())
    recipes.change("approve", name, pending["revision"], "benchmark fixture", capabilities, ())


def mcp_config(directory, task, approach):
    fixture = directory / "fixture.json"
    fixture.write_text(canonical({"tools": task["tools"]}), encoding="utf-8")
    downstream = [str(ROOT / "bench" / "fixture_stdio.py"), str(fixture),
                  str(directory / "requests.jsonl")]
    if approach == "direct":
        servers = {"bench": {"command": sys.executable, "args": [*downstream, "--direct"]}}
    else:
        config = directory / "wyrit.toml"
        recipe_setting = ""
        if approach == "wyrit-recipe":
            approve_fixture_recipe(directory / "recipes", task)
            recipe_setting = "recipes_path = \"recipes\"\n"
        config.write_text(
            f'{recipe_setting}'
            'ledger_path = "runs.jsonl"\n[servers.bench]\ntransport = "stdio"\n'
            'namespace = "bench"\nexpose = "all"\ndeny_writes = true\n'
            f'command = {json.dumps(sys.executable, ensure_ascii=False)}\n'
            f'args = {json.dumps(downstream, ensure_ascii=False)}\n',
            encoding="utf-8",
        )
        servers = {"wyrit": {
            "command": sys.executable,
            "args": ["-m", "wyrit.mcp_server", "--config", str(config)],
            "env": {"PYTHONPATH": str(ROOT / "src")},
        }}
    path = directory / "mcp.json"
    config = {"mcpServers": servers}
    path.write_text(canonical(config), encoding="utf-8")
    return path, config
