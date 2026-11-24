# Phase 17 handoff

Run the live benchmark at the checkpoint before phase 18. From the repository root, use Python 3.11 or newer, Node.js with recipe validation support, a supported Wyrit native sandbox, and a signed-in Claude Code subscription with `claude` on `PATH`. Optionally set `GITHUB_TOKEN` with Issues read access to `jej2k5/mcpxy-test`.

```sh
python -m pip install -e .
python -m bench calibrate --tasks bench/tasks --repetitions 3
```

Review correctness, errors, projected list-price equivalent, subscription usage, and wall time. After approving the projection, paste the printed calibration ID into `read` and press Enter.

```sh
read -r CALIBRATION_ID
python -m bench run --tasks bench/tasks --repetitions 3 --calibration-id "$CALIBRATION_ID"
```

Use matching settings and GitHub token availability for both commands. Phase 14 calibration IDs require a fresh calibration for this suite. If using API billing, set `ANTHROPIC_API_KEY`, review `bench/config.json`, and add `--driver api` to both commands.

Open `bench-results/run-*/report.md` for the review and `report.json` for raw metrics. Check correctness and eligible run counts before reading the cost and wall-time ratios. A ratio below 1 favors Wyrit for that metric. Use list-price cost for the go/no-go because cache-read tokens cost one tenth of standard input tokens. The recipe runs cover `dependent-chain`, `fan-out`, and `fan-out-large`, so use per-task ratios for like-for-like comparisons.

The loader regenerates both fan-out tasks in memory and verifies their canonical SHA-256 digests. To print the digests for a deliberate fixture update, run `python -m bench.generate_fan_out` and review the output against `bench/tasks/fan-out.sha256.json`.

In the console, configure `recipes_path` to enable **Save as recipe** from a successful Monitoring run with retained source. The form saves a pending proposal. Review capability declarations and approve separately on the Recipes page.

The build gate runs offline with a fake Claude Code driver and local MCP fixtures.

```sh
python -m unittest discover -s tests
```
