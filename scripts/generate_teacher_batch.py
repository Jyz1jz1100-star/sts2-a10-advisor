"""Generate the first batch of search-teacher labels from the V2 stack.

Example (bounded quality batch, ~a few minutes)::

    python scripts/generate_teacher_batch.py \
        --seed-count 40 --out data/teacher/v2_batch0.jsonl

Outputs:

* ``<out>``          JSONL, one high-confidence label per line;
* ``<out>.manifest.json``  dataset hash, emulator hash, budgets, per-phase
  counts, rejected/divergent-state counts, and the plan hash.

Every record is ``simulator_act1``-scoped.  The batch is teacher *candidate*
data; quality review (gap distribution, phase balance, replay audit of a
random sample) must pass before scaling to 50k鈥?00k labels.
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
    parser.add_argument("--seed-start", type=int, default=1400000000,
                        help="teacher seeds live outside every train/eval partition")
    parser.add_argument("--seed-count", type=int, default=40)
    parser.add_argument("--shard-index", type=int, default=0,
                        help="process this stride of the seed range (parallel workers)")
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--rollout-max-steps", type=int, default=24)
    parser.add_argument("--min-score-gap", type=float, default=0.5)
    parser.add_argument("--max-decisions-per-run", type=int, default=12)
    parser.add_argument("--traversal-steps", type=int, default=400)
    args = parser.parse_args(argv)

    sys.path.insert(0, str(args.emulator_root / "src"))
    from sts2_gym import native  # noqa: E402

    from training.prefix_replay_teacher import sts2_run_env_factory  # noqa: E402
    from training.teacher_batch import (  # noqa: E402
        TeacherBatchConfig,
        generate_batch,
    )

    emulator = native_hash(args.emulator_root)
    factory = sts2_run_env_factory(
        max_episode_steps=1200, max_floors=16
    )
    config = TeacherBatchConfig(
        rollout_max_steps=args.rollout_max_steps,
        min_score_gap=args.min_score_gap,
        max_decisions_per_run=args.max_decisions_per_run,
        traversal_steps=args.traversal_steps,
    )
    if not 0 <= args.shard_index < args.shard_count:
        raise SystemExit("shard-index must be in [0, shard-count)")
    all_seeds = list(range(args.seed_start, args.seed_start + args.seed_count))
    seeds = all_seeds[args.shard_index :: args.shard_count]
    suffix = f".shard{args.shard_index:02d}" if args.shard_count > 1 else ""
    out_path = args.out.with_suffix(args.out.suffix + suffix)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    gaps: list[float] = []
    replay_audits: list[bool] = []
    stats = {"runs": 0, "visited": 0, "interesting": 0, "labelled": 0, "rejected": 0}
    # Stream records straight to the final file with a running digest so an
    # interrupted long batch stays auditable instead of vanishing with a temp
    # file.  The manifest, written only on success, is the completeness claim.
    digest = hashlib.sha256()
    with out_path.open("w", encoding="utf-8") as handle:
        for record in generate_batch(
            seeds, env_factory=factory, emulator_hash=emulator,
            config=config, progress=lambda snapshot: stats.update(snapshot),
        ):
            line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            handle.write(line)
            handle.flush()
            digest.update(line.encode("utf-8"))
            phase = str(record["phase"])
            counts[phase] = counts.get(phase, 0) + 1
            gaps.append(float(record["score_gap"]))
            replay_audits.append(
                record["state_hashes"]["capture"]
                == record["state_hashes"]["reverify"]
            )

    manifest = {
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": "simulator_act1",
        "disclaimer": "Act 1 emulator search only; this is not a real-game A10 result.",
        "dataset_path": str(out_path),
        "dataset_sha256": digest.hexdigest(),
        "records": len(gaps),
        "emulator_root": str(args.emulator_root),
        "emulator_native_sha256": emulator,
        "run_native_api_version": int(getattr(native, "_REQUIRED_RUN_NATIVE_API_VERSION", 8)),
        "seeds": {"start": args.seed_start, "count": args.seed_count,
                  "shard_index": args.shard_index, "shard_count": args.shard_count,
                  "processed": len(seeds)},
        "budget": {
            "rollout_max_steps": config.rollout_max_steps,
            "discount": config.discount,
            "min_score_gap": config.min_score_gap,
            "max_decisions_per_run": config.max_decisions_per_run,
            "traversal_steps": config.traversal_steps,
        },
        "stats": stats,
        "phase_counts": dict(sorted(counts.items())),
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
