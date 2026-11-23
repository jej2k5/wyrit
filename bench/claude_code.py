"""Run one Claude Code subscription session against benchmark-owned MCP servers."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic

from .agent import JSON_INSTRUCTION, SYSTEM, check_answer, error_details, error_row, read_outcomes
from .tasks import canonical

ROOT = Path(__file__).resolve().parent.parent
COST_LABEL = "list-price equivalent; billed to the subscription"
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                "cache_read_input_tokens")


def mcp_config(directory, task, approach):
    fixture = directory / "fixture.json"
    fixture.write_text(canonical({"tools": task["tools"]}), encoding="utf-8")
    downstream = [str(ROOT / "bench" / "fixture_stdio.py"), str(fixture),
                  str(directory / "requests.jsonl")]
    if approach == "direct":
        servers = {"bench": {"command": sys.executable, "args": [*downstream, "--direct"]}}
    else:
        config = directory / "wyrit.toml"
        config.write_text(
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


def json_lines(stream):
    for line in stream:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            yield value


def read_log(path):
    try:
        with path.open(encoding="utf-8") as stream:
            return list(json_lines(stream))
    except FileNotFoundError:
        return []


def run_claude(task, approach, settings, env):
    start = monotonic()
    row = {**dict.fromkeys(TOKEN_FIELDS, 0), "total_tokens": 0, "cost_usd": 0}
    try:
        _run_claude(task, approach, settings, env, row)
    except Exception as error:
        details = error_details(error)
        if row.get("error"):
            details["error"] = row["error"] + "; " + details["error"]
        row = {**error_row(error, monotonic() - start), **row, **details}
    return row


def _run_claude(task, approach, settings, env, row):
    with TemporaryDirectory(prefix="wyrit-bench-") as temporary:
        directory = Path(temporary)
        cwd = directory / "work"
        cwd.mkdir()
        config_path, config = mcp_config(directory, task, approach)
        allowed_tools = [f"mcp__{name}" for name in sorted(config["mcpServers"])]
        args = [
            "claude", "-p", task["prompt"] + JSON_INSTRUCTION,
            "--output-format", "stream-json", "--verbose", "--model", settings["model"],
            "--tools", "", "--strict-mcp-config", "--mcp-config", str(config_path),
            "--allowedTools", *allowed_tools, "--permission-mode", "dontAsk",
            "--no-session-persistence", "--setting-sources", "", "--disable-slash-commands",
            "--append-system-prompt", SYSTEM[approach],
        ]
        excluded = {"ANTHROPIC_API_KEY", "GITHUB_TOKEN"}
        child_env = {key: env[key] for key in env if key not in excluded}
        result, tool_calls, error = invoke(args, cwd, child_env, settings["timeout_seconds"])
        denials = result.get("permission_denials") or []
        if denials:
            names = ", ".join(denial.get("tool_name") or "<unknown>" for denial in denials)
            denial_error = f"Claude Code denied {len(denials)} tool call(s): {names}"
            if error:
                row["process_error"] = error
            error = denial_error
            row["permission_denials"] = denials
        usage = result.get("usage") or {}
        tokens = {field: usage.get(field, 0) for field in TOKEN_FIELDS}
        answer = result.get("result", "")
        execution_statuses = []
        outcomes = []
        row.update(
            **tokens, total_tokens=sum(tokens.values()), model_turns=result.get("num_turns", 0),
            wall_seconds=result["wall_seconds"], cost_usd=result.get("total_cost_usd", 0),
            tool_calls=tool_calls, answer=answer, stop_reason="error" if error else "end_turn",
            is_error=bool(error), error=error, execution_statuses=execution_statuses,
            tool_outcomes=outcomes,
        )
        records = read_log(directory / "requests.jsonl")
        requests = [record["request"] for record in records if record["request"] is not None]
        row["downstream_calls"] = len(requests)
        if approach == "direct":
            outcomes.extend(record["outcome"] for record in records)
        else:
            read_outcomes(directory / "runs.jsonl",
                          lambda capability: (capability or "").removeprefix("bench.") or None,
                          execution_statuses, outcomes)
        row["correct"] = not error and check_answer(task, answer, requests, outcomes)


def invoke(args, cwd, env, timeout):
    result = {}
    error = assistant_error = None
    tool_ids = set()
    unnamed_calls = 0
    # Files let timeout cleanup finish even when a descendant holds stdout open.
    with (cwd.parent / "stdout").open("w+", encoding="utf-8", errors="replace") as stdout, (
        cwd.parent / "stderr"
    ).open("w+", encoding="utf-8", errors="replace") as stderr:
        start = monotonic()
        try:
            process = subprocess.Popen(
                args, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                start_new_session=True,
            )
        except OSError as exc:
            error = f"Could not start Claude Code: {exc.strerror}"
        else:
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                error = f"Claude Code timed out after {timeout:g} seconds"
            finally:
                # The benchmark MCP servers inherit this process group.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            stdout.seek(0)
            for event in json_lines(stdout):
                if event.get("type") == "result":
                    result = event
                if event.get("type") == "assistant":
                    assistant_error = event.get("error") or assistant_error
                    for block in event.get("message", {}).get("content", []):
                        if block.get("type") == "tool_use":
                            if block.get("id"):
                                tool_ids.add(block["id"])
                            else:
                                unnamed_calls += 1
            if error is None and (process.returncode or result.get("is_error") or not result):
                stderr.seek(0)
                reason = (result.get("result") or " ".join(result.get("errors") or [])
                          or stderr.read(4096).strip() or assistant_error or result.get("subtype"))
                if process.returncode:
                    error = f"Claude Code exited with status {process.returncode}"
                elif result:
                    error = "Claude Code reported an error"
                else:
                    error = "Claude Code returned no result event"
                if reason:
                    error += f" ({str(reason)[:500]})"
        elapsed = monotonic() - start
    result["wall_seconds"] = result.get("duration_ms", elapsed * 1000) / 1000
    return result, len(tool_ids) + unnamed_calls, error
