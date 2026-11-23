# Measuring direct MCP against Wyrit

Run these commands from the checkout root with Python 3.11 or later. Install the project's dependencies and use Node.js on a host with a supported Wyrit native sandbox. Source `ANTHROPIC_API_KEY` through your usual secret manager. Optionally set `GITHUB_TOKEN` to a token with Issues read access to `jej2k5/mcpxy-test`.

```sh
python -m pip install -e .
python -m bench calibrate --repetitions 3
python -m bench run --repetitions 3 --calibration-id <CALIBRATION_ID>
```

Replace `<CALIBRATION_ID>` with the printed value, without angle brackets. Calibration executes each active task once per approach, prints the measured token cost, and projects the cost of the full run. Review its costs and correctness before authorizing the run. The run must use the same repetitions, model, prices, task data, and GitHub token availability as calibration. Repeat any `--model`, `--config`, `--tasks`, and `--output` options on both commands. The default output directory is `bench-results/`.

Edit `bench/config.json` to set the default Anthropic Messages model, turn and response token limits, and base USD prices per million input and output tokens. The estimate multiplies the observed one-run costs by repetitions and excludes the already-incurred calibration cost. Model behavior and live inputs can vary, so the projection is an estimate. Update the pricing table before calibrating when rates change. [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing)

## What gets measured

- Direct mode exposes downstream tool schemas to the model. Wyrit mode exposes its actual MCP `search` and `execute`. Both use a fresh agent context, a host-owned MCP session with the same inputs, and a read-only grant for downstream calls. Wyrit fails closed if its native sandbox is unavailable.
- Token totals sum the API's `input_tokens` and `output_tokens` on each turn. Wall time covers model requests and tool invocations through the last response. It excludes session setup, GitHub snapshot fetching, checking, and report writing. Tool calls count model-facing invocations. Downstream calls count requests received by the downstream server, excluding denied writes and discovery.
- Each completed session produces `report.json` with individual runs and `report.md` with the aggregate tables. Per-task and overall statistics show means, population variances, and Wyrit-minus-direct deltas. More tokens, turns, time, calls or cost and lower correctness are flagged as losses. JSON includes execution statuses and observed tool outcomes.
- With `GITHUB_TOKEN` set, the host fetches the five oldest available issues across open and closed states, excluding pull requests. It reads up to ten comments per issue, keeping the first 240 characters of each. Both agents read from the same in-memory MCP snapshot for that invocation. Calibration and the full run fetch fresh snapshots separately. Comment excerpts returned to the agent go to Anthropic as tool results. Without the token, the task is marked skipped. [GitHub issues](https://docs.github.com/en/rest/issues/issues#list-repository-issues) · [GitHub issue comments](https://docs.github.com/en/rest/issues/comments#list-issue-comments)

## Replace a task

Pass `--tasks path/to/tasks` to select a directory of JSON task files. Copy a bundled fixture and edit its unique `id`, `prompt`, and `tools`. Each tool has a description, an optional object `input_schema`, and `cases` mapping exact `arguments` objects to structured `result` objects. Calls with no matching result return an MCP tool error. A tool with `"write": true` is exposed for a simulated denial. Set `expected` to the exact JSON answer, and optionally require observed `required_requests` and `required_outcomes`. Object key order is ignored and array order is significant. Record representative read-only results from your own workflow as fixture cases to replay them across both approaches.

The built-in `github` task uses the fixed repository and takes a `contains` string for its case-insensitive checker. Update the prompt accordingly if you change the condition.

## Offline verification

```sh
python -m unittest discover -s tests
```

Benchmark tests use scripted model responses and in-memory MCP fixtures. The native execution test runs where the sandbox is available.
