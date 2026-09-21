"""Count every action the live driver posted, and how each was answered.

The full-run contract's seventh check is "every action was legal and acked", so
the refusal rate per action is the difference between a property of the player
and a coin flip on client timing.  This reads the recorded autoplay traces and
says which it is, per action, per batch.

    python scripts/census_action_refusals.py [--trace-glob 'runs/solver_supervisor/*/autoplay_trace.jsonl']
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

DEFAULT_GLOB = "runs/solver_supervisor/*/autoplay_trace.jsonl"


def _records(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def census(paths: list[Path]) -> dict[str, dict[str, int]]:
    """action -> {"posted", "refused"}, paired by trace order."""
    totals: dict[str, dict[str, int]] = {}
    for path in paths:
        pending: str | None = None
        for row in _records(path):
            kind = row.get("event_type")
            raw = row.get("raw")
            if not isinstance(raw, dict):
                continue
            if kind == "action":
                action = raw.get("action")
                pending = str(action) if action else None
            elif kind == "result" and pending is not None:
                cell = totals.setdefault(pending, {"posted": 0, "refused": 0})
                cell["posted"] += 1
                if raw.get("status") != "ok":
                    cell["refused"] += 1
                pending = None
    return totals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--trace-glob", default=DEFAULT_GLOB)
    args = parser.parse_args(argv)
    paths = sorted(Path().glob(args.trace_glob))
    if not paths:
        print(f"no traces matched {args.trace_glob}", file=sys.stderr)
        return 1
    totals = census(paths)
    print(f"{len(paths)} traces matched {args.trace_glob}")
    for action in sorted(totals, key=lambda a: (-totals[a]["refused"], a)):
        cell = totals[action]
        rate = cell["refused"] / cell["posted"] if cell["posted"] else 0.0
        print(
            f"{action:24s} posted={cell['posted']:5d} refused={cell['refused']:4d}"
            f" rate={rate:6.1%}"
        )
    refused_total = sum(c["refused"] for c in totals.values())
    posted_total = sum(c["posted"] for c in totals.values())
    print(f"TOTAL posted={posted_total} refused={refused_total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
