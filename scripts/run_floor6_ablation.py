"""Floor6 reward ablation runner (review item 4, 2026-09-01).

Three arms x three PPO seeds x a fixed 250k-step budget, every run
warm-started from the promoted floor3 checkpoint, identical training episode
seeds across arms (only the PPO-side seed varies), and a joint metric on the
same 100 floor6 checkpoint-partition seeds:

* boundary rate with the Wilson 95% lower bound,
* mean final floor, mean final HP fraction, mean steps, true terminal wins.

Selection is NEVER by mean_return; the lexicographic comparison lives in
``scripts/summarize_floor6_ablation.py``.  Promotion decisions inside the
ablation are non-authoritative (the promotion eval is shrunk to 100 episodes
and the stage gate is not the ablation signal).

Arms (reward overrides on top of ``config/training_v2.toml``):

* ``A`` baseline: combat_reward_scale 0.10, no new components;
* ``B``: + first_floor_advance_reward 1.0, boundary_success_reward 3.0;
* ``C``: B + step_cost 0.01, combat_reward_scale 0.05.

Usage (one process per arm; arms can run concurrently)::

    python scripts/run_floor6_ablation.py --arm B \
        --ppo-seeds 91001,91002,91003 \
        --warm-start runs/curriculum_v2/v2curriculum-20260901T091856Z/floor3/checkpoints/step_000000500016.zip
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

ARM_OVERRIDES: dict[str, dict[str, float]] = {
    "A": {},
    "B": {"first_floor_advance_reward": 1.0, "boundary_success_reward": 3.0},
    "C": {
        "first_floor_advance_reward": 1.0,
        "boundary_success_reward": 3.0,
        "step_cost": 0.01,
        "combat_reward_scale": 0.05,
    },
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path,
                        default=PROJECT_ROOT / "config" / "training_v2.toml")
    parser.add_argument("--emulator-root", type=Path,
                        default=PROJECT_ROOT.parent
                        / "third_party/slay-the-spire-2-emulator-main")
    parser.add_argument("--stage", default="floor6")
    parser.add_argument("--arm", required=True, choices=sorted(ARM_OVERRIDES))
    parser.add_argument("--ppo-seeds", default="91001,91002,91003",
                        help="comma-separated PPO-side seeds (env seeds shared)")
    parser.add_argument("--warm-start", type=Path, required=True,
                        help="promoted floor3 checkpoint every run starts from")
    parser.add_argument("--timesteps", type=int, default=250_000)
    parser.add_argument("--checkpoint-every-steps", type=int, default=62_500,
                        help="early-reading eval interval inside the run")
    parser.add_argument("--joint-eval-episodes", type=int, default=100)
    parser.add_argument("--promotion-eval-episodes", type=int, default=100,
                        help="shrunk: ablation promotion calls are non-authoritative")
    parser.add_argument("--out-root", type=Path,
                        default=PROJECT_ROOT / "runs" / "ablations")
    parser.add_argument("--label", default=None,
                        help="run-group label; defaults to floor6-<timestamp>")
    args = parser.parse_args(argv)

    import dataclasses

    sys.path.insert(0, str(args.emulator_root / "src"))
    import sts2_gym  # noqa: PLC0415,E402
    from sb3_contrib import MaskablePPO  # noqa: PLC0415,E402

    from training.evaluation import evaluate_policy  # noqa: PLC0415,E402
    from training.metrics import atomic_write_json  # noqa: PLC0415,E402
    from training.v2_config import load_v2_training_config  # noqa: PLC0415,E402
    from training.v2_curriculum import (  # noqa: PLC0415,E402
        _environment_factory,
        _train_stage,
    )

    if not args.warm_start.is_file():
        raise SystemExit(f"warm-start checkpoint missing: {args.warm_start}")

    config = load_v2_training_config(args.config)
    stage = config.stage(args.stage)
    overrides = ARM_OVERRIDES[args.arm]
    arm_config = dataclasses.replace(
        config,
        reward=dataclasses.replace(config.reward, **overrides),
    )
    # Per-run stage: fixed ablation budget + denser checkpoint evals for
    # early readings; episode seeds and the observation contract unchanged.
    run_stage = dataclasses.replace(
        stage,
        timesteps=args.timesteps,
        checkpoint_every_steps=args.checkpoint_every_steps,
        promotion_eval_episodes=args.promotion_eval_episodes,
    )

    ppo_seeds = [int(value) for value in args.ppo_seeds.split(",") if value]
    label = args.label or (
        f"floor6-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    )
    out_dir = args.out_root / label
    out_dir.mkdir(parents=True, exist_ok=True)

    joint_seeds = arm_config.partition(args.stage, "checkpoint").seeds(
        args.joint_eval_episodes
    )
    device = "auto"
    try:
        import torch

        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        device = "cpu"

    manifest = {
        "generated_at": datetime.now(UTC).isoformat(),
        "label": label,
        "arm": args.arm,
        "reward_overrides": overrides,
        "resolved_reward": dataclasses.asdict(arm_config.reward),
        "stage": args.stage,
        "timesteps_per_run": args.timesteps,
        "checkpoint_every_steps": args.checkpoint_every_steps,
        "ppo_seeds": ppo_seeds,
        "env_seed_note": (
            "training episode seeds come from the unchanged SeedStream "
            "namespace: identical across arms"
        ),
        "warm_start": {
            "path": str(args.warm_start),
            "sha256": _sha256_file(args.warm_start),
        },
        "joint_eval": {
            "episodes": args.joint_eval_episodes,
            "partition": f"{args.stage}.checkpoint",
            "seed_start": joint_seeds[0],
            "seed_stop_exclusive": joint_seeds[-1] + 1,
        },
        "emulator_native_sha256": _sha256_file(
            args.emulator_root / "out" / "Sts2Emulator.dll"
        ),
        "selection_rule": (
            "lexicographic boundary_rate -> wilson_low -> mean_final_floor -> "
            "mean_final_hp_fraction -> fewer mean_steps; NEVER mean_return"
        ),
        "runs": {},
    }

    for ppo_seed in ppo_seeds:
        run_dir = out_dir / f"{args.arm}_seed{ppo_seed}"
        promoted, checkpoint = _train_stage(
            arm_config,
            run_stage,
            run_dir,
            args.warm_start,
            sts2_gym,
            ppo_seed=ppo_seed,
        )
        # Joint metric on the fixed 100 checkpoint-partition seeds.
        model = MaskablePPO.load(checkpoint, env=None, device=device)
        factory = _environment_factory(arm_config, run_stage, sts2_gym)
        metrics = evaluate_policy(
            model.policy,
            env_factory=factory,
            seeds=joint_seeds,
            stage=args.stage,
            split=f"ablation:{args.arm}:seed{ppo_seed}",
            scope=stage.scope,
            checkpoint=str(checkpoint),
            max_steps_per_episode=run_stage.max_episode_steps,
        ).to_dict()
        atomic_write_json(run_dir / "joint_metrics.json", metrics)
        manifest["runs"][str(ppo_seed)] = {
            "run_dir": str(run_dir),
            "promoted": bool(promoted),
            "checkpoint": str(checkpoint),
            "joint_metrics": {
                key: metrics.get(key)
                for key in (
                    "episodes",
                    "boundary_rate",
                    "boundary_wilson_95_low",
                    "boundary_wilson_95_high",
                    "win_rate",
                    "mean_final_floor",
                    "mean_final_hp_fraction",
                    "mean_steps",
                    "truncation_rate",
                    "illegal_actions",
                    "unclassified_dead_ends",
                )
            },
        }
        print(json.dumps({f"{args.arm}/seed{ppo_seed}":
                          manifest["runs"][str(ppo_seed)]["joint_metrics"]},
                         ensure_ascii=False))

    manifest["completed_at"] = datetime.now(UTC).isoformat()
    atomic_write_json(out_dir / f"manifest-{args.arm}.json", manifest)
    print(json.dumps({"manifest": str(out_dir / f"manifest-{args.arm}.json")},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
