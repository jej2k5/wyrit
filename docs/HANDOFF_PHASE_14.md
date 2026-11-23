# Phase 14 handoff

From the repository root, use Python 3.11 or later and a signed-in Claude Code subscription with `claude` on `PATH`. Use Node.js on a macOS or Linux host with a supported Wyrit native sandbox. Optionally set `GITHUB_TOKEN` with Issues read access to `jej2k5/mcpxy-test`.

```sh
python -m pip install -e .
python -m bench calibrate --repetitions 3
```

Review the per-run correctness and errors and the projected list-price equivalent, subscription usage, and wall time. Paste the printed ID into `read` and press Enter to authorize the full run.

```sh
read -r CALIBRATION_ID
python -m bench run --repetitions 3 --calibration-id "$CALIBRATION_ID"
```

Reports are written to `bench-results/run-*/report.md` and `bench-results/run-*/report.json`. Use matching options and GitHub token availability for both commands. Pass `--model` to choose a model, `--timeout` to change the Claude Code timeout from 600 seconds, or `--tasks` to use a different suite of JSON task files. The GitHub task reads a bounded snapshot once per command so both approaches see the same data for that command. [Benchmark README](../bench/README.md)

For API billing, set `ANTHROPIC_API_KEY`, review the price table in `bench/config.json`, and run a separate calibration.

```sh
python -m bench calibrate --driver api --repetitions 3
read -r CALIBRATION_ID
python -m bench run --driver api --repetitions 3 --calibration-id "$CALIBRATION_ID"
```

Run the offline gate from the checkout root.

```sh
python -m unittest discover -s tests
```

The gate uses scripted Messages responses and a fake Claude Code executable with local MCP fixtures. Tests requiring native sandbox execution skip when the host cannot provide it.
