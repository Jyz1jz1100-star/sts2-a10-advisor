"""Pretrained-PPO vs ordinary-PPO, fixed-seed small-scale comparison.

Review item 3 followed through: the masked-CE pretrained actor is the PPO
warm-start; this runner asks whether entering the floor6 PPO stage from the
pretrained actor beats entering it the ordinary ways, on identical fixed
seeds and a small budget.

Arms (floor6, ``--timesteps`` default 250k, same fixed training episode
seeds from the unchanged SeedStream, same PPO-side seed, checkpoint evals on
the same 100 floor6 checkpoint-partition seeds):

* ``pretrained`` — curriculum-configured MaskablePPO with the actor's
  architecture (``pi=[256,256], vf=[128,128]``), weights loaded from the
  ``pretrained_actor.zip`` policy state dict, then ``set_random_seed``.
  Identical hyperparameters to ``vanilla`` — **only the initialization
  differs**.
* ``vanilla`` — same architecture and hyperparameters, random initialization
  (the "ordinary" PPO as constructed from scratch).
* ``warm`` — the existing production path: ``MaskablePPO.load`` of the
  promoted floor3 checkpoint (default architecture [64,64]).  Reported as
  the real-world ordinary baseline; its architecture differs, so it is not
  the clean init-isolation arm.

Selection is the joint metric (boundary + Wilson, mean final floor, mean HP
fraction, mean steps, true wins) — never mean_return — matching the ablation
protocol.  Usage::

    python scripts/compare_pretrained_vs_vanilla_ppo.py \
        --pretrained models/bc_pretrain_r3/pretrained_actor.zip \
        --warm-start runs/curriculum_v2/v2curriculum-20260901T091856Z/floor3/checkpoints/step_000000500016.zip \
        --label ppo-pretrain-vs-vanilla
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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _build_vec_env(config, stage, sts2_gym, workers: int, stream_suffix: str = ""):
    from stable_baselines3.common.vec_env import DummyVecEnv

    from training.v2_curriculum import _training_environment_factory

    envs = [
        _training_environment_factory(
            config, stage, sts2_gym, rank, workers, stream_suffix=stream_suffix
        )
        for rank in range(workers)
    ]
    return DummyVecEnv(envs)


def _joint_eval(model, *, env_factory, seeds, stage, split, checkpoint, max_steps):
    from training.evaluation import evaluate_policy

    metrics = evaluate_policy(
        model,
        env_factory=env_factory,
        seeds=seeds,
        stage=stage.name,
        split=split,
        scope=stage.scope,
        checkpoint=checkpoint,
        max_steps_per_episode=max_steps,
        # The stage decides the world. Without this a campaign stage is scored on
        # one act per seed while still reporting `scope='simulator_full_run'`, which
        # is the collector-side defect already fixed in the curriculum reappearing on
        # the measurement side -- and it would silently decide an initialisation
        # comparison, since both arms would be graded on the same truncated world.
        campaign=stage.campaign,
    ).to_dict()
    return {
        "episodes": metrics["episodes"],
        "boundary_rate": metrics["boundary_rate"],
        "boundary_wilson_95_low": metrics["boundary_wilson_95_low"],
        "boundary_wilson_95_high": metrics["boundary_wilson_95_high"],
        "win_rate": metrics["win_rate"],
        "mean_final_floor": metrics["mean_final_floor"],
        "mean_final_hp_fraction": metrics.get("mean_final_hp_fraction"),
        "mean_steps": metrics["mean_steps"],
        "mean_return": metrics["mean_return"],
        "truncation_rate": metrics["truncation_rate"],
        "illegal_actions": metrics["illegal_actions"],
        "unclassified_dead_ends": metrics["unclassified_dead_ends"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path,
                        default=PROJECT_ROOT / "config" / "training_v2.toml")
    parser.add_argument("--emulator-root", type=Path,
                        default=PROJECT_ROOT.parent
                        / "third_party/slay-the-spire-2-emulator-main")
    parser.add_argument("--stage", default="floor6")
    parser.add_argument("--pretrained", type=Path, required=True,
                        help="models/bc_pretrain_r3/pretrained_actor.zip")
    parser.add_argument("--warm-start", type=Path, required=True,
                        help="promoted floor3 checkpoint for the warm arm")
    parser.add_argument("--arms", default="pretrained,vanilla,warm",
                        help="comma-separated arm names to run")
    parser.add_argument("--ppo-seed", type=int, default=91001)
    parser.add_argument("--timesteps", type=int, default=250_000)
    parser.add_argument("--checkpoint-every-steps", type=int, default=62_500)
    parser.add_argument("--joint-eval-episodes", type=int, default=100)
    parser.add_argument("--net-arch", default="pi=[256,256],vf=[128,128]",
                        help="actor architecture shared by pretrained/vanilla")
    parser.add_argument("--label", default=None)
    parser.add_argument("--out-root", type=Path,
                        default=PROJECT_ROOT / "runs" / "ppo_compare")
    parser.add_argument("--device", default="auto")
    args = parser.parse_args(argv)

    import dataclasses

    sys.path.insert(0, str(args.emulator_root / "src"))
    import sts2_gym  # noqa: PLC0415,E402
    from sb3_contrib import MaskablePPO  # noqa: PLC0415,E402
    from stable_baselines3.common.callbacks import BaseCallback  # noqa: PLC0415,E402

    from training.metrics import atomic_write_json  # noqa: PLC0415,E402
    from training.v2_config import load_v2_training_config  # noqa: PLC0415,E402
    from training.v2_curriculum import _environment_factory  # noqa: PLC0415,E402

    for path, name in ((args.pretrained, "--pretrained"),
                       (args.warm_start, "--warm-start")):
        if not Path(path).is_file():
            raise SystemExit(f"{name} checkpoint missing: {path}")

    config = load_v2_training_config(args.config)
    stage = config.stage(args.stage)
    arms = [arm.strip() for arm in args.arms.split(",") if arm.strip()]

    # Parse the actor architecture: "pi=[256,256],vf=[128,128]" (split on the
    # commas that separate key=value pairs, not the ones inside the brackets).
    def _parse_arch(spec: str) -> dict:
        out: dict[str, list[int]] = {}
        pieces = spec.split(",")
        buffer = ""
        for piece in pieces:
            buffer = (buffer + "," + piece) if buffer else piece
            if buffer.count("[") != buffer.count("]"):
                continue  # a bracket is still open: this comma was inside []
            key, values = buffer.split("=")
            out[key.strip()] = [int(v) for v in values.strip("[]").split(",")]
            buffer = ""
        if buffer:
            raise SystemExit(f"cannot parse net-arch spec: {spec!r}")
        return out

    net_arch = _parse_arch(args.net_arch)
    alg = config.algorithm
    device = args.device
    if device == "auto":
        import torch

        device = "cuda" if torch.cuda.is_available() else "cpu"

    label = args.label or (
        f"ppo-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    )
    out_dir = args.out_root / label
    out_dir.mkdir(parents=True, exist_ok=True)

    joint_seeds = config.partition(args.stage, "checkpoint").seeds(
        args.joint_eval_episodes
    )
    eval_factory = _environment_factory(config, stage, sts2_gym)
    max_steps = int(stage.max_episode_steps)

    class CompareCallback(BaseCallback):
        def __init__(self, *, arm_dir: Path, eval_every: int):
            super().__init__()
            self.arm_dir = arm_dir
            self.eval_every = eval_every
            self._last = 0

        def _on_step(self) -> bool:
            if self.num_timesteps - self._last < self.eval_every:
                return True
            self._last = self.num_timesteps
            stem = f"step_{self.num_timesteps:012d}"
            checkpoint = self.arm_dir / "checkpoints" / stem
            checkpoint.parent.mkdir(parents=True, exist_ok=True)
            self.model.save(checkpoint)
            payload = _joint_eval(
                self.model,
                env_factory=eval_factory,
                seeds=joint_seeds,
                stage=stage,
                split=f"ppo-compare:{label}:{stem}",
                checkpoint=checkpoint.with_suffix(".zip"),
                max_steps=max_steps,
            )
            (self.arm_dir / "metrics").mkdir(parents=True, exist_ok=True)
            atomic_write_json(self.arm_dir / "metrics" / f"{stem}.json", payload)
            print(json.dumps({f"{label}:{stem}": payload}, ensure_ascii=False),
                  flush=True)
            return True

    manifest = {
        "generated_at": datetime.now(UTC).isoformat(),
        "label": label,
        "stage": args.stage,
        "arms": arms,
        "ppo_seed": args.ppo_seed,
        "timesteps_per_arm": args.timesteps,
        "checkpoint_every_steps": args.checkpoint_every_steps,
        "env_seed_note": (
            "training episode seeds come from the unchanged SeedStream "
            "namespace: identical across arms"
        ),
        "init_note": {
            "pretrained": "actor weights loaded into a same-arch, same-"
                          "hyperparameter curriculum PPO (only init differs)",
            "vanilla": "same architecture/hyperparameters, random init",
            "warm": "MaskablePPO.load of the promoted floor3 checkpoint "
                    "(production path; default [64,64] arch)",
        },
        "algorithm": dataclasses.asdict(alg),
        "net_arch": net_arch,
        "pretrained_actor": {
            "path": str(args.pretrained),
            "sha256": _sha256_file(args.pretrained),
        },
        "warm_start_checkpoint": {
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
            "joint metric (boundary -> Wilson -> floor -> HP -> steps); "
            "mean_return reported for transparency, never selected on"
        ),
        "arms": {},
    }

    for arm in arms:
        arm_dir = out_dir / f"{arm}_seed{args.ppo_seed}"
        vec_env = _build_vec_env(config, stage, sts2_gym,
                                 int(stage.parallel_envs))
        try:
            if arm == "pretrained":
                actor = MaskablePPO.load(args.pretrained, env=None, device=device)
                actor_sd = actor.policy.state_dict()
                model = MaskablePPO(
                    "MlpPolicy",
                    vec_env,
                    verbose=1,
                    device=device,
                    n_steps=alg.n_steps,
                    batch_size=alg.batch_size,
                    n_epochs=alg.n_epochs,
                    gamma=alg.gamma,
                    learning_rate=alg.learning_rate,
                    ent_coef=alg.entropy_coefficient,
                    policy_kwargs=dict(net_arch=net_arch),
                    seed=args.ppo_seed,
                )
                model.policy.load_state_dict(actor_sd)
                del actor
            elif arm == "vanilla":
                model = MaskablePPO(
                    "MlpPolicy",
                    vec_env,
                    verbose=1,
                    device=device,
                    n_steps=alg.n_steps,
                    batch_size=alg.batch_size,
                    n_epochs=alg.n_epochs,
                    gamma=alg.gamma,
                    learning_rate=alg.learning_rate,
                    ent_coef=alg.entropy_coefficient,
                    policy_kwargs=dict(net_arch=net_arch),
                    seed=args.ppo_seed,
                )
            elif arm == "warm":
                model = MaskablePPO.load(args.warm_start, env=vec_env, device=device)
                model.set_random_seed(args.ppo_seed)
            else:
                raise SystemExit(f"unknown arm: {arm}")

            callback = CompareCallback(arm_dir=arm_dir,
                                       eval_every=args.checkpoint_every_steps)
            model.learn(total_timesteps=args.timesteps, callback=callback)
            final = arm_dir / "checkpoints" / "final"
            final.parent.mkdir(parents=True, exist_ok=True)
            model.save(final)
            payload = _joint_eval(
                model,
                env_factory=eval_factory,
                seeds=joint_seeds,
                stage=stage,
                split=f"ppo-compare:{label}:{arm}:final",
                checkpoint=final.with_suffix(".zip"),
                max_steps=max_steps,
            )
            atomic_write_json(arm_dir / "joint_metrics.json", payload)
            manifest["arms"][arm] = {
                "dir": str(arm_dir),
                "final_checkpoint": str(final.with_suffix(".zip")),
                "joint_metrics": payload,
            }
            print(json.dumps({f"{arm}:final": payload}, ensure_ascii=False),
                  flush=True)
        finally:
            vec_env.close()

    manifest["completed_at"] = datetime.now(UTC).isoformat()
    atomic_write_json(out_dir / "manifest.json", manifest)
    print(json.dumps({"manifest": str(out_dir / "manifest.json")},
                     ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
