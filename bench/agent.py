"""Run a bounded Anthropic tool-use loop through host-owned MCP sessions."""

from contextlib import ExitStack
from datetime import timedelta
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from time import monotonic

from anyio.from_thread import start_blocking_portal
from mcp.shared.memory import create_connected_server_and_client_session

from src.wyrit.capabilities import CapabilityRegistry
from src.wyrit.configured_host import configured_host
from src.wyrit.mcp_adapter import MCPAdapter
from src.wyrit.mcp_server import create_server

from .setup import mcp_config
from .tasks import canonical, check, downstream_server


APPROACHES = ("direct", "wyrit", "wyrit-recipe")
JSON_INSTRUCTION = "\nReturn only the requested JSON value."
SYSTEM = {
    "direct": "Use the supplied MCP tools to complete the task.",
    "wyrit": (
        "Use execute's capability catalog or call search if you need more detail. "
        "In execute, use await tools.call with the discovered ID and an arguments object. "
        "Source is an async function body. Use return for a JSON result. "
        "Catch individual tool errors if you need to continue after a failure."
    ),
}
SYSTEM["wyrit-recipe"] = (
    SYSTEM["wyrit"] + " When a matching reviewed recipe appears in the execute catalog, use run_recipe."
    + JSON_INSTRUCTION
)


def error_details(error):
    return {"stop_reason": "error", "is_error": True,
            "error": f"{type(error).__name__}: {error}", "correct": False}


def error_row(error, elapsed):
    return {
        "input_tokens": 0, "output_tokens": 0, "model_turns": 0, "wall_seconds": elapsed,
        "tool_calls": 0, "downstream_calls": 0, "answer": "", "execution_statuses": [],
        "tool_outcomes": [], **error_details(error),
    }


def read_outcomes(path, tool_names, execution_statuses, outcomes, *, successful_recipes=None):
    try:
        stream = path.open(encoding="utf-8")
    except FileNotFoundError:
        return
    malformed = []
    with stream:
        for line_number, line in enumerate(stream, 1):
            try:
                run = json.loads(line)
                if not isinstance(run["outcome"], str) or not isinstance(run["actions"], list):
                    raise ValueError
                actions = []
                for action in run["actions"]:
                    capability = action.get("capability")
                    if ((capability is not None and not isinstance(capability, str))
                            or not isinstance(action["outcome"], str)):
                        raise ValueError
                    actions.append({"tool": tool_names(capability), "outcome": action["outcome"]})
            except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
                malformed.append(line_number)
                continue
            execution_statuses.append(run["outcome"])
            outcomes.extend(actions)
            if successful_recipes is not None and run["outcome"] == "SUCCESS":
                successful_recipes.append(run.get("recipe"))
    if malformed:
        raise ValueError(f"Malformed runs.jsonl at line {malformed[0]} ({len(malformed)} invalid records)")


def run_agent(task, approach, model, settings):
    start = monotonic()
    try:
        return _run_agent(task, approach, model, settings)
    except Exception as error:
        return error_row(error, monotonic() - start)


def _run_agent(task, approach, model, settings):
    requests = []
    outcomes = []
    execution_statuses = []
    with ExitStack() as stack:
        portal = stack.enter_context(start_blocking_portal())

        def connect(server):
            return stack.enter_context(portal.wrap_async_context_manager(
                create_connected_server_and_client_session(server)
            ))

        def record(request, outcome):
            if request is not None:
                requests.append(request)
            if approach == "direct":
                outcomes.append(outcome)

        if approach == "wyrit-recipe":
            tool_names = {f"bench.{name}": name for name in task["tools"]}
        else:
            downstream = connect(downstream_server(task, record, deny_writes_at_entry=approach == "direct"))
            adapter = MCPAdapter("bench", downstream, portal)
            capabilities = {cap.adapter_metadata["tool_name"]: cap for cap in adapter.capabilities}
            tool_names = {cap.id: name for name, cap in capabilities.items()}
        if approach == "direct":
            tools = [
                {"name": name, "description": cap.description, "input_schema": cap.input_schema}
                for name, cap in capabilities.items()
            ]

            def invoke(name, arguments):
                if name not in capabilities:
                    return "Unknown MCP tool", True
                response = portal.call(downstream.call_tool, name, arguments, timedelta(seconds=10))
                blocks = [block.model_dump(mode="json") for block in response.content]
                if response.isError:
                    if len(blocks) == 1 and blocks[0]["type"] == "text":
                        return blocks[0]["text"], True
                    return blocks, True
                result = getattr(response, "structuredContent", None)
                return (blocks if result is None else result), False

        else:
            directory = Path(stack.enter_context(TemporaryDirectory()))
            ledger = directory / "runs.jsonl"
            if approach == "wyrit-recipe":
                mcp_config(directory, task, approach)
                host = stack.enter_context(configured_host(directory / "wyrit.toml"))
                server = create_server(host, allowed_capability_ids=tool_names)
            else:
                server = create_server(CapabilityRegistry(capabilities.values()), adapter.dispatch,
                                       allowed_capability_ids=tool_names, ledger_path=ledger)
            session = connect(server._mcp_server)
            tools = [
                {"name": tool.name, "description": tool.description, "input_schema": tool.inputSchema}
                for tool in portal.call(session.list_tools).tools
            ]
            names = {tool["name"] for tool in tools}

            def invoke(name, arguments):
                if name not in names:
                    return {"code": "DENIED"}, True
                response = portal.call(session.call_tool, name, arguments)
                result = getattr(response, "structuredContent", None)
                if result is None:
                    blocks = [block.model_dump(mode="json") for block in response.content]
                    result = blocks
                    if not response.isError and len(blocks) == 1 and blocks[0]["type"] == "text":
                        try:
                            result = json.loads(blocks[0]["text"])
                        except (ValueError, RecursionError):
                            pass
                if name in {"execute", "run_recipe"}:
                    execution_statuses.append(result.get("status") if isinstance(result, dict)
                                              else "MCP_ERROR")
                return result, response.isError

        metrics = run_loop(task["prompt"], approach, model, settings, tools, invoke)
        successful_recipes = []
        if approach != "direct":
            try:
                read_outcomes(ledger, tool_names.get, [], outcomes, successful_recipes=successful_recipes)
                if approach == "wyrit-recipe":
                    request_log = directory / "requests.jsonl"
                    if request_log.exists():
                        with request_log.open(encoding="utf-8") as stream:
                            requests.extend(record["request"] for record in map(json.loads, stream)
                                            if record["request"] is not None)
            except (OSError, ValueError) as error:
                details = error_details(error)
                if metrics.get("error"):
                    details["error"] = metrics["error"] + "; " + details["error"]
                metrics.update(details)
        answer = metrics.pop("answer")
        correct = metrics["stop_reason"] == "end_turn" and check_answer(task, answer, requests, outcomes)
        if approach == "wyrit-recipe":
            correct = correct and task["recipe"]["name"] in successful_recipes
        return {**metrics, "downstream_calls": len(requests), "correct": correct,
                "execution_statuses": execution_statuses, "tool_outcomes": outcomes}


def check_answer(task, answer, requests, outcomes):
    try:
        if isinstance(answer, str):
            fence = re.fullmatch(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n[ \t]*```",
                                 answer.strip(), re.DOTALL)
            if fence:
                answer = fence[1]
        return check(task, json.loads(answer), requests, outcomes)
    except (TypeError, ValueError, RecursionError):
        return False


def run_loop(prompt, approach, model, settings, tools, invoke):
    messages = [{"role": "user", "content": prompt if approach == "wyrit-recipe"
                 else prompt + JSON_INSTRUCTION}]
    input_tokens = output_tokens = tool_calls = 0
    answer = ""
    start = monotonic()
    turn = 0
    failure = {}
    try:
        for _ in range(settings["max_turns"]):
            response = model(
                model=settings["model"], system=SYSTEM[approach], messages=messages,
                tools=tools, max_tokens=settings["max_tokens"],
            )
            turn += 1
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
                    content_value = value if is_error and isinstance(value, str) else canonical(value)
                    results.append({"type": "tool_result", "tool_use_id": block["id"],
                                    "content": content_value, "is_error": is_error})
            if not results:
                stop = "invalid_tool_response"
                break
            messages.extend([{"role": "assistant", "content": content},
                             {"role": "user", "content": results}])
    except Exception as error:
        failure = error_details(error)
        stop = "error"
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "model_turns": turn,
            "wall_seconds": monotonic() - start, "tool_calls": tool_calls, "answer": answer,
            "stop_reason": stop, **failure}
