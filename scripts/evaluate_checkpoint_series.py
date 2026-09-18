"""Evaluate a checkpoint series of one arm on a single never-trained partition.

The campaign's per-split records answer "did this arm ever win?" but they are not
comparable to each other: the checkpoint, promotion and final splits are different
seed ranges, and the fan-out gives every arm its own disjoint partition. So a
1% win_rate at 1M steps and a 2% win_rate at 4M steps are two different tests, not
a trend.

This tool fixes the partition (default: the stage's ``final`` split, which the
curriculum validates as disjoint from every train split and which the campaign's
own evaluations never touch) and walks a list of checkpoints through the same
``training.evaluation.evaluate_policy`` the trainer uses. Differences in the output
are then attributable to the weights, not to the seeds.

Nothing here is a promotion decision: win rates at this sample size carry wide
intervals, and the act1 gate is 35%.

    G:/qoder/third_party/slay-the-spire-2-emulator-main/.venv/Scripts/python.exe \
        scripts/evaluate_checkpoint_series.py \
        --config runtime/fanout/b_terminal-1.toml --stage act1 --episodes 200 \
        --checkpoint runtime/fanout/b_terminal-1/.../act1/checkpoints/step_000001000008.zip \
        --checkpoint runtime/fanout/b_terminal-1/.../act1/checkpoints/final.zip \
        --out runs/checkpoint_series/b_terminal-1.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "third_party" / "slay-the-spire-2-emulator-main" / "src"))
sys.path.insert(0, str(ROOT))

INTERESTING = ("win_rate", "wins", "episodes", "mean_final_floor", "max_final_floor",
               "final_floor_histogram",
               "illegal_actions", "unclassified_dead_ends", "defect_truncation_rate")


def _depth_line(histogram: dict) -> str:
    """Cumulative "reached at least floor f" counts for the deepest floors.

    Splitting "how many runs get to the end of the act" from "how many win there"
    is the only reason this comparator prints a distribution at all: a mean
    terminal floor of 8 is equally consistent with everyone dying at 8 and with
    half dying at 6 and half at the boss.
    """

    if not histogram:
        return "no floor reported"
    total = sum(histogram.values())
    floors = sorted(int(floor) for floor in histogram)
    parts = []
    for floor in floors[-3:]:
        reached = sum(count for key, count in histogram.items() if int(key) >= floor)
        parts.append(f">={floor}:{reached}/{total}")
    return " ".join(parts)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def reject_unusable_split(split: str) -> None:
    """Refuse a split the weights were fit on.

    A win rate measured on the train split is not evidence of anything, and this
    tool exists precisely to make numbers comparable, so the guard has to be
    impossible to trip over by passing the wrong ``--split``.
    """

    if split == "train":
        raise SystemExit("the train split is what the weights were fit on; it cannot "
                         "evidence a win rate")


def _partition_seeds(partition, episodes: int) -> list[int]:
    seeds_attr = getattr(partition, "seeds", None)
    if callable(seeds_attr):
        available = list(seeds_attr())
    elif seeds_attr is not None:
        available = list(seeds_attr)
    else:
        available = list(range(int(partition.start), int(partition.start) + int(partition.count)))
    if episodes > len(available):
        raise SystemExit(
            f"requested {episodes} episodes but the partition holds {len(available)} seeds; "
            "reuse of a seed outside its declared partition is not a fair test")
    return available[:episodes]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="the arm's TOML")
    parser.add_argument("--stage", default="act1")
    parser.add_argument("--split", default="final",
                        help="a split declared disjoint from the stage's train split")
    parser.add_argument("--episodes", type=int, default=200)
    parser.add_argument("--checkpoint", type=Path, action="append", required=True)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    import sts2_gym  # noqa: F401  (proves the emulator import path works)

    from sb3_contrib import MaskablePPO
    from stable_baselines3.common.vec_env import DummyVecEnv

    from training.evaluation import evaluate_policy
    from training.teacher_v3 import assert_seeds_outside_frozen_lineages
    from training.v2_config import load_v2_training_config
    from training.v2_curriculum import _environment_factory

    config = load_v2_training_config(args.config.resolve())
    stage = next((s for s in config.stages if s.name == args.stage), None)
    if stage is None:
        raise SystemExit(f"stage {args.stage!r} is not in {args.config}")
    reject_unusable_split(args.split)
    seeds = _partition_seeds(config.partition(stage.name, args.split), args.episodes)
    assert_seeds_outside_frozen_lineages(seeds)
    seed_sha256 = hashlib.sha256(",".join(str(seed) for seed in seeds).encode()).hexdigest()
    print(f"{stage.name}/{args.split}: {len(seeds)} seeds {seeds[0]}…{seeds[-1]} "
          f"sha256 {seed_sha256[:16]}…")
    print(f"scope: {stage.scope} (simulator only; the emulator has no Act 2 or 3)")

    env_factory = _environment_factory(config, stage, sts2_gym)
    probe_env = DummyVecEnv([lambda: env_factory(seeds[0])])
    results: list[dict[str, object]] = []
    for checkpoint in args.checkpoint:
        claimed = _sha256(checkpoint)
        model = MaskablePPO.load(str(checkpoint), env=probe_env, device="cpu")
        metrics = evaluate_policy(
            model,
            env_factory=env_factory,
            seeds=seeds,
            stage=stage.name,
            split=args.split,
            scope=stage.scope,
            checkpoint=checkpoint,
            max_steps_per_episode=stage.max_episode_steps,
        ).to_dict()
        row = {"checkpoint": str(checkpoint), "checkpoint_sha256": claimed}
        row.update({key: metrics.get(key) for key in INTERESTING})
        results.append(row)
        print(f"  {checkpoint.parent.parent.parent.name}/{checkpoint.name:24} "
              f"sha {claimed[:12]}… win_rate={row['win_rate']!s:>6} "
              f"({row['wins']}/{row['episodes']}) "
              f"illegal={row['illegal_actions']} unclassified={row['unclassified_dead_ends']} "
              f"max_floor={row['max_final_floor']} depth[{_depth_line(row['final_floor_histogram'])}]")

    payload = {
        "schema_version": 1,
        "generated_by": "scripts/evaluate_checkpoint_series.py",
        "config": str(args.config),
        "stage": stage.name,
        "split": args.split,
        "seeds": {"count": len(seeds), "start": seeds[0], "end": seeds[-1],
                  "seed_sha256": seed_sha256},
        "scope_note": ("simulator_act1 only; the bundled emulator generates Act 1 "
                       "(RunConstants.MapBossRow = 16, single boss node), so nothing "
                       "here evidences an Act 1-3 clear."),
        "results": results,
    }
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8")
        print(f"wrote {args.out} (sha256 {_sha256(args.out)[:16]}…)")
    print("VERDICT:", "series evaluated on one shared unseen partition"
          if len(results) > 1 else "single checkpoint, no trend claim available")
    return 0 if all(row["illegal_actions"] == 0 for row in results) else 4


if __name__ == "__main__":
    raise SystemExit(main())
