"""Generate teacher-v3 labels: combat beam search + long out-of-run rollouts.

This is the successor generator required by ``data/teacher/FREEZE-R2.txt``:
the frozen batch0/batch1/dagger0 lineage (24-step greedy-continuation scorer)
is never expanded; new labels come from the v3 search teacher instead.

Two gates are enforced before any record is written:

* **Seed freeze guard** — the requested seeds must sit outside the frozen R2
  lineage and, unless ``--allow-reserved-test-corpus`` is passed, outside the
  reserved final-BC-holdout range (``1_410_000_000+``).
* **Expert-label gate** — records are emitted with
  ``label_status: "candidate"`` unless ``--strength-report`` points at a
  ``scripts/evaluate_teacher_strength.py`` report whose gate passed
  (``gate.expert_labels_approved`` true on ``scope=simulator_act1``).  Only
  then may records carry ``label_status: "expert"``.

Usage::

    python scripts/generate_teacher_batch_v3.py \
        --seed-count 20 --out data/teacher/v3_batch0.jsonl

Outputs ``<out>`` (JSONL) plus ``<out>.manifest.json`` (dataset hash, budgets,
phase counts, freeze notice, strength-report reference when given).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def native_hash(emulator_root: Path) -> str:
    digest = hashlib.sha256()
    dll = emulator_root / "out" / "Sts2Emulator.dll"
    if not dll.is_file():
        raise SystemExit(f"missing native library: {dll}")
    with dll.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emulator-root", type=Path, default=PROJECT_ROOT.parent
                        / "third_party/slay-the-spire-2-emulator-main")
    parser.add_argument("--seed-start", type=int, default=None,
                        help="default: training.teacher_v3.V3_DEFAULT_SEED_START")
    parser.add_argument("--seed-count", type=int, default=20)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--beam-width", type=int, default=64)
    parser.add_argument("--lookahead-turns", type=int, default=1,
                        help="enemy rounds searched after the labelled turn (1-2)")
    parser.add_argument("--max-node-expansions", type=int, default=4096)
    parser.add_argument("--continuations", type=int, default=3)
    parser.add_argument("--rollout-max-steps", type=int, default=600)
    parser.add_argument("--max-floor", type=int, default=6,
                        help="curriculum boundary for out-of-run rollouts")
    parser.add_argument("--min-score-gap", type=float, default=0.5)
    parser.add_argument("--max-decisions-per-run", type=int, default=8)
    parser.add_argument("--traversal-steps", type=int, default=400)
    parser.add_argument("--strength-report", type=Path, default=None,
                        help="strength JSON whose passing gate upgrades records "
                             "from candidate to expert labels")
    parser.add_argument("--allow-reserved-test-corpus", action="store_true",
                        help="permit seeds >= 1_410_000_000 (reserved final "
                             "BC holdout); refused by default")
    args = parser.parse_args(argv)

    from training.teacher_batch import TeacherBatchConfig, is_interesting, traverse_run
    from training.teacher_v3 import (
        FROZEN_R2_SEED_RANGES,
        RESERVED_TEACHER_TEST_SEED_START,
        V3_DEFAULT_SEED_START,
        BeamSearchConfig,
        LongRolloutConfig,
        assert_seeds_outside_frozen_lineages,
        label_decision_v3,
        strength_report_approves,
    )

    seed_start = args.seed_start if args.seed_start is not None else V3_DEFAULT_SEED_START
    if not 0 <= args.shard_index < args.shard_count:
        raise SystemExit("shard-index must be in [0, shard-count)")
    all_seeds = list(range(seed_start, seed_start + args.seed_count))
    seeds = all_seeds[args.shard_index :: args.shard_count]
    assert_seeds_outside_frozen_lineages(
        seeds, allow_reserved_test_corpus=args.allow_reserved_test_corpus
    )

    label_status = "candidate"
    strength_note = (
        "no strength report provided; records are candidates and must not be "
        "called expert labels"
    )
    strength_reference: dict[str, object] | None = None
    if args.strength_report is not None:
        report = json.loads(args.strength_report.read_text(encoding="utf-8"))
        if strength_report_approves(report):
            label_status = "expert"
            strength_note = "strength gate passed; records may be used as expert labels"
        else:
            strength_note = (
                "strength report provided but its gate did not pass; records "
                "remain candidates"
            )
        strength_reference = {
            "path": str(args.strength_report),
            "sha256": hashlib.sha256(
                args.strength_report.read_bytes()
            ).hexdigest(),
            "approved": strength_report_approves(report),
        }

    sys.path.insert(0, str(args.emulator_root / "src"))
    from sts2_gym import native  # noqa: PLC0415,E402

    from training.prefix_replay_teacher import sts2_run_env_factory  # noqa: PLC0415,E402

    emulator = native_hash(args.emulator_root)
    factory = sts2_run_env_factory(max_episode_steps=1200, max_floors=16)
    traversal_config = TeacherBatchConfig(
        rollout_max_steps=args.rollout_max_steps,
        min_score_gap=args.min_score_gap,
        max_decisions_per_run=args.max_decisions_per_run,
        traversal_steps=args.traversal_steps,
    )
    beam_config = BeamSearchConfig(
        beam_width=args.beam_width,
        opponent_lookahead_turns=args.lookahead_turns,
        max_node_expansions=args.max_node_expansions,
    )
    rollout_config = LongRolloutConfig(
        continuations=args.continuations,
        max_steps=args.rollout_max_steps,
        max_floor=args.max_floor,
    )

    suffix = f".shard{args.shard_index:02d}" if args.shard_count > 1 else ""
    out_path = args.out.with_suffix(args.out.suffix + suffix)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    import random  # noqa: PLC0415,E402

    counts: dict[str, int] = {}
    gaps: list[float] = []
    modes: dict[str, int] = {}
    replay_audits: list[bool] = []
    stats = {"runs": 0, "visited": 0, "interesting": 0, "labelled": 0, "rejected": 0}
    digest = hashlib.sha256()
    with out_path.open("w", encoding="utf-8") as handle:
        for seed in seeds:
            stats["runs"] += 1
            batch_digest = hashlib.blake2b(
                f"teacher-v3-traversal:{seed}".encode(), digest_size=8
            )
            rng = random.Random(int.from_bytes(batch_digest.digest(), "big"))
            labelled = 0
            for decision in traverse_run(
                seed, env_factory=factory, rng=rng, config=traversal_config
            ):
                stats["visited"] += 1
                if not is_interesting(decision):
                    continue
                stats["interesting"] += 1
                if labelled >= args.max_decisions_per_run:
                    continue
                try:
                    record = label_decision_v3(
                        decision,
                        env_factory=factory,
                        emulator_hash=emulator,
                        beam_config=beam_config,
                        rollout_config=rollout_config,
                        min_score_gap=args.min_score_gap,
                    )
                except Exception:
                    stats["rejected"] += 1
                    continue
                if record is None:
                    stats["rejected"] += 1
                    continue
                labelled += 1
                stats["labelled"] += 1
                record["label_status"] = label_status
                line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                handle.write(line)
                handle.flush()
                digest.update(line.encode("utf-8"))
                phase = str(record["phase"])
                counts[phase] = counts.get(phase, 0) + 1
                gaps.append(float(record["score_gap"]))
                mode = record["teacher"]["search"]["mode"]
                modes[mode] = modes.get(mode, 0) + 1
                replay_audits.append(
                    record["state_hashes"]["capture"]
                    == record["state_hashes"]["reverify"]
                )

    manifest = {
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": "simulator_act1",
        "disclaimer": "Act 1 emulator search only; this is not a real-game A10 result.",
        "teacher_version": 3,
        "label_status": label_status,
        "label_status_note": strength_note,
        "strength_report": strength_reference,
        "dataset_path": str(out_path),
        "dataset_sha256": digest.hexdigest(),
        "records": len(gaps),
        "emulator_root": str(args.emulator_root),
        "emulator_native_sha256": emulator,
        "run_native_api_version": int(getattr(native, "_REQUIRED_RUN_NATIVE_API_VERSION", 8)),
        "freeze": {
            "notice": "R2 lineage (batch0/batch1/dagger0) is frozen per FREEZE-R2.txt",
            "frozen_ranges": [list(pair) for pair in FROZEN_R2_SEED_RANGES],
            "reserved_test_corpus_start": RESERVED_TEACHER_TEST_SEED_START,
            "allow_reserved_test_corpus": args.allow_reserved_test_corpus,
        },
        "seeds": {"start": seed_start, "count": args.seed_count,
                  "shard_index": args.shard_index, "shard_count": args.shard_count,
                  "processed": len(seeds)},
        "budget": {
            "beam_width": beam_config.beam_width,
            "opponent_lookahead_turns": beam_config.opponent_lookahead_turns,
            "max_node_expansions": beam_config.max_node_expansions,
            "continuations": rollout_config.continuations,
            "rollout_max_steps": rollout_config.max_steps,
            "max_floor": rollout_config.max_floor,
            "min_score_gap": args.min_score_gap,
            "max_decisions_per_run": args.max_decisions_per_run,
            "traversal_steps": args.traversal_steps,
        },
        "stats": stats,
        "phase_counts": dict(sorted(counts.items())),
        "search_modes": dict(sorted(modes.items())),
        "replay_audit": {
            "checked": len(replay_audits),
            "all_capture_reverify_match": all(replay_audits) if replay_audits else True,
        },
        "score_gap": {
            "min": min(gaps) if gaps else None,
            "median": sorted(gaps)[len(gaps) // 2] if gaps else None,
            "max": max(gaps) if gaps else None,
        },
    }
    out_path.with_suffix(out_path.suffix + ".manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if gaps else 1


if __name__ == "__main__":
    raise SystemExit(main())
