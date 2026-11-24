# Phase 17: Measure again

Extend the phase 14 benchmark so the operator can measure phases 15 and 16 against the same baseline. The build gate stays offline; the operator runs the real benchmark at the checkpoint before phase 18.

**Tasks.** Add two fixture tasks to `bench/tasks/`, generated deterministically by a small checked-in script (fixed seed) so the data can be regenerated:
- `fan-out`: a `list-tickets` tool returning 30 ticket IDs and a `get-ticket` tool returning each ticket's details (id, title, priority, status, hours_logged, a description of about 60 words and three comments of about 25 words each, roughly 1.3 KB per ticket). The prompt asks for the count and total `hours_logged` of tickets that are high priority and open; all 31 calls are required requests.
- `fan-out-large`: the same shape with 30 tickets of roughly 6 KB each (longer descriptions and ten comments), to show how the gap scales with record size. Keep Wyrit's default limits (32 calls, `max_output_bytes` 16384 per response); size the records so each response fits.

**A third approach, `wyrit-recipe`.** For any task that declares a `recipe` (name, description, input schema, source), the harness writes that recipe into the Wyrit config's `recipes_path` as an approved recipe (computing the approval digests exactly as phase 16 does) and runs the task with Wyrit plus the recipe. The agent's prompt is the task prompt unchanged; it must discover the recipe from the `execute` catalog and use `run_recipe`. Give `fan-out`, `fan-out-large` and `dependent-chain` recipes. Tasks without a recipe skip this approach. Reports show all three approaches side by side per task, and overall statistics per approach.

**Report.** Keep the phase 14 metrics and add, per task, Wyrit's and Wyrit-recipe's cost and wall-time ratios against direct, so the operator can read the go/no-go directly. List-price cost is the headline cost metric; note in the report that cache-read tokens are priced at a tenth of input tokens, so total tokens overstate Wyrit's cost.

**Operator run.** Calibration and authorization stay as in phase 14 (Claude Code driver by default). The handoff gives the exact commands, with `--tasks bench/tasks`.

Tests (offline, fake `claude` as in phase 14): the generator is deterministic and its output matches the checked-in tasks; `wyrit-recipe` writes an approved, runnable recipe and passes `run_recipe` through the allowed tools; tasks without recipes skip the approach; the report renders three approaches and the ratios. Produce the phase handoff.

**Recipes page polish (operator review of phase 16, 2026-11-23).** The operator approved phase 16; the flow and rules are right, the page is thin. In the same console, same design rules:
1. Each recipe row shows, without expanding: name, description, a clear write badge when any declared capability is a write, state (pending, runnable, unapproved, stale with the capability named), and run count and success rate from the ledger.
2. Fill the empty summary bar at the top of the page with counts by state and a "needs review" item when anything is pending, unapproved or stale.
3. **Save as recipe** from Monitoring: on a successful run whose source was captured (phase 13 stores generated code for 7 days), offer "Save as recipe", which opens a short form (name, description, argument schema prefilled as an empty object with a note that `args` is unused unless the operator edits the source) and submits it through the same validation as `propose_recipe`, landing as pending. Never approve in the same step.
4. Keep "recipe" as the working name throughout; the operator may rename the concept later, so keep the user-facing word in one place in the console code rather than scattered literals.
Tests: rows render description, write badge, state and stats; the summary counts match; Save as recipe creates a pending recipe from a captured run and is unavailable when the source was not captured or the run failed.

**Attempt 1 stopped (operator, 2026-11-23): UNREVIEWABLE_DELTA on `bench/tasks/06-fan-out-large.json`.** buildctl cannot review a changed file over about 100 KB, and the large fan-out is about 180 KB. Do not check in large generated task files. Check in the generator only; the harness generates `fan-out` and `fan-out-large` at load time from the fixed seed into memory (or a temporary directory), and `load_tasks` verifies each generated task against a checked-in SHA-256 digest so the data cannot drift silently. Every checked-in file in this phase must stay well under 100 KB. The small `fan-out` may still be checked in if it stays under that size, but generating both the same way is simpler. Tests assert the generator's digest and that no file under `bench/tasks/` exceeds 64 KB.
