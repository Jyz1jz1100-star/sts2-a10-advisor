"""Split a recorded evaluation's win rate by the act the emulator generated.

Why this exists: the campaign's metrics declare ``scope: simulator_act1``, but the
bundled emulator picks the run's act in RunMapGenerator.cs:10 (``bool underdocks =
actRng.NextBool()``), and measuring 40 of the arm's own act1/promotion seeds through
``training.v2_curriculum._environment_factory`` gives 20 runs in Act 1 (Overgrowth)
and 20 in Act 2 (Underdocks), deterministically per seed. So a campaign win rate is
a single-act win rate over a mixed-act population, and the aggregate cannot be read
as an Act 1 number.

This tool re-evaluates the same checkpoint on the subset of seeds that generate each
act, through the same ``training.evaluation.evaluate_policy`` the trainer uses, so
the two rates become separately reviewable. It does not rewrite any existing metrics
file: those records stay mixed-act, and this run is what splits them.

    G:/qoder/third_party/slay-the-spire-2-emulator-main/.venv/Scripts/python.exe \
        scripts/split_win_rate_by_generated_act.py \
        --config runtime/fanout/b_terminal-1.toml --stage act1 --split promotion \
        --checkpoint runtime/fanout/b_terminal-1/.../act1/checkpoints/step_000002000016.zip
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "third_party" / "slay-the-spire-2-emulator-main" / "src"))
sys.path.insert(0, str(ROOT))

ACT_NAMES = {1: "overgrowth", 2: "underdocks"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _seed_list(partition, episodes: int | None) -> list[int]:
    attr = getattr(partition, "seeds", None)
    if callable(attr):
        available = list(attr())
    elif attr is not None:
        available = list(attr)
    else:
        available = list(range(int(partition.start), int(partition.start) + int(partition.count)))
    return available if episodes is None else available[:episodes]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--stage", default="act1")
    parser.add_argument("--split", default="promotion")
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--episodes", type=int, default=None,
                        help="cap the partition prefix; default is the whole split")
    parser.add_argument("--act", type=int, choices=(1, 2), default=None,
                        help="evaluate only the seeds that generate this act")
    parser.add_argument("--census-only", action="store_true",
                        help="just report which seed generates which act")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    import sts2_gym  # noqa: F401

    from training.v2_config import load_v2_training_config
    from training.v2_curriculum import _environment_factory

    config = load_v2_training_config(args.config.resolve())
    stage = next((s for s in config.stages if s.name == args.stage), None)
    if stage is None:
        raise SystemExit(f"stage {args.stage!r} not in {args.config}")
    if args.split == "train" and not args.census_only:
        raise SystemExit("the train split cannot evidence a win rate")
    seeds = _seed_list(config.partition(stage.name, args.split), args.episodes)

    # Phase 1: which act does each seed generate? Reset-only, so this is cheap and
    # touches no weights. One env is reused on purpose: NativeRunCore.reset frees the
    # handle it owns, so an env-per-seed census leaks a native run per seed and the
    # emulator starts failing Sts2Run_GetInfo with status -1. The act must be a
    # function of the seed for the split to mean anything, so it is sampled twice and
    # disagreement aborts the run.
    factory = _environment_factory(config, stage, sts2_gym)
    census_env = factory(seeds[0])
    acts: dict[int, int] = {}
    for seed in seeds:
        _obs, info = census_env.reset(seed=seed)
        acts[seed] = int(info.get("act") or 0)
    unstable = [seed for seed in seeds[: min(10, len(seeds))]
                if int(census_env.reset(seed=seed)[1].get("act") or 0) != acts[seed]]
    census_env.close()
    if unstable:
        raise SystemExit(f"act is not seed-deterministic for {unstable[:3]}; "
                         "a per-act split would be meaningless")
    census = Counter(ACT_NAMES.get(acts[seed], str(acts[seed])) for seed in seeds)
    print(f"{stage.name}/{args.split}: {len(seeds)} seeds, generated acts: {dict(census)}")
    print(f"(act is seed-deterministic: re-sampled {min(10, len(seeds))} seeds, all agreed)")
    if args.census_only:
        return 0

    # Phase 2: evaluate each act's seeds separately, same code path as the trainer.
    from sb3_contrib import MaskablePPO
    from stable_baselines3.common.vec_env import DummyVecEnv

    from training.evaluation import evaluate_policy

    checkpoint_sha = _sha256(args.checkpoint)
    print(f"checkpoint {args.checkpoint.name} sha256 {checkpoint_sha[:16]}…")
    probe = DummyVecEnv([lambda: factory(seeds[0])])
    model = MaskablePPO.load(str(args.checkpoint), env=probe, device="cpu")

    rows: list[dict[str, object]] = []
    for act, name in sorted(ACT_NAMES.items()):
        if args.act is not None and act != args.act:
            continue
        subset = [seed for seed in seeds if acts[seed] == act]
        if not subset:
            continue
        metrics = evaluate_policy(
            model,
            env_factory=factory,
            seeds=subset,
            stage=stage.name,
            split=f"{args.split}_act{act}",
            scope=f"simulator_act{act}",
            checkpoint=args.checkpoint,
            max_steps_per_episode=stage.max_episode_steps,
        ).to_dict()
        row = {"act": act, "act_name": name, "seeds": len(subset),
               "seed_sha256": hashlib.sha256(
                   ",".join(str(seed) for seed in subset).encode()).hexdigest(),
               "checkpoint_sha256": checkpoint_sha}
        row.update({key: metrics.get(key) for key in
                    ("win_rate", "wins", "episodes", "mean_final_floor", "max_final_floor",
                     "truncation_rate", "defect_truncation_rate", "winning_seeds", "by_act",
                     "illegal_actions", "unclassified_dead_ends")})
        rows.append(row)
        print(f"  act {act} ({name:10}) {row['wins']}/{row['seeds']} win_rate={row['win_rate']} "
              f"mean_floor={row['mean_final_floor']} max_floor={row['max_final_floor']} "
              f"trunc={row['truncation_rate']} "
              f"illegal={row['illegal_actions']} unclassified={row['unclassified_dead_ends']}")

    payload = {"schema_version": 1,
               "generated_by": "scripts/split_win_rate_by_generated_act.py",
               "config": str(args.config), "stage": stage.name, "split": args.split,
               "checkpoint": str(args.checkpoint), "census": dict(census),
               "note": ("campaign records labelled simulator_act1 are a mixed-act "
                        "population; these subsets are split by the act the emulator "
                        "generated for each seed"),
               "results": rows}
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
