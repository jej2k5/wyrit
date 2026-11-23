# Measuring direct MCP against Wyrit

From the checkout root, use Python 3.11 or later and install Wyrit. For the default driver, have a signed-in Claude Code subscription with `claude` on `PATH`. Wyrit execution requires Node.js and a supported native sandbox on macOS or Linux. To include the GitHub task, set `GITHUB_TOKEN` with Issues read access to `jej2k5/mcpxy-test`.

```sh
python -m pip install -e .
python -m bench calibrate --repetitions 3
read -r CALIBRATION_ID
python -m bench run --repetitions 3 --calibration-id "$CALIBRATION_ID"
```

Calibration runs each active task once per approach. Review the per-run correctness and errors and the projected list-price cost, subscription token usage, and wall time before pasting the calibration ID into `read`. The projection multiplies the observed measurements by the planned repetitions and excludes calibration itself. Failed sessions can leave usage unreported. Review the error rows before using that projection.

The calibration ID binds the driver, model, task data, GitHub task availability, driver settings, and repetition count. Repeat any `--driver`, `--model`, `--config`, `--tasks`, `--output`, and Claude Code `--timeout` options on both commands. The timeout defaults to 600 seconds per Claude Code process. The default output directory is `bench-results/`.

Claude Code receives a server-level permission for each server in its generated config. The bundled approaches use `mcp__bench` for direct and `mcp__wyrit` for Wyrit. Calibration exits with an error and withholds the ID if Claude Code reports any permission denials or if a task with `required_requests` records zero downstream calls. Calibrate again if you have an ID from before the server-level permission fix.

## Messages API driver

Set `ANTHROPIC_API_KEY` in the environment and explicitly select the API driver.

```sh
python -m bench calibrate --driver api --repetitions 3
read -r CALIBRATION_ID
python -m bench run --driver api --repetitions 3 --calibration-id "$CALIBRATION_ID"
```

Edit `bench/config.json` to set the default model. For the API driver, it also contains the maximum turns, maximum response tokens per turn, and the input and output prices per million tokens for each model. API costs use that editable table. [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing)

## What gets measured

- Direct mode exposes the downstream tool schemas. Wyrit mode exposes its MCP `search` and `execute`, backed by the same fixtures. Both grant read access only. Wyrit fails closed if its native sandbox is unavailable.
- The subscription driver starts one headless Claude Code process per run from an empty temporary working directory. Claude Code owns its login. The benchmark removes `ANTHROPIC_API_KEY` and `GITHUB_TOKEN` from the child environment. [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference)
- Claude Code contributes the final event's turn count, duration, input, output, cache creation and cache read tokens, and cost. The total adds the four token counts. Cost is labeled “list-price equivalent; billed to the subscription.” Claude Code includes the same fixed instructions in both approaches. Compare the differences between approaches when interpreting token counts.
- The Messages API driver adds input and output tokens from each API turn. Its wall time covers model requests and tool invocations through the last response. Both drivers count model-facing tool calls separately from requests admitted to the downstream fixtures.
- Sessions write `report.json` with individual runs and `report.md` with mean, population variance, and Wyrit-minus-direct deltas overall and by task. Higher resource use or a lower correctness rate is flagged as a loss. Claude Code usage limits, nonzero exits, and timeouts produce error rows with reasons. The driver makes one attempt per run.
- Claude Code permission denials remain visible in individual run records and error listings. Summary statistics use only runs without permission denials. Deltas are unavailable when either approach has no eligible runs.
- With `GITHUB_TOKEN` set, the host fetches the five oldest available issues across all states, excluding pull requests. It reads up to ten comments per issue and keeps the first 240 characters of each. Both approaches read the same snapshot for that invocation. Calibration and the full run fetch separately, outside the timed comparison. Comment excerpts returned to the agent go to Anthropic as tool results. Without the token, the task is skipped. [GitHub issues](https://docs.github.com/en/rest/issues/issues#list-repository-issues) · [GitHub issue comments](https://docs.github.com/en/rest/issues/comments#list-issue-comments)

## Replace a task

Pass `--tasks path/to/tasks` to select a directory of JSON task files. Copy a bundled fixture and edit its unique `id`, `prompt`, and `tools`. Each tool has a description, an optional object `input_schema`, and `cases` mapping exact `arguments` objects to structured `result` objects. Calls with no matching result return an MCP tool error. A tool with `"write": true` is exposed for a simulated denial. Set `expected` to the exact JSON answer, and optionally require observed `required_requests` and `required_outcomes`. Object key order is ignored and array order is significant. Record representative read-only results from your own workflow as fixture cases to replay them across both approaches.

The built-in `github` task uses the fixed repository and takes a `contains` string for its case-insensitive checker. Update the prompt accordingly if you change the condition.

## Offline verification

```sh
python -m unittest discover -s tests
```

Tests use a scripted Messages client and a fake `claude` executable on `PATH`. They exercise in-memory and stdio MCP fixtures. Native execution coverage requires host sandbox support.
