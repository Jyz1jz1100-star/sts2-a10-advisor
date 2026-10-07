"""Count refused posts per run and per refusal class, for a live batch trace.

The acceptance contract allows zero illegal actions, and the assessor counts every refused POST
there, so a fix to the driver has to be measured the same way: per run, not per batch, and by
mechanism, not as one number. Two classes that look identical in a total are different findings --
"screen was already gone when we posted" (ours to prevent) and "the client refused a screen we
legitimately saw open one poll earlier" (a race to name and quantify).

    .tools/python/.../python.exe scripts/measure_post_refusals.py \
        runs/solver_supervisor/<batch>/autoplay_trace.jsonl
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Refusal wording the bridge emits, mapped to what it says about who was wrong.
CLASSES = (
    (re.compile(r"Stale decision", re.I), "decision_id_moved"),
    (re.compile(r"is not open|no .*screen is open", re.I), "screen_already_gone"),
    (re.compile(r"Unknown menu option", re.I), "menu_option_not_accepted"),
    (re.compile(r"not enough gold|cannot afford", re.I), "affordability"),
)


def classify(message: str) -> str:
    for pattern, name in CLASSES:
        if pattern.search(message or ""):
            return name
    return "other"


def measure(trace: pathlib.Path) -> dict:
    run_id = None
    per_run: dict[str, Counter] = {}
    order: list[str] = []
    with trace.open(encoding="utf-8") as handle:
        lines = list(handle)
    for line in lines:
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        kind = row.get("event_type")
        raw = row.get("raw") or {}
        if kind == "run_identity":
            run_id = str(raw.get("run_id") or "unknown")
        elif kind == "game_over" or (kind == "state" and raw.get("state_type") == "game_over"):
            run_id = run_id or "unknown"
        if kind not in ("action", "result"):
            continue
        bucket = run_id or "PRE_RUN"
        if bucket not in per_run:
            per_run[bucket] = Counter()
            order.append(bucket)
        tally = per_run[bucket]
        if kind == "action":
            tally["posted"] += 1
        else:
            status = str(raw.get("status") or "")
            if status in ("ok", "accepted"):
                tally["accepted"] += 1
            else:
                tally["refused"] += 1
                tally[f"class:{classify(str(raw.get('error') or raw.get('message')))}"] += 1
                tally[f"msg:{str(raw.get('error'))[:60]}"] += 1
    runs = []
    for key in order:
        tally = per_run[key]
        runs.append({
            "run_id": key,
            "posted": tally["posted"],
            "accepted": tally["accepted"],
            "refused": tally["refused"],
            "refusal_classes": {k[len("class:"):]: v for k, v in sorted(tally.items())
                                if k.startswith("class:")},
            "refusal_messages": {k[len("msg:"):]: v for k, v in sorted(tally.items())
                                 if k.startswith("msg:")},
        })
    return {
        "trace": trace.relative_to(ROOT).as_posix() if trace.is_relative_to(ROOT)
        else trace.as_posix(),
        "runs": len(runs),
        "posted_total": sum(row["posted"] for row in runs),
        "refused_total": sum(row["refused"] for row in runs),
        "runs_with_zero_refusals": sum(1 for row in runs if row["refused"] == 0),
        "per_run": runs,
        "not_established": [
            "that a refusal-free run is achievable from these counts alone: the contract is per run,"
            " and this report only says which runs were clean and which classes appeared",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=pathlib.Path, nargs="+")
    parser.add_argument("--out", type=pathlib.Path, default=None)
    args = parser.parse_args()
    reports = [measure(path) for path in args.trace]
    payload = reports[0] if len(reports) == 1 else {"batches": reports}
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    for report in reports:
        print(f"{report['trace']}: runs {report['runs']}  posted {report['posted_total']}"
              f"  refused {report['refused_total']}"
              f"  clean runs {report['runs_with_zero_refusals']}/{report['runs']}")
        for row in report["per_run"]:
            if row["refused"]:
                print(f"   {row['run_id']}: {row['refused']}/{row['posted']} refused"
                      f" {json.dumps(row['refusal_classes'], ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
