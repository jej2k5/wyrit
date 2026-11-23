"""Aggregate raw runs and surface every metric on which Wyrit trails."""

from statistics import mean, pvariance

from .claude_code import COST_LABEL


METRICS = ("input_tokens", "output_tokens", "model_turns", "wall_seconds",
           "tool_calls", "downstream_calls", "cost_usd", "correct")
SUBSCRIPTION_METRICS = ("cache_creation_input_tokens", "cache_read_input_tokens",
                        "total_tokens", "is_error")
SUBSCRIPTION_NOTE = (
    "Claude Code includes the same fixed instructions in both approaches. "
    "Compare the differences between approaches when interpreting token counts."
)


def summarize(rows, metrics):
    approaches = {}
    for approach in ("direct", "wyrit"):
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
        "Overall statistics pool eligible runs. Variance is population variance. "
        "Correctness means the fraction of runs that passed the task checker.", "",
    ]
    if "comparison_note" in report:
        lines.extend([report["comparison_note"], "", f"USD cost is {report['cost_label']}.", ""])
        lines.extend(["Runs with Claude Code permission denials are excluded from statistics.", ""])
    for task, reason in plan["skipped_tasks"].items():
        lines.extend([f"Skipped {task} · {reason}", ""])
    for name, group in [("Overall", report["overall"]), *report["per_task"].items()]:
        losses = ", ".join(group["wyrit_losses"]) or "none"
        comparison = (f"Wyrit trails on {losses}." if all(
            approach["runs"] for approach in group["approaches"].values()
        ) else "Comparison unavailable because one or both approaches have no eligible runs.")
        lines.extend([
            f"## {name}", "", comparison, "",
            "| Metric | Direct mean | Direct variance | Wyrit mean | Wyrit variance | Wyrit − direct |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ])
        for metric in group["wyrit_minus_direct"]:
            direct = group["approaches"]["direct"]["metrics"][metric]
            wyrit = group["approaches"]["wyrit"]["metrics"][metric]
            delta = group["wyrit_minus_direct"][metric]
            values = [direct["mean"], direct["variance"], wyrit["mean"], wyrit["variance"]]
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
