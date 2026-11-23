# Phase 14 handoff

From the repository root, install the project dependencies and load `ANTHROPIC_API_KEY` into the environment. Use Node.js on a host with a supported Wyrit native sandbox. Set `GITHUB_TOKEN` with Issues read access to `jej2k5/mcpxy-test` to include the GitHub task.

```sh
python -m pip install -e .
python -m bench calibrate --repetitions 3
```

Review the per-run cost and correctness and the projected total. Paste the printed ID when prompted.

```sh
read -r CALIBRATION_ID
python -m bench run --repetitions 3 --calibration-id "$CALIBRATION_ID"
```

The full run writes Markdown and JSON reports under `bench-results/run-*/`. Use the same options and GitHub token availability for both commands. The estimate uses the editable price table in `bench/config.json`. Pass `--model` to choose a model from that table or `--tasks` to point at a directory of task files. The benchmark README covers the data format and metric definitions. [Benchmark README](../bench/README.md)

The GitHub task reads a bounded live snapshot once for each command. Both agents receive the same issue and comment data through the downstream MCP fixture. GitHub fetch time is outside the timed comparison.

```sh
python -m unittest discover -s tests
```

The offline gate uses a scripted Messages client and in-memory MCP servers. Native execution coverage depends on host support.
