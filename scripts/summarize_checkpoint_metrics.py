"""Aggregate checkpoint evaluation JSONs for one curriculum stage into a table.

    python scripts/summarize_checkpoint_metrics.py \
        runs/curriculum/<run>/act1 [--csv]

Prints one row per metrics file (checkpoint probes, early promotion, final
promotion), ordered by the step count embedded in the filename where possible,
with the gate-relevant columns: win rate, Wilson 95% lower bound, truncation
rate, illegal actions, mean final floor, and episode count. Split and
experimental fields are passed through so simulator records can never be
confused with real-game acceptance numbers.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

STEP_RE = re.compile(r"step_(\d{12})")

COLUMNS = (
    "name",
    "split",
    "scope",
    "experimental",
    "episodes",
    "win_rate",
    "wilson_95_low",
    "wilson_95_high",
    "truncation_rate",
    "illegal_actions",
    "mean_final_floor",
    "mean_steps",
    "generated_at",
)


def load_rows(stage_dir: Path) -> list[dict[str, str]]:
    metrics_dir = stage_dir / "metrics"
    if not metrics_dir.is_dir():
        return []
    rows: list[tuple[int, dict[str, str]]] = []
    for path in sorted(metrics_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"warning: {path}: {exc}", file=sys.stderr)
            continue
        if not isinstance(data, dict) or "win_rate" not in data:
            continue

        def number(key: str, digits: int = 4) -> str:
            value = data.get(key)
            return "-" if value is None else f"{float(value):.{digits}f}"

        step_match = STEP_RE.search(path.name)
        order = int(step_match.group(1)) if step_match else 10**15
        rows.append(
            (
                order,
                {
                    "name": path.name,
                    "split": str(data.get("split", "?")),
                    "scope": str(data.get("scope", "?")),
                    "experimental": str(data.get("experimental", "?")),
                    "episodes": str(data.get("episodes", "-")),
                    "win_rate": number("win_rate"),
                    "wilson_95_low": number("wilson_95_low"),
                    "wilson_95_high": number("wilson_95_high"),
                    "truncation_rate": number("truncation_rate"),
                    "illegal_actions": str(data.get("illegal_actions", "-")),
                    "mean_final_floor": number("mean_final_floor", 2),
                    "mean_steps": number("mean_steps", 1),
                    "generated_at": str(data.get("generated_at", ""))[:19],
                },
            )
        )
    rows.sort(key=lambda item: (item[0], item[1]["name"]))
    return [row for _, row in rows]


def print_decisions(stage_dir: Path) -> None:
    for name in ("early-promotion-decision.json", "promotion_decision.json"):
        path = stage_dir / name
        if not path.is_file():
            continue
        try:
            decision = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"warning: {path}: {exc}", file=sys.stderr)
            continue
        print(
            f"{name}: promoted={decision.get('promoted')} "
            f"reasons={decision.get('reasons')}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage_dir", type=Path)
    parser.add_argument("--csv", action="store_true")
    args = parser.parse_args()
    if not args.stage_dir.is_dir():
        print(f"missing directory: {args.stage_dir}", file=sys.stderr)
        return 2
    rows = load_rows(args.stage_dir)
    if args.csv:
        writer = csv.DictWriter(sys.stdout, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    else:
        if not rows:
            print("(no metrics yet)")
        else:
            widths = {key: max(len(key), *(len(row[key]) for row in rows)) for key in COLUMNS}
            header = "  ".join(key.ljust(widths[key]) for key in COLUMNS)
            print(header)
            for row in rows:
                print("  ".join(row[key].ljust(widths[key]) for key in COLUMNS))
    print_decisions(args.stage_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
