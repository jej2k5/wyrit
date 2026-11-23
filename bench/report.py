"""Aggregate raw runs and surface every metric on which Wyrit trails."""

from statistics import mean, pvariance


METRICS = ("input_tokens", "output_tokens", "model_turns", "wall_seconds",
           "tool_calls", "downstream_calls", "cost_usd", "correct")


def summarize(rows):
    approaches = {}
    for approach in ("direct", "wyrit"):
        runs = [row for row in rows if row["approach"] == approach]
        approaches[approach] = {
            "runs": len(runs),
            "metrics": {
                metric: {
                    "mean": mean(row[metric] for row in runs),
                    "variance": pvariance(row[metric] for row in runs),
                }
                for metric in METRICS
            },
        }
    direct = approaches["direct"]["metrics"]
    wyrit = approaches["wyrit"]["metrics"]
    deltas = {metric: wyrit[metric]["mean"] - direct[metric]["mean"] for metric in METRICS}
    return {
        "approaches": approaches,
        "wyrit_minus_direct": deltas,
        "wyrit_losses": [metric for metric, delta in deltas.items()
                         if (delta < 0 if metric == "correct" else delta > 0)],
    }


def make_report(plan, projection_id, rows):
    return {
        "plan": plan, "projection_id": projection_id, "runs": rows,
        "overall": summarize(rows),
        "per_task": {
            task: summarize([row for row in rows if row["task"] == task])
            for task in plan["active_tasks"]
        },
    }


def markdown(report):
    plan = report["plan"]
    lines = [
        "# Wyrit benchmark", "",
        f"Model `{plan['settings']['model']}` · {plan['repetitions']} repetitions per task and approach",
        "", f"Calibration `{report['projection_id']}`", "",
        "Overall statistics pool all completed runs. Variance is population variance. "
        "Correctness means the fraction of runs that passed the task checker.", "",
    ]
    for task, reason in plan["skipped_tasks"].items():
        lines.extend([f"Skipped {task} · {reason}", ""])
    for name, group in [("Overall", report["overall"]), *report["per_task"].items()]:
        losses = ", ".join(group["wyrit_losses"]) or "none"
        lines.extend([
            f"## {name}", "", f"Wyrit trails on {losses}.", "",
            "| Metric | Direct mean | Direct variance | Wyrit mean | Wyrit variance | Wyrit − direct |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ])
        for metric in METRICS:
            direct = group["approaches"]["direct"]["metrics"][metric]
            wyrit = group["approaches"]["wyrit"]["metrics"][metric]
            delta = group["wyrit_minus_direct"][metric]
            lines.append(f"| {metric} | {direct['mean']:.6g} | {direct['variance']:.6g} | "
                         f"{wyrit['mean']:.6g} | {wyrit['variance']:.6g} | {delta:+.6g} |")
        lines.append("")
    return "\n".join(lines)
