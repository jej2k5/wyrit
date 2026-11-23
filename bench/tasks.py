"""Declarative tasks, downstream MCP fixtures, and correctness checkers."""

import hashlib
import json
import re

from mcp import types
from mcp.server.lowlevel import Server


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def load_tasks(directory):
    tasks = []
    for path in sorted(directory.glob("*.json")):
        task = json.loads(path.read_text(encoding="utf-8"))
        if task["kind"] not in {"fixture", "github"} or not task["id"] or not task["prompt"]:
            raise ValueError(f"Invalid task in {path}")
        if task["kind"] == "fixture":
            fixture = task["tools"]
            if not fixture or any(not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", name)
                                  or name.startswith("encoded-") for name in fixture):
                raise ValueError(f"Invalid fixture tool names in {path}")
            task["expected"]
        else:
            task["contains"]
        tasks.append(task)
    if not tasks or len({task["id"] for task in tasks}) != len(tasks):
        raise ValueError("Provide a nonempty task suite with unique IDs")
    return tasks


def github_fixture(task, snapshot):
    needle = task["contains"].casefold()
    return {
        **task,
        "tools": {
            "list-issues": {
                "description": "List the selected GitHub issues in jej2k5/mcpxy-test",
                "cases": [{"arguments": {}, "result": {"numbers": list(snapshot)}}],
            },
            "read-comments": {
                "description": "Read the selected GitHub comment excerpts for an issue number",
                "input_schema": {"type": "object", "properties": {"number": {"type": "integer"}},
                                 "required": ["number"], "additionalProperties": False},
                "cases": [
                    {"arguments": {"number": number}, "result": {"comments": comments}}
                    for number, comments in snapshot.items()
                ],
            },
        },
        "expected": {"count": sum(needle in comment.casefold()
                                  for comments in snapshot.values() for comment in comments)},
        "required_requests": [
            {"tool": "list-issues", "arguments": {}},
            *({"tool": "read-comments", "arguments": {"number": number}} for number in snapshot),
        ],
    }


def downstream_server(task, record, *, deny_writes_at_entry=False):
    """Serve immutable fixture data over MCP and record requests actually received."""
    server = Server("bench-downstream")
    fixture = task["tools"]

    @server.list_tools()
    async def list_tools():
        return [
            types.Tool(
                name=name, description=tool["description"],
                inputSchema=tool.get("input_schema", {
                    "type": "object", "additionalProperties": False,
                }),
                annotations=types.ToolAnnotations(readOnlyHint=not tool.get("write", False)),
            )
            for name, tool in fixture.items()
        ]

    async def call_tool(request):
        name, arguments = request.params.name, request.params.arguments or {}
        tool = fixture[name]
        case = next((case for case in tool.get("cases", [])
                     if canonical(case["arguments"]) == canonical(arguments)), {})
        denied = tool.get("write", False) and deny_writes_at_entry
        failed = tool.get("write", False) or "result" not in case
        record(None if denied else {"tool": name, "arguments": arguments},
               {"tool": name, "outcome": "DENIED" if denied else "FAILED" if failed else "SUCCEEDED"})
        if failed:
            message = "DENIED: Fixture write denied by policy" if denied else "Fixture tool failed"
            result = types.CallToolResult(
                content=[types.TextContent(type="text", text=message)], isError=True,
            )
        else:
            result = types.CallToolResult(
                content=[types.TextContent(type="text", text=canonical(case["result"]))],
                structuredContent=case["result"],
            )
        return types.ServerResult(result)

    server.request_handlers[types.CallToolRequest] = call_tool
    return server


def check(task, answer, requests, outcomes):
    observed = {canonical(request) for request in requests}
    return (
        canonical(answer) == canonical(task["expected"])
        and all(canonical(request) in observed for request in task.get("required_requests", []))
        and all(outcome in outcomes for outcome in task.get("required_outcomes", []))
    )
