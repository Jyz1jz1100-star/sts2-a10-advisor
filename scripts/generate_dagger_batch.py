"""Generate a formal DAgger batch from the current BC student.

The student re-plays the simulator on *fresh* seeds (a range disjoint from
every train/eval partition and from all teacher batches), the teacher labels
the states where the student is uncertain or in danger, and the result is a
teacher-corrected dataset in the same record format as the search batches::

    python scripts/generate_dagger_batch.py --checkpoint models/bc_v2_batch0.pt \
        --seed-start 1500100000 --seed-count 200 --out data/teacher/dagger0.jsonl

Manifests record the student checkpoint hash so the correction cycle is
provable: which student's mistakes a DAgger batch answers.
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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--emulator-root", type=Path,
                        default=PROJECT_ROOT.parent / "third_party"
                        / "slay-the-spire-2-emulator-main")
    parser.add_argument("--seed-start", type=int, default=1500100000)
    parser.add_argument("--seed-count", type=int, default=200)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--traversal-steps", type=int, default=200)
    parser.add_argument("--max-decisions-per-run", type=int, default=10)
    parser.add_argument("--rollout-max-steps", type=int, default=24)
    parser.add_argument("--min-score-gap", type=float, default=0.3)
    parser.add_argument("--uncertainty-margin", type=float, default=0.35)
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(args.emulator_root / "src"))
    from sts2_gym import native  # noqa: E402

    from training.behavior_clone_v2 import load_model  # noqa: E402
    from training.dagger_batch import DaggerConfig, generate_dagger_batch  # noqa: E402
    from training.prefix_replay_teacher import sts2_run_env_factory  # noqa: E402
    from training.v2_native_env import NativeRunCore  # noqa: E402

    dll = args.emulator_root / "out" / "Sts2Emulator.dll"
    emulator_hash = sha256_file(dll)
    checkpoint_sha = sha256_file(args.checkpoint)
    model = load_model(args.checkpoint)
    raw_factory = sts2_run_env_factory(max_episode_steps=1200, max_floors=16)

    def core_factory():
        return NativeRunCore(native, max_episode_steps=1200)

    config = DaggerConfig(
        rollout_max_steps=args.rollout_max_steps,
        min_score_gap=args.min_score_gap,
        max_decisions_per_run=args.max_decisions_per_run,
        traversal_steps=args.traversal_steps,
        uncertainty_margin=args.uncertainty_margin,
    )
    seeds = list(range(args.seed_start, args.seed_start + args.seed_count))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    stats: dict[str, int] = {}
    disagreements = 0
    count = 0
    with args.out.open("w", encoding="utf-8") as handle:
        for record in generate_dagger_batch(
            seeds, core_factory=core_factory, env_factory=raw_factory,
            model=model, emulator_hash=emulator_hash, device=args.device,
            config=config, progress=lambda snapshot: stats.update(snapshot),
        ):
            line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            handle.write(line)
            handle.flush()
            digest.update(line.encode("utf-8"))
            count += 1
            if not record["teacher_agreement"]:
                disagreements += 1

    manifest = {
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": "simulator_act1",
        "source": "dagger",
        "dataset_path": str(args.out),
        "dataset_sha256": digest.hexdigest(),
        "records": count,
        "teacher_student_disagreements": disagreements,
        "student_checkpoint": str(args.checkpoint),
        "student_checkpoint_sha256": checkpoint_sha,
        "emulator_native_sha256": emulator_hash,
        "seeds": {"start": args.seed_start, "count": args.seed_count},
        "budget": {
            "rollout_max_steps": config.rollout_max_steps,
            "min_score_gap": config.min_score_gap,
            "max_decisions_per_run": config.max_decisions_per_run,
            "traversal_steps": config.traversal_steps,
            "uncertainty_margin": config.uncertainty_margin,
        },
        "stats": stats,
    }
    args.out.with_suffix(args.out.suffix + ".manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if count else 1


if __name__ == "__main__":
    raise SystemExit(main())
