"""Aggregate raw runs and surface every metric on which Wyrit trails."""

from statistics import mean, pvariance

from .agent import APPROACHES
from .claude_code import COST_LABEL


METRICS = ("cost_usd", "wall_seconds", "input_tokens", "output_tokens", "model_turns",
           "tool_calls", "downstream_calls", "correct")
SUBSCRIPTION_METRICS = ("cache_creation_input_tokens", "cache_read_input_tokens",
                        "total_tokens", "is_error")
SUBSCRIPTION_NOTE = (
    "List-price cost is the headline measure. Claude Code includes fixed instructions in each approach. "
    "Cache-read tokens are priced at one tenth of input tokens, so total tokens overstate Wyrit's cost."
)


def summarize(rows, metrics):
    approaches = {}
    for approach in APPROACHES:
        runs = [row for row in rows if row["approach"] == approach and not row.get("permission_denials")]
        approaches[approach] = {
            "runs": len(runs),
            "metrics": {
                metric: {
                    "mean": mean(row[metric] for row in runs) if runs else None,
                    "variance": pvariance(row[metric] for row in runs) if runs else None,
                }
                for metric in metrics
            },
        }
    direct = approaches["direct"]["metrics"]
    wyrit = approaches["wyrit"]["metrics"]
    deltas = {
        metric: wyrit[metric]["mean"] - direct[metric]["mean"]
        if direct[metric]["mean"] is not None and wyrit[metric]["mean"] is not None else None
        for metric in metrics
    }
    return {
        "approaches": approaches,
        "ratios_vs_direct": {
            approach: {
                metric: approaches[approach]["metrics"][metric]["mean"] / direct[metric]["mean"]
                if direct[metric]["mean"] and approaches[approach]["runs"] else None
                for metric in ("cost_usd", "wall_seconds")
            }
            for approach in APPROACHES[1:]
        },
        "wyrit_minus_direct": deltas,
        "wyrit_losses": [metric for metric, delta in deltas.items()
                         if delta is not None and (delta < 0 if metric == "correct" else delta > 0)],
    }


def make_report(plan, projection_id, rows):
    subscription = plan["driver"] == "claude-code"
    metrics = METRICS + SUBSCRIPTION_METRICS if subscription else METRICS
    return {
        "plan": plan, "projection_id": projection_id, "runs": rows,
        "driver": plan["driver"],
        **({"cost_label": COST_LABEL, "comparison_note": SUBSCRIPTION_NOTE} if subscription else {}),
        "overall": summarize(rows, metrics),
        "per_task": {
            task: summarize([row for row in rows if row["task"] == task], metrics)
            for task in plan["active_tasks"]
        },
    }


def markdown(report):
    plan = report["plan"]
    lines = [
        "# Wyrit benchmark", "",
        f"Driver `{report['driver']}` · Model `{plan['settings']['model']}` · {plan['repetitions']} repetitions per task and approach",
        "", f"Calibration `{report['projection_id']}`", "",
        "Overall statistics pool eligible runs for each approach and may cover different tasks. "
        "Variance is population variance. "
        "Correctness means the fraction of runs that passed the task checker.", "",
    ]
    if "comparison_note" in report:
        lines.extend([report["comparison_note"], "", f"USD cost is {report['cost_label']}.", ""])
        lines.extend(["Runs with Claude Code permission denials are excluded from statistics.", ""])
    for task, reason in plan["skipped_tasks"].items():
        lines.extend([f"Skipped {task} · {reason}", ""])
    recipe_tasks = {row["task"] for row in report["runs"] if row["approach"] == "wyrit-recipe"}
    for name, group in [("Overall", report["overall"]), *report["per_task"].items()]:
        losses = ", ".join(group["wyrit_losses"]) or "none"
        comparison = (f"Wyrit trails on {losses}." if all(
            group["approaches"][approach]["runs"] for approach in ("direct", "wyrit")
        ) else "Comparison unavailable because one or both approaches have no eligible runs.")
        lines.extend([f"## {name}", "", comparison, ""])
        if name != "Overall":
            if name not in recipe_tasks:
                lines.extend(["Wyrit-recipe skipped because this task has no recipe.", ""])
            lines.extend([
                "| Approach | Cost / direct | Wall time / direct |",
                "| --- | ---: | ---: |",
            ])
            for approach, ratios in group["ratios_vs_direct"].items():
                cells = [f"{value:.3f}×" if value is not None else "—" for value in ratios.values()]
                lines.append(f"| {approach} | " + " | ".join(cells) + " |")
            lines.append("")
        counts = group["approaches"]
        lines.extend([
            "Eligible runs · " + " · ".join(f"{approach} {counts[approach]['runs']}"
                                            for approach in APPROACHES),
            "",
        ])
        lines.extend([
            "| Metric | Direct mean | Direct variance | Wyrit mean | Wyrit variance | "
            "Wyrit-recipe mean | Wyrit-recipe variance | Wyrit − direct |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ])
        for metric in group["wyrit_minus_direct"]:
            delta = group["wyrit_minus_direct"][metric]
            values = [counts[approach]["metrics"][metric][stat]
                      for approach in APPROACHES for stat in ("mean", "variance")]
            cells = [f"{value:.6g}" if value is not None else "—" for value in values]
            cells.append(f"{delta:+.6g}" if delta is not None else "—")
            lines.append(f"| {metric} | " + " | ".join(cells) + " |")
        lines.append("")
    failures = [row for row in report["runs"] if row.get("error")]
    if failures:
        lines.extend(["## Run errors", ""])
        for row in failures:
            reason = " ".join(row["error"].split())
            lines.append(f"- {row['task']} · {row['approach']} · repetition {row['repetition']} · {reason}")
        lines.append("")
    return "\n".join(lines)
