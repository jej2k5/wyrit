"""Run a bounded Anthropic tool-use loop through host-owned MCP sessions."""

from contextlib import ExitStack
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import uuid4

from anyio.from_thread import start_blocking_portal
from mcp.shared.memory import create_connected_server_and_client_session

from src.wyrit.capabilities import CapabilityRegistry, RunAuthorization
from src.wyrit.invocation import CallError
from src.wyrit.mcp_adapter import MCPAdapter
from src.wyrit.mcp_server import create_server

from .tasks import canonical, check, downstream_server


APPROACHES = ("direct", "wyrit")
SYSTEM = {
    "direct": "Use the supplied MCP tools to complete the task.",
    "wyrit": (
        "Use search to discover capabilities and execute to run JavaScript. "
        "In execute, use await tools.call with the discovered ID and an arguments object. "
        "Source is an async function body. Use return for a JSON result. "
        "Catch individual tool errors if you need to continue after a failure."
    ),
}


def run_agent(task, approach, model, settings):
    requests = []
    with ExitStack() as stack:
        portal = stack.enter_context(start_blocking_portal())

        def connect(server):
            return stack.enter_context(portal.wrap_async_context_manager(
                create_connected_server_and_client_session(server)
            ))

        downstream = connect(downstream_server(task, requests))
        adapter = MCPAdapter("bench", downstream, portal)
        capabilities = {cap.adapter_metadata["tool_name"]: cap for cap in adapter.capabilities}
        ids = frozenset(cap.id for cap in capabilities.values())
        outcomes = []
        execution_statuses = []
        if approach == "direct":
            run_id = uuid4().hex
            authorization = RunAuthorization(run_id, ids)
            tools = [
                {"name": name, "description": cap.description, "input_schema": cap.input_schema}
                for name, cap in capabilities.items()
            ]

            def invoke(name, arguments):
                capability = capabilities.get(name)
                if capability is None or not authorization.allows(run_id, capability.id):
                    return {"code": "DENIED"}, True
                try:
                    value = adapter.dispatch(capability.id, arguments, deadline=monotonic() + 10)
                except CallError as error:
                    outcome = error.action_status or {
                        "DENIED": "DENIED", "CALL_TIMEOUT": "UNKNOWN", "OUTCOME_UNKNOWN": "UNKNOWN",
                    }.get(error.code, "FAILED")
                    outcomes.append({"tool": name, "outcome": outcome})
                    return {"code": error.code, "outcome": outcome}, True
                outcomes.append({"tool": name, "outcome": "SUCCEEDED"})
                return value, False

        else:
            directory = stack.enter_context(TemporaryDirectory())
            ledger = Path(directory) / "runs.jsonl"
            server = create_server(CapabilityRegistry(capabilities.values()), adapter.dispatch,
                                   allowed_capability_ids=ids, ledger_path=ledger)
            session = connect(server._mcp_server)
            tools = [
                {"name": tool.name, "description": tool.description, "input_schema": tool.inputSchema}
                for tool in portal.call(session.list_tools).tools
            ]
            names = {tool["name"] for tool in tools}
            tool_names = {cap.id: name for name, cap in capabilities.items()}

            def invoke(name, arguments):
                if name not in names:
                    return {"code": "DENIED"}, True
                response = portal.call(session.call_tool, name, arguments)
                result = getattr(response, "structuredContent", None)
                if result is None:
                    blocks = [block.model_dump(mode="json") for block in response.content]
                    result = (
                        json.loads(blocks[0]["text"])
                        if not response.isError and len(blocks) == 1 and blocks[0]["type"] == "text"
                        else blocks
                    )
                if name == "execute":
                    execution_statuses.append(result.get("status") if isinstance(result, dict)
                                              else "MCP_ERROR")
                return result, response.isError

        metrics = run_loop(task["prompt"], approach, model, settings, tools, invoke)
        if approach == "wyrit" and ledger.exists():
            for line in ledger.read_text(encoding="utf-8").splitlines():
                for action in json.loads(line)["actions"]:
                    outcomes.append({"tool": tool_names.get(action["capability"]),
                                     "outcome": action["outcome"]})
        try:
            answer = json.loads(metrics.pop("answer"))
            correct = metrics["stop_reason"] == "end_turn" and check(task, answer, requests, outcomes)
        except (ValueError, RecursionError):
            correct = False
        return {**metrics, "downstream_calls": len(requests), "correct": correct,
                "execution_statuses": execution_statuses, "tool_outcomes": outcomes}


def run_loop(prompt, approach, model, settings, tools, invoke):
    messages = [{"role": "user", "content": prompt + "\nReturn only the requested JSON value."}]
    input_tokens = output_tokens = tool_calls = 0
    answer = ""
    start = monotonic()
    for turn in range(1, settings["max_turns"] + 1):
        response = model(
            model=settings["model"], system=SYSTEM[approach], messages=messages,
            tools=tools, max_tokens=settings["max_tokens"],
        )
        input_tokens += response["usage"]["input_tokens"]
        output_tokens += response["usage"]["output_tokens"]
        content = response["content"]
        stop = response["stop_reason"]
        if stop != "tool_use":
            answer = "".join(block["text"] for block in content if block["type"] == "text")
            break
        if turn == settings["max_turns"]:
            stop = "turn_limit"
            break
        results = []
        for block in content:
            if block["type"] == "tool_use":
                tool_calls += 1
                value, is_error = invoke(block["name"], block["input"])
                results.append({"type": "tool_result", "tool_use_id": block["id"],
                                "content": canonical(value), "is_error": is_error})
        if not results:
            stop = "invalid_tool_response"
            break
        messages.extend([{"role": "assistant", "content": content},
                         {"role": "user", "content": results}])
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "model_turns": turn,
            "wall_seconds": monotonic() - start, "tool_calls": tool_calls, "answer": answer,
            "stop_reason": stop}
