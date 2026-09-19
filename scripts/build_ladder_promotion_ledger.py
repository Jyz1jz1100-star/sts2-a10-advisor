"""What the ladder actually decided at each rung, read off the promotion records on disk.

The campaign report says the configured five-rung ladder never became a five-rung ladder, and that
has been argued from two weakly-related observations: `floor13` has no directory, and the act-1
arms carry no attestable parent link. Neither statement says what happened at `floor10`, which
does exist on disk with checkpoints and metrics -- including a file literally named
``early-promotion-step_*.json``. So the honest version of clause six needs the gate records, not an
inference from missing links.

This script reads every ``runs/curriculum_v2*`` stage directory, takes the evaluation numbers the
trainer itself recorded, and applies the thresholds from `config/training_v2.toml` for that stage:

* `promotion_evaluations` are the 500-episode records (the ones named `early-promotion-*` and any
  record whose episode count reaches the stage's `promotion_eval_episodes`); 100-episode checkpoint
  probes are reported separately as `checkpoint_probes` because they are not gate decisions;
* each evaluation is judged on the gate the config states -- `min_win_rate`, `min_wilson_lower`,
  `max_truncation_rate`, `max_illegal_actions`, `min_episodes` -- and passes only if every
  applicable test holds; where the record lacks a field the check is reported as `unknown`, not as
  a pass;
* configured rungs with no directory at all are listed with `run_present: false`, which is the
  `floor13` case;
* checkpoint digests are recomputed from the files so a reviewer can tie an evaluation record to
  the weights it scored, and the metrics files' own recorded checkpoint path is compared with the
  directory the record lives in (the campaign's earlier runs were copied between machine roots, so
  the absolute paths in those files are expected to differ and that mismatch is reported, not
  hidden).
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import collections
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from training.metrics import EvaluationMetrics  # noqa: E402
from training.promotion import decide_promotion  # noqa: E402
from training.v2_config import load_v2_training_config  # noqa: E402
from training.v2_curriculum import _promotion_config  # noqa: E402


def decide(record: dict, stage) -> dict:
    """Run the repo's own gate on a recorded evaluation, rather than re-implementing it.

    The first version of this script compared ``truncation_rate`` to the stage's
    ``max_truncation_rate`` and reported 8/8 failures. That was wrong twice over: the gate reads
    ``defect_truncation_rate`` (boundary hits at a stage's own floor are completions, not
    defects -- training/promotion.py:39-48), and it also tests ``boundary_rate`` against
    ``min_boundary_rate``. Calling ``decide_promotion`` keeps this ledger honest about which
    number the ladder actually uses.
    """
    fields = {field.name for field in dataclasses.fields(EvaluationMetrics)}
    missing = fields - set(record)
    metrics = EvaluationMetrics(**{key: value for key, value in record.items() if key in fields})
    decision = decide_promotion(metrics, _promotion_config(stage))
    return {"promoted": bool(decision.promoted),
            "reasons": list(decision.reasons),
            "observed": decision.observed,
            "required": decision.required,
            "fields_absent_from_the_record": sorted(missing)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config/training_v2.toml")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    config = load_v2_training_config(args.config.resolve())
    stages = list(config.stages)
    rows: list[dict] = []
    for stage in stages:
        run_dirs = sorted((ROOT / "runs").glob(f"curriculum_v2*/**/{stage.name}"))
        if not run_dirs:
            rows.append({"stage": stage.name, "run_present": False, "run_dirs": [],
                         "promotion_evaluations": [], "checkpoint_probes": 0,
                         "checkpoints": []})
            continue
        for run_dir in run_dirs:
            metrics = sorted((run_dir / "metrics").glob("*.json"))
            promotions, probes = [], []
            for path in metrics:
                record = json.loads(path.read_text(encoding="utf-8"))
                entry = {"file": str(path.relative_to(ROOT)),
                         "is_early_promotion_record": path.name.startswith("early-promotion"),
                         "episodes": record.get("episodes"),
                         "win_rate": record.get("win_rate"),
                         "wilson_lower": record.get("wilson_lower"),
                         "truncation_rate": record.get("truncation_rate"),
                         "illegal_actions": record.get("illegal_actions"),
                         "recorded_checkpoint": record.get("checkpoint"),
                         "steps": record.get("steps") or record.get("timesteps")}
                entry.update(decide(record, stage))
                is_gate = (entry["is_early_promotion_record"]
                           or (record.get("episodes") or 0) >= stage.promotion_eval_episodes)
                (promotions if is_gate else probes).append(entry)
            checkpoints = []
            for zip_path in sorted((run_dir / "checkpoints").glob("*.zip")):
                checkpoints.append({"file": str(zip_path.relative_to(ROOT)),
                                    "sha256": hashlib.sha256(zip_path.read_bytes()).hexdigest(),
                                    "bytes": zip_path.stat().st_size})
            rows.append({"stage": stage.name, "run_present": True,
                         "run_dir": str(run_dir.relative_to(ROOT)),
                         "promotion_evaluations": promotions,
                         "checkpoint_probes": len(probes),
                         "probe_episode_counts": sorted({p["episodes"] for p in probes}),
                         "checkpoints": checkpoints,
                         "gate_thresholds": {"min_win_rate": stage.min_win_rate,
                                             "min_wilson_lower": stage.min_wilson_lower,
                                             "max_truncation_rate": stage.max_truncation_rate,
                                             "max_illegal_actions": stage.max_illegal_actions,
                                             "min_episodes": stage.min_episodes,
                                             "promotion_eval_episodes": stage.promotion_eval_episodes}})

    payload = {
        "aggregates": {
            "configured_stages": [stage.name for stage in stages],
            "stages_with_no_run_at_all": sorted({row["stage"] for row in rows
                                                 if not row["run_present"]}),
            "stages_with_a_promotion_decision": sorted({row["stage"] for row in rows
                                                        if row.get("promotion_evaluations")}),
            "promotion_evaluations_total": sum(len(row.get("promotion_evaluations", []))
                                               for row in rows),
            "promotion_evaluations_promoting_total": sum(
                1 for row in rows for record in row.get("promotion_evaluations", [])
                if record["promoted"]),
            "gate_failure_reasons": dict(collections.Counter(
                reason.split(" ", 1)[0] for row in rows
                for record in row.get("promotion_evaluations", [])
                for reason in record["reasons"])),
            "checkpoints_recorded": sum(len(row.get("checkpoints", [])) for row in rows),
        },
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "not_established": [
            "that any of these decisions is still reachable by a reviewer on another machine -- "
            "runs/ is gitignored, so only this ledger's contents travel",
            "the parent of each act-1 arm, which is a separate question handled by "
            "ladder_lineage_20260919.json; a rung can be evaluated and still not be the parent",
            "anything about the shipped game; thresholds come from this repository's own config"],
        "rows": rows,
        "scope": ("every runs/curriculum_v2*/**/<configured stage> directory, judged against "
                  "config/training_v2.toml as it reads now"),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")
    agg = payload["aggregates"]
    print("configured:", agg["configured_stages"])
    print("no run at all:", agg["stages_with_no_run_at_all"],
          "| with a promotion decision:", agg["stages_with_a_promotion_decision"])
    print(f"promotion evaluations: {agg['promotion_evaluations_total']}, promoting: "
          f"{agg['promotion_evaluations_promoting_total']}, "
          f"reasons: {agg['gate_failure_reasons']}")
    for row in rows:
        for record in row.get("promotion_evaluations", []):
            print(f"  {row['stage']:8} {Path(record['file']).name[:38]:40} "
                  f"episodes={record['episodes']} win={record['win_rate']} "
                  f"defect_trunc={record['observed'].get('defect_truncation_rate')} "
                  f"promoted={record['promoted']} reasons={record['reasons'][:2]}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
