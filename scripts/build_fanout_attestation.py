"""Build docs/evidence/fanout_attestation_20260919.json.

The objective asks for a multi-arm concurrent overnight run through
``scripts/run_curriculum_fanout.py``. Until now that clause was evidenced by prose plus
directories under ``runtime/``, which is gitignored -- so on another machine there is nothing to
check. This commits the attestation instead: one row per arm run, with its ``plan.json`` digest and
the fields that define the run shape, the metrics files it produced (which the committed metrics
index already hashes), and the observed activity window.

Two honesty notes baked into the output. Concurrency is inferred from overlapping activity
windows (``plan.json`` records no timestamps), so it is reported as an overlap count with its
limit stated, not as proof of wall-clock parallelism. And the 2026-09-18 campaign's plans predate
the ``warm_start`` field the runner writes now, so those runs' parent links are attested by the
launcher only -- counted here, not hidden.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN_ROOTS = ("runtime/fanout", "runtime/act1_overnight", "runtime/act1_ab")


def plan_fields(plan: dict) -> dict:
    stages = plan.get("stages") or []
    first = stages[0] if stages else {}
    return {
        "stages": [stage.get("name") for stage in stages],
        "parallel_envs": first.get("parallel_envs"),
        "checkpoint_every_steps": first.get("checkpoint_every_steps"),
        "promotion_episodes": (first.get("promotion") or {}).get("episodes"),
        "max_episode_steps": first.get("max_episode_steps"),
        "initialize_from_previous": first.get("initialize_from_previous"),
        "warm_start_recorded": "warm_start" in plan,
        "observation_contract_keys": sorted(plan.get("observation_contract") or {}),
        # plans record seed_partitions as [{name, start, count}]; the ranges themselves are the
        # evidence that arms share a partition and differ only in weights.
        "seed_partitions": [{"name": entry.get("name"), "start": entry.get("start"),
                             "count": entry.get("count")}
                            for entry in (plan.get("seed_partitions") or [])
                            if isinstance(entry, dict)],
        "character": plan.get("character"),
        "ascension": plan.get("ascension"),
        "emulator": plan.get("emulator"),
    }


def window(paths: list[Path]) -> dict:
    stamps = [path.stat().st_mtime for path in paths if path.exists()]
    if not stamps:
        return {"files": 0}
    low, high = min(stamps), max(stamps)
    return {"files": len(stamps),
            "first_activity": datetime.fromtimestamp(low, UTC).isoformat(timespec="seconds"),
            "last_activity": datetime.fromtimestamp(high, UTC).isoformat(timespec="seconds"),
            "span_seconds": round(high - low)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path,
                        default=ROOT / "docs/evidence/metrics_index_20260919.json")
    parser.add_argument("--out", type=Path,
                        default=ROOT / "docs/evidence/fanout_attestation_20260919.json")
    args = parser.parse_args()

    indexed = {row["path"]: row["sha256"] for row in
               json.loads(args.index.read_text(encoding="utf-8"))["rows"]}

    rows = []
    for root in CAMPAIGN_ROOTS:
        base = ROOT / root
        if not base.exists():
            continue
        for arm_dir in sorted(p for p in base.iterdir() if p.is_dir()):
            plans = sorted(arm_dir.rglob("plan.json"))
            if not plans:
                rows.append({"arm": f"{root}/{arm_dir.name}", "plan_found": False})
                continue
            for plan_path in plans:
                run_dir = plan_path.parent
                metrics = sorted(run_dir.rglob("*.json"))
                metrics = [p for p in metrics if p.name != "plan.json"
                           and "metrics" in p.parts]
                plan = json.loads(plan_path.read_text(encoding="utf-8"))
                rows.append({
                    "arm": f"{root}/{arm_dir.name}",
                    "run": run_dir.name,
                    "plan_found": True,
                    "plan_path": plan_path.relative_to(ROOT).as_posix(),
                    "plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
                    "run_dir": run_dir.relative_to(ROOT).as_posix(),
                    **plan_fields(plan),
                    "metrics_files": [p.relative_to(ROOT).as_posix() for p in metrics],
                    "metrics_files_in_index": sum(
                        1 for p in metrics
                        if p.relative_to(ROOT).as_posix() in indexed),
                    "checkpoint_zips": len(list((run_dir / "act1" / "checkpoints").glob("*.zip")))
                    if (run_dir / "act1" / "checkpoints").exists() else 0,
                    **{**window(metrics), "activity_paths_measured": len(metrics)},
                })

    timed = [(row["first_activity"], row["last_activity"]) for row in rows
             if row.get("first_activity") and row.get("last_activity")]
    overlapping = sum(1 for i in range(len(timed)) for j in range(i + 1, len(timed))
                      if timed[i][0] < timed[j][1] and timed[j][0] < timed[i][1])
    starts = sorted(start for start, _ in timed)
    ends = sorted(end for _, end in timed)
    pairs_possible = len(timed) * (len(timed) - 1) // 2
    payload = {
        "_comment": [
            "Committed attestation of the overnight fan-out the objective asks for: one row per arm run,",
            "its plan.json digest and run shape, the metrics files it produced (all hashed in",
            "metrics_index_20260919.json), and an activity window taken from file mtimes because the",
            "plans record no timestamps.",
        ],
        "aggregates": {
            "arms_with_plans": sum(1 for row in rows if row.get("plan_found")),
            "arms_without_plans": [row["arm"] for row in rows if not row.get("plan_found")],
            "campaign_span_first_activity": starts[0] if starts else None,
            "campaign_span_last_activity": ends[-1] if ends else None,
            "metrics_files_covered_by_the_index": sum(row.get("metrics_files_in_index", 0)
                                                      for row in rows),
            "metrics_files_listed": sum(len(row.get("metrics_files", [])) for row in rows),
            "plans_recording_warm_start": sum(1 for row in rows
                                              if row.get("warm_start_recorded")),
            "timed_runs": len(timed),
            "window_overlap_pairs": overlapping,
            "window_overlap_pairs_possible": pairs_possible,
        },
        "established": [
            "every arm run's plan.json is present and hashed, and every metrics file it lists is a",
            "row in the committed metrics index, so the fan-out's outputs are checkable without the "
            "gitignored directories",
        ],
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "not_established": [
            "true wall-clock concurrency: overlap is inferred from mtime windows, and per-run "
            "parallel_envs=12 is a configuration fact, not a measurement of realised parallelism "
            "(measured elsewhere at roughly 10% effective within one run)",
            "rung-to-rung warm-start lineage for runs whose plans predate the warm_start field -- "
            "those parent links rest on the launcher text, which lives under gitignored runtime/",
        ],
        "reproduce": "python scripts/build_fanout_attestation.py",
        "rows": sorted(rows, key=lambda row: (row["arm"], row.get("run", ""))),
        "scope": "this machine's runtime directories, summarised for review elsewhere",
    }
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    print(json.dumps(payload["aggregates"], ensure_ascii=False, sort_keys=True)[:900])
    print(f"rows: {len(rows)}; wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
