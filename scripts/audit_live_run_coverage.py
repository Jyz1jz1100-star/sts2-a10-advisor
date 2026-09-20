"""Replay a recorded live bridge trace and report what each run actually covered.

The batch summary counts actions; it has never been able to answer "did this run
meet each act's Ancient, fight 1+1+2 bosses, and end on the game's victory
screen?".  This reads a recorded ``autoplay_trace.jsonl`` -- the same state stream
the driver saw -- and answers it per run, with no live game required.

    python scripts/audit_live_run_coverage.py runs/.../autoplay_trace.jsonl \
        --out docs/evidence/live_run_coverage_20260920.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bridge.run_progress import RunCoverage  # noqa: E402


def _states(path: Path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("event_type") == "state" and isinstance(record.get("raw"), dict):
                yield record["raw"]


def _seal(coverage: RunCoverage, runs: list[dict[str, Any]], reason: str) -> None:
    if not coverage.acts_seen:
        return
    row = coverage.coverage()
    row["sealed_by"] = reason
    runs.append(row)


def audit(path: Path) -> dict[str, Any]:
    coverage = RunCoverage()
    runs: list[dict[str, Any]] = []
    screen_counts: Counter[str] = Counter()
    for state in _states(path):
        state_type = str(state.get("state_type") or "unknown")
        screen_counts[state_type] += 1
        if coverage.closed:
            # A finished run's screen keeps repeating on every poll; only begin a
            # new ledger once the game is actually showing something else.
            if state_type in ("menu", "game_over"):
                continue
            _seal(coverage, runs, "game_over")
            coverage = RunCoverage()
        elif state_type == "menu" and coverage.acts_seen:
            # Gameplay then the menu, with no terminal screen between: the run
            # was abandoned or the recording was cut, and it must not be merged
            # into whichever run is started next.
            _seal(coverage, runs, "menu_without_terminal")
            coverage = RunCoverage()
        coverage.observe(state)
    if not coverage.closed:
        # The recording stopped mid-run: abandoned, or the batch was cut short.
        _seal(coverage, runs, "trace_end")
    return {
        "trace": str(path),
        "state_events_by_screen": dict(sorted(screen_counts.items())),
        "runs": runs,
        "run_count": len(runs),
        "runs_reaching_act_3": sum(1 for row in runs if 3 in row["acts_seen"]),
        "runs_with_certified_clear": sum(1 for row in runs if row["run_complete"]),
        "victory_evidence_available": any(
            row["outcome"] is True
            and row["outcome_source"] == "bridge_is_victory_flag"
            for row in runs
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("traces", type=Path, nargs="+")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    report = {"schema_version": 1, "traces": [audit(p) for p in args.traces]}
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
