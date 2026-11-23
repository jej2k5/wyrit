"""Calibrate and explicitly authorize a benchmark session."""

import argparse
import json
import math
import os
from pathlib import Path
import sys
from uuid import uuid4

from .agent import APPROACHES, run_agent
from .clients import AnthropicMessages, github_snapshot
from .report import make_report, markdown
from .tasks import digest, github_fixture, load_tasks


ROOT = Path(__file__).resolve().parent


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="command", required=True)
    for command in ("calibrate", "run"):
        mode = modes.add_parser(command)
        mode.add_argument("--repetitions", type=positive_int, default=3)
        mode.add_argument("--model")
        mode.add_argument("--tasks", type=Path, default=ROOT / "tasks")
        mode.add_argument("--config", type=Path, default=ROOT / "config.json")
        mode.add_argument("--output", type=Path, default=Path("bench-results"))
        if command == "run":
            mode.add_argument("--calibration-id", required=True)
    return parser.parse_args(argv)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def make_plan(args, tasks, github_available):
    config = read_json(args.config)
    model = args.model if args.model is not None else config["model"]
    prices = config["prices_per_million"].get(model)
    if prices is None or any(type(prices.get(key)) not in (int, float)
                             or not math.isfinite(prices[key]) or prices[key] < 0
                             for key in ("input", "output")):
        raise ValueError("Configure finite, nonnegative input and output prices for the selected model")
    settings = {"model": model, "prices_per_million": prices}
    for name in ("max_turns", "max_tokens"):
        value = config[name]
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")
        settings[name] = value
    skipped = {task["id"]: "GITHUB_TOKEN is unset" for task in tasks
               if task["kind"] == "github" and not github_available}
    active = [task["id"] for task in tasks if task["id"] not in skipped]
    if not active:
        raise ValueError("No runnable tasks")
    return {"version": 1, "suite_digest": digest(tasks), "active_tasks": active,
            "skipped_tasks": skipped, "approaches": list(APPROACHES),
            "settings": settings, "repetitions": args.repetitions}


def run_suite(tasks, settings, repetitions, model):
    prices = settings["prices_per_million"]
    for repetition in range(1, repetitions + 1):
        for task in tasks:
            # Alternate the order across repetitions to reduce order effects.
            approaches = APPROACHES if repetition % 2 else APPROACHES[::-1]
            for approach in approaches:
                row = run_agent(task, approach, model, settings)
                cost = (row["input_tokens"] * prices["input"]
                        + row["output_tokens"] * prices["output"]) / 1_000_000
                yield {**row, "task": task["id"], "approach": approach,
                       "repetition": repetition, "cost_usd": cost}


def validate_projection(args, plan):
    projection_id = args.calibration_id
    if len(projection_id) != 64 or any(char not in "0123456789abcdef" for char in projection_id):
        raise ValueError("Invalid calibration ID")
    try:
        projection = read_json(args.output / "calibrations" / f"{projection_id}.json")
    except FileNotFoundError:
        raise ValueError("Calibration ID not found in the selected output directory") from None
    if digest(projection) != projection_id or projection["plan"] != plan:
        raise ValueError("Calibration does not match this benchmark plan")


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def main(argv=None, *, model_client=None, env=None, snapshot_loader=github_snapshot):
    args = parse_args(argv)
    env = os.environ if env is None else env
    try:
        tasks = load_tasks(args.tasks)
        github_token = env.get("GITHUB_TOKEN")
        plan = make_plan(args, tasks, bool(github_token))
        if args.command == "run":
            validate_projection(args, plan)
        if model_client is None:
            key = env.get("ANTHROPIC_API_KEY")
            if not key:
                raise ValueError("Set ANTHROPIC_API_KEY before running the benchmark")
            model_client = AnthropicMessages(key)

        for task_id, reason in plan["skipped_tasks"].items():
            print(f"Skipped {task_id} · {reason}")
        tasks = [task for task in tasks if task["id"] in plan["active_tasks"]]
        if any(task["kind"] == "github" for task in tasks):
            snapshot = snapshot_loader(github_token)
            tasks = [github_fixture(task, snapshot) if task["kind"] == "github" else task
                     for task in tasks]

        if args.command == "calibrate":
            rows = []
            for row in run_suite(tasks, plan["settings"], 1, model_client):
                rows.append(row)
                print(f"{row['task']} {row['approach']} · {row['input_tokens']} input + "
                      f"{row['output_tokens']} output tokens · ${row['cost_usd']:.8f} · "
                      f"correct={row['correct']}", flush=True)
            projected = sum(row["cost_usd"] for row in rows) * args.repetitions
            projection = {"plan": plan, "runs": rows, "projected_cost_usd": projected}
            projection_id = digest(projection)
            directory = args.output / "calibrations"
            directory.mkdir(parents=True, exist_ok=True)
            write_json(directory / f"{projection_id}.json", projection)
            print(f"Projected cost for {len(rows) * args.repetitions} runs · ${projected:.8f}")
            print(f"Calibration ID {projection_id}")
        else:
            rows = list(run_suite(tasks, plan["settings"], args.repetitions, model_client))
            report = make_report(plan, args.calibration_id, rows)
            args.output.mkdir(parents=True, exist_ok=True)
            directory = args.output / f"run-{uuid4().hex}"
            directory.mkdir()
            write_json(directory / "report.json", report)
            (directory / "report.md").write_text(markdown(report), encoding="utf-8")
            print(f"Reports written to {directory}")
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        print(f"bench: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
