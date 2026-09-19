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
    metrics = EvaluationMetrics.from_payload(record)
    decision = decide_promotion(metrics, _promotion_config(stage))
    assert metrics.absent_metrics == frozenset(missing - {"absent_metrics"}), (
        f"{record.get('checkpoint')}：the gate and this ledger disagree about which "
        "keys the record omitted, so the defaults either of them trusted are unknown"
    )
    return {"promoted": bool(decision.promoted),
            "reasons": list(decision.reasons),
            "observed": decision.observed,
            "required": decision.required,
            "unrecorded_clauses_the_gate_refused": [
                reason for reason in decision.reasons if "was not recorded" in reason],
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
                         "promotion_evaluations": [], "live_decision": None,
                         "checkpoint_probes": 0, "checkpoints": []})
            continue
        for run_dir in run_dirs:
            # Three different populations live under runs/curriculum_v2*: the campaign's ladder,
            # a run the operator stopped and named ``aborted-...``, and the smoke ladders whose
            # config asks for 30 episodes at a boundary rate of 0.0.  A smoke promotion is not a
            # rung of the campaign's ladder, and an aborted run must not decide whether floor6
            # promoted, so each row is labelled by which of the three it came from.
            parts = run_dir.relative_to(ROOT).parts
            lowered = " ".join(part.lower() for part in parts)
            run_kind = ("aborted" if "abort" in lowered
                        else "smoke" if "smoke" in lowered
                        else "campaign")
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
            # The decision the trainer recorded when it ran is a different artifact from the
            # metrics it ran on, and it carries the thresholds that were in force that day.
            # Re-judging an old record against today's config answers "would this promote now",
            # which is not the question "what did the ladder decide then".
            decision_path = run_dir / "promotion_decision.json"
            live = None
            if decision_path.is_file():
                recorded = json.loads(decision_path.read_text(encoding="utf-8"))
                then = recorded.get("required") or {}
                now = {k: v for k, v in dataclasses.asdict(_promotion_config(stage)).items()
                       if k in {"min_episodes", "min_win_rate", "min_wilson_lower",
                                "max_truncation_rate", "max_illegal_actions",
                                "min_boundary_rate", "min_boundary_wilson_lower"}}
                rejudged = [r["promoted"] for r in promotions
                            if (r["episodes"] or 0) >= stage.promotion_eval_episodes]
                live = {
                    "file": str(decision_path.relative_to(ROOT)),
                    "promoted_when_it_ran": bool(recorded.get("promoted")),
                    "reasons_when_it_ran": list(recorded.get("reasons") or []),
                    "observed_when_it_ran": recorded.get("observed") or {},
                    # File mtime, not a hash-chained timestamp: it is the only ordering evidence
                    # these pre-dated commits carry, so it travels labelled as such.
                    "decided_at_file_mtime_utc": datetime.fromtimestamp(
                        decision_path.stat().st_mtime, UTC).isoformat(timespec="seconds"),
                    "thresholds_in_force_then": then,
                    "thresholds_that_did_not_exist_then": sorted(set(now) - set(then)),
                    "thresholds_changed_since": {
                        key: {"then": value, "now": now.get(key)}
                        for key, value in then.items()
                        if key in now and now.get(key) != value},
                    "rejudged_under_todays_config_promotes": any(rejudged) if rejudged else None,
                    "the_two_verdicts_agree": (
                        bool(recorded.get("promoted")) == any(rejudged) if rejudged else None),
                }
            rows.append({"stage": stage.name, "run_present": True,
                         "run_dir": str(run_dir.relative_to(ROOT)),
                         "run_kind": run_kind,
                         "promotion_evaluations": promotions,
                         "live_decision": live,
                         "checkpoint_probes": len(probes),
                         "probe_episode_counts": sorted({p["episodes"] for p in probes}),
                         "checkpoints": checkpoints,
                         "gate_thresholds": {"min_win_rate": stage.min_win_rate,
                                             "min_wilson_lower": stage.min_wilson_lower,
                                             "max_truncation_rate": stage.max_truncation_rate,
                                             "max_illegal_actions": stage.max_illegal_actions,
                                             "min_episodes": stage.min_episodes,
                                             "promotion_eval_episodes": stage.promotion_eval_episodes}})

    kept = [row for row in rows if row.get("run_kind") == "campaign"]
    off_ladder = [row for row in rows if row.get("run_kind", "campaign") != "campaign"]
    off_ladder_records = collections.Counter(
        row.get("run_kind", "?") for row in off_ladder
        for _ in row.get("promotion_evaluations", []))
    payload = {
        "aggregates": {
            "configured_stages": [stage.name for stage in stages],
            "stages_with_no_run_at_all": sorted({row["stage"] for row in rows
                                                 if not row["run_present"]}),
            "stages_with_a_promotion_decision": sorted({row["stage"] for row in kept
                                                        if row.get("promotion_evaluations")}),
            "promotion_evaluations_total": sum(len(row.get("promotion_evaluations", []))
                                               for row in kept),
            "promotion_evaluations_promoting_total": sum(
                1 for row in kept for record in row.get("promotion_evaluations", [])
                if record["promoted"]),
            "gate_failure_reasons": dict(collections.Counter(
                reason.split(" ", 1)[0] for row in kept
                for record in row.get("promotion_evaluations", [])
                for reason in record["reasons"])),
            "promotion_evaluations_excluded_by_run_kind": dict(sorted(off_ladder_records.items())),
            "checkpoints_recorded": sum(len(row.get("checkpoints", [])) for row in rows),
            "live_decisions_recorded_by_the_trainers": sum(
                1 for row in kept if row.get("live_decision")),
            "stages_where_the_live_verdict_was_promoted": sorted(
                {row["stage"] for row in kept
                 if (row.get("live_decision") or {}).get("promoted_when_it_ran")}),
            "stages_where_todays_rejudgement_disagrees_with_the_live_verdict": sorted(
                {row["stage"] for row in kept
                 if (row.get("live_decision") or {}).get("the_two_verdicts_agree") is False}),
            "thresholds_that_changed_under_the_ladder": {
                key: {row["stage"]: row["live_decision"]["thresholds_changed_since"][key]
                      for row in kept if key in (row.get("live_decision") or {}).get(
                          "thresholds_changed_since", {})}
                for key in sorted({key for row in kept for key in
                                   (row.get("live_decision") or {}).get(
                                       "thresholds_changed_since", {})})},
        },
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "not_established": [
            "that any of these decisions is still reachable by a reviewer on another machine -- "
            "runs/ is gitignored, so only this ledger's contents travel",
            "the parent of each act-1 arm, which is a separate question handled by "
            "ladder_lineage_20260919.json; a rung can be evaluated and still not be the parent",
            "that a live verdict and today's re-judgement of the same record are the same fact -- "
            "they answer 'what did the ladder decide then' and 'would this promote now', and the "
            "thresholds moved between the two",
            "anything about the shipped game; thresholds come from this repository's own config"],
        "rows": rows,
        "scope": ("every runs/curriculum_v2*/**/<configured stage> directory, judged twice: once by "
                  "the promotion_decision.json the trainer wrote at the time, and once by "
                  "config/training_v2.toml as it reads now; a directory whose own name says it "
                  "was aborted is listed and its records counted separately, never in the totals"),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")
    agg = payload["aggregates"]
    print("configured:", agg["configured_stages"])
    print("no run at all:", agg["stages_with_no_run_at_all"],
          "| with a promotion decision:", agg["stages_with_a_promotion_decision"])
    print(f"promotion evaluations: {agg['promotion_evaluations_total']}, promoting: "
          f"{agg['promotion_evaluations_promoting_total']}, "
          f"excluded by run kind: "
          f"{agg['promotion_evaluations_excluded_by_run_kind']}, "
          f"reasons: {agg['gate_failure_reasons']}")
    print("live decisions recorded:", agg["live_decisions_recorded_by_the_trainers"],
          "| promoted when they ran:", agg["stages_where_the_live_verdict_was_promoted"],
          "| today's re-judgement disagrees:",
          agg["stages_where_todays_rejudgement_disagrees_with_the_live_verdict"])
    print("thresholds that moved:", json.dumps(
        agg["thresholds_that_changed_under_the_ladder"], ensure_ascii=False))
    for row in rows:
        live = row.get("live_decision")
        if live:
            print(f"  {row['stage']:8} LIVE [{row.get('run_kind','?'):10}] "
                  f"{Path(live['file']).parents[1].name:34} "
                  f"promoted_when_it_ran={live['promoted_when_it_ran']} "
                  f"boundary={live['observed_when_it_ran'].get('boundary_rate')} "
                  f"(required {live['thresholds_in_force_then'].get('min_boundary_rate')}) "
                  f"rejudged_now={live['rejudged_under_todays_config_promotes']}")
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
