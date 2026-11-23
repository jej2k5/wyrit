# Phase 14: The measurement harness

Build a benchmark that runs the same multi-step tasks two ways against the same downstream tools: an agent with direct MCP tool calls, and an agent with Wyrit's `search` and `execute`. It records, per run: model input and output tokens (from the API's usage fields), model turns, wall time, tool calls and whether the task's answer is correct (each task has a checker). Code lives under `bench/` with its own README; it calls the Anthropic Messages API with the model configurable and the API key read from `ANTHROPIC_API_KEY`, and it is never imported by the `wyrit` package.

Tasks (deterministic fake MCP servers for the first three, so results are reproducible):
1. Filter and join two tools' results, returning only mismatches.
2. A dependent chain: list, fetch details for matching items, aggregate.
3. A failing tool and a denied write; score whether each approach reports the outcomes correctly.
4. Real, read-only: on the `jej2k5/mcpxy-test` GitHub repository, list issues, read their comments and count those matching a condition (needs a token from the environment; skipped cleanly without one).
The operator may swap any task for one from their own work; tasks are data files, not code changes.

**Calibration first:** `python -m bench calibrate` runs each task once per approach, prints measured tokens and cost per run, and the projected cost of the full benchmark (tasks x approaches x repetitions, prices from a config table the operator can edit), then stops. `python -m bench run --repetitions N` runs the full benchmark only when given that projection's ID, so it cannot start by accident. It writes a report (Markdown and JSON) with per-task and overall numbers and variance, including where Wyrit loses.

The gate is offline, so tests use a fake model client and the fake servers: the harness wiring, token accounting, cost projection, checkers, the refusal to run without a calibration ID, and report generation. No test calls the real API. Produce the phase handoff with the exact operator commands.
