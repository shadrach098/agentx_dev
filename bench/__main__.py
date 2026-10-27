"""``python -m bench``: run the agentx benchmark, list its tasks, or compare two runs.

    python -m bench list
    python -m bench run --model gpt-6.1-sol                 # every task (asks first: it uses your API key)
    python -m bench run --model gpt-6.1-sol --task audit --task team-job
    python -m bench compare bench/results/A.json bench/results/B.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

from bench.tasks import BY_ID, TASKS

RESULTS = Path(__file__).resolve().parent / "results"


def _short(count: float) -> str:
    count = float(count)
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}m"
    if count >= 1000:
        return f"{count / 1000:.0f}k"
    return f"{count:.0f}"


def table(rows: List[Dict[str, Any]]) -> str:
    head = f"{'task':<20} {'result':<6} {'time':>7} {'reqs':>5} {'tokens':>7} {'cached':>7} {'counted':>8} {'tools':>6}  note"
    lines = [head, "-" * len(head)]
    for row in rows:
        cached = f"{row['cached'] * 100 // max(1, row['tokens_in'])}%" if row["tokens_in"] else "-"
        tools = row["tool_calls"] + row["team_tool_uses"]
        lines.append(f"{row['task']:<20} {'PASS' if row['passed'] else 'FAIL':<6} {row['seconds']:>6.0f}s "
                     f"{row['requests']:>5} {_short(row['tokens_in'] + row['tokens_out']):>7} {cached:>7} "
                     f"{_short(row['counted']):>8} {tools:>6}  {row['note'][:70]}")
    passed = sum(1 for row in rows if row["passed"])
    total = {key: sum(row[key] for row in rows) for key in ("seconds", "requests", "tokens_in", "tokens_out",
                                                             "cached", "counted")}
    lines.append("-" * len(head))
    lines.append(f"{f'{passed}/{len(rows)} passed':<27} {total['seconds']:>6.0f}s {total['requests']:>5} "
                 f"{_short(total['tokens_in'] + total['tokens_out']):>7} "
                 f"{(total['cached'] * 100 // max(1, total['tokens_in'])):>6}% {_short(total['counted']):>8}")
    return "\n".join(lines)


def cmd_list(args: argparse.Namespace) -> int:
    for task in TASKS:
        print(f"{task.id:<20} {task.repo:<6} {task.what:<32} budget {_short(task.budget)}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from bench.runner import git_commit, run_task
    chosen = [BY_ID[name] for name in args.task] if args.task else list(TASKS)
    unknown = [name for name in args.task or [] if name not in BY_ID]
    if unknown:
        print(f"unknown task: {', '.join(unknown)} (python -m bench list)", file=sys.stderr)
        return 2
    budget = sum(task.budget for task in chosen) * args.repeat
    if not args.yes:
        print(f"This runs {len(chosen)} task(s) x {args.repeat} on {args.model} with your API key. Each task is "
              f"wrapped up at its token budget (together about {_short(budget)} tokens at most, cached input "
              "counted as a tenth).")
        if input("Go ahead? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing was run.")
            return 1
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = RESULTS / f"{stamp}-{args.model}"
    rows: List[Dict[str, Any]] = []
    for round_number in range(args.repeat):
        for task in chosen:
            print(f"[{len(rows) + 1}/{len(chosen) * args.repeat}] {task.id} ...", flush=True)
            result = run_task(task, args.model, log_dir=run_dir / f"round-{round_number + 1}", keep=args.keep)
            rows.append(result.as_dict())
            print(f"    {'PASS' if result.passed else 'FAIL'} - {result.note[:100]}", flush=True)
    report = {"model": args.model, "started": stamp, "commit": git_commit(), "repeat": args.repeat, "tasks": rows}
    path = run_dir.parent / f"{run_dir.name}.json"     # not with_suffix: "gpt-6.1-sol" would lose ".1-sol"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print()
    print(table(rows))
    print(f"\nSaved {path} (logs and diffs in {run_dir})")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    old = json.loads(Path(args.old).read_text(encoding="utf-8"))
    new = json.loads(Path(args.new).read_text(encoding="utf-8"))
    print(f"old: {old['model']} @ {old.get('commit') or '?'} ({old['started']})")
    print(f"new: {new['model']} @ {new.get('commit') or '?'} ({new['started']})\n")

    def by_task(report: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
        rows: Dict[str, List[Dict[str, Any]]] = {}
        for row in report["tasks"]:
            rows.setdefault(row["task"], []).append(row)
        return rows

    before, after = by_task(old), by_task(new)
    print(f"{'task':<20} {'passed':>13} {'counted tokens':>22} {'time':>16}")
    for task in sorted(set(before) | set(after)):
        a, b = before.get(task, []), after.get(task, [])

        def rate(rows):
            return f"{sum(r['passed'] for r in rows)}/{len(rows)}" if rows else "-"

        def mean(rows, key):
            return sum(r[key] for r in rows) / len(rows) if rows else 0

        change = ""
        if a and b and mean(a, "counted"):
            change = f"{(mean(b, 'counted') - mean(a, 'counted')) * 100 / mean(a, 'counted'):+.0f}%"
        print(f"{task:<20} {rate(a):>6} -> {rate(b):<5} {_short(mean(a, 'counted')):>7} -> "
              f"{_short(mean(b, 'counted')):<6} {change:>6} {mean(a, 'seconds'):>6.0f}s -> {mean(b, 'seconds'):.0f}s")
    return 0


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bench", description="The agentx benchmark.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="the tasks").set_defaults(run=cmd_list)
    run = sub.add_parser("run", help="run tasks against a model (uses your API key)")
    run.add_argument("--model", required=True, help="the model agentx uses, for example gpt-6.1-sol")
    run.add_argument("--task", action="append", help="one task (repeatable); default every task")
    run.add_argument("--repeat", type=int, default=1, help="run each task this many times (models vary)")
    run.add_argument("--yes", action="store_true", help="do not ask before running")
    run.add_argument("--keep", action="store_true", help="keep each task's repo copy in the temp folder")
    run.set_defaults(run=cmd_run)
    compare = sub.add_parser("compare", help="compare two saved runs")
    compare.add_argument("old")
    compare.add_argument("new")
    compare.set_defaults(run=cmd_compare)
    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
