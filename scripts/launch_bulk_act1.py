"""Launch one bounded, direct Act 1 simulator PPO experiment.

The checked-in V2 curriculum runner intentionally loads a whole PPO archive
when continuing a prior stage.  The BC actor archive is different: it is a
MaskablePPO container whose rollout hyperparameters belong to distillation.
This launcher therefore constructs the configured learner first and copies
only the verified actor weights from the leak-free r3 archive.

This is an experimental simulator run.  It writes under the configured
``runs/bulk_training`` tree, never touches the reserved final seed partition,
and never invokes live/game/lock/autoplay machinery.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def _resolve(path: Path, *, root: Path = PROJECT_ROOT) -> Path:
    return path if path.is_absolute() else (root / path).resolve()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _contract_sha() -> str:
    from training.v2_observation import observation_contract

    return hashlib.sha256(
        json.dumps(observation_contract(), sort_keys=True).encode("utf-8")
    ).hexdigest()


def _safe_total_timesteps(target: int, n_envs: int) -> int:
    """Return a vector-step budget that never exceeds ``target``.

    SB3's on-policy loop treats ``total_timesteps`` as a lower bound and may
    finish a whole rollout beyond it.  A vectorized environment advances the
    learner by ``n_envs`` every environment step, so flooring to that unit
    gives the callback a reachable hard boundary even when the requested
    budget is not divisible by the number of environments.
    """

    if isinstance(target, bool) or not isinstance(target, int) or target < 0:
        raise ValueError("target must be a non-negative integer")
    if isinstance(n_envs, bool) or not isinstance(n_envs, int) or n_envs <= 0:
        raise ValueError("n_envs must be a positive integer")
    return target - (target % n_envs)


class _HardBudgetStopMixin:
    """Stop an SB3 callback once the vector-step budget is reached."""

    _hard_budget: int

    def _on_step(self) -> bool:
        # Keep the normal checkpoint/evaluation callback first.  If this step
        # lands exactly on the budget, it can still produce the final regular
        # checkpoint before returning False to abort the partial rollout.
        if not super()._on_step():
            return False
        return int(self.num_timesteps) < self._hard_budget


def _make_manifest(
    *,
    config: Any,
    label: str,
    run_root: Path,
    pretrained: Path,
    ppo_seed: int,
    device: str,
) -> dict[str, Any]:
    from training.v2_curriculum import plan

    stage = config.stage("act1")
    payload = plan(config, PROJECT_ROOT)
    payload["experiment"] = {
        "label": label,
        "kind": "bounded_direct_act1_pretrained_actor",
        "scope": "simulator_act1",
        "experimental": True,
        "promoted": False,
        "promotion_note": (
            "Direct Act 1 experiment; no checkpoint from this run is promoted "
            "or treated as a live-game claim."
        ),
        "run_root": str(run_root),
        "ppo_seed": ppo_seed,
        "device": device,
        "parallel_envs": stage.parallel_envs,
        "timesteps": stage.timesteps,
        "reserved_final_partition": {
            "start": config.partition("act1", "final").start,
            "count": config.partition("act1", "final").count,
        },
        "pretrained_actor": {
            "path": str(pretrained),
            "sha256": _sha256_file(pretrained),
        },
        "learner_policy": {
            "architecture": {"pi": [256, 256], "vf": [128, 128]},
            "source_weights_only": True,
            "algorithm": dataclasses.asdict(config.algorithm),
        },
    }
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "config" / "training_v2_bulk_act1.toml",
    )
    parser.add_argument(
        "--pretrained",
        type=Path,
        default=PROJECT_ROOT / "models" / "bc_pretrain_r3" / "pretrained_actor.zip",
    )
    parser.add_argument("--label", required=True)
    parser.add_argument("--ppo-seed", type=int, default=91004)
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    args = parser.parse_args(argv)

    from training.metrics import atomic_write_json
    from training.v2_config import load_v2_training_config

    config_path = _resolve(args.config)
    pretrained = _resolve(args.pretrained)
    if not pretrained.is_file():
        raise SystemExit(f"pretrained actor is missing: {pretrained}")
    config = load_v2_training_config(config_path)
    if tuple(stage.name for stage in config.stages) != ("act1",):
        raise SystemExit("bulk launcher requires a single direct act1 stage")
    stage = config.stage("act1")
    hard_budget = _safe_total_timesteps(stage.timesteps, stage.parallel_envs)

    emulator_root = _resolve(config.emulator_root)
    sys.path.insert(0, str(emulator_root / "src"))

    import torch
    from sb3_contrib import MaskablePPO
    from stable_baselines3.common.callbacks import BaseCallback
    from stable_baselines3.common.vec_env import DummyVecEnv

    import sts2_gym
    from training.v2_curriculum import (
        _callback_class,
        _environment_factory,
        _training_environment_factory,
    )

    device = args.device
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"

    output_root = _resolve(config.output_dir)
    run_root = output_root / args.label
    run_root.mkdir(parents=True, exist_ok=False)
    stage_dir = run_root / "act1"
    stage_dir.mkdir(parents=True, exist_ok=False)
    atomic_write_json(
        run_root / "plan.json",
        _make_manifest(
            config=config,
            label=args.label,
            run_root=run_root,
            pretrained=pretrained,
            ppo_seed=args.ppo_seed,
            device=device,
        ),
    )
    manifest_path = run_root / "manifest.json"
    manifest: dict[str, Any] = {
        "label": args.label,
        "scope": "simulator_act1",
        "experimental": True,
        "promoted": False,
        "status": "starting",
        "started_at": datetime.now(UTC).isoformat(),
        "run_root": str(run_root),
        "config": str(config_path),
        "pretrained_actor": str(pretrained),
        "pretrained_actor_sha256": _sha256_file(pretrained),
        "emulator_root": str(emulator_root),
        "emulator_native_sha256": _sha256_file(
            emulator_root / "out" / "Sts2Emulator.dll"
        ),
        "ppo_seed": args.ppo_seed,
        "device": device,
        "timesteps": stage.timesteps,
        "timesteps_target": stage.timesteps,
        "timesteps_budget": hard_budget,
        "actual_timesteps": 0,
        "actual_updates": 0,
        "parallel_envs": stage.parallel_envs,
        "train_partition": dataclasses.asdict(config.partition("act1", "train")),
        "checkpoint_partition": dataclasses.asdict(
            config.partition("act1", "checkpoint")
        ),
        "promotion_partition": dataclasses.asdict(
            config.partition("act1", "promotion")
        ),
        "final_partition_reserved": dataclasses.asdict(
            config.partition("act1", "final")
        ),
        "reward": dataclasses.asdict(config.reward),
        "learner_policy": {
            "architecture": {"pi": [256, 256], "vf": [128, 128]},
            "source_weights_only": True,
            "algorithm": dataclasses.asdict(config.algorithm),
        },
    }
    atomic_write_json(manifest_path, manifest)

    vector_env = None
    try:
        env_factory = _environment_factory(config, stage, sts2_gym)
        vector_env = DummyVecEnv(
            [
                _training_environment_factory(
                    config, stage, sts2_gym, rank, stage.parallel_envs
                )
                for rank in range(stage.parallel_envs)
            ]
        )

        # Verify the source archive can be read, then transfer policy weights
        # into a new learner carrying the experiment's actual PPO settings.
        source = MaskablePPO.load(pretrained, env=None, device=device)
        source_obs = tuple(source.observation_space.shape or ())
        source_actions = int(source.action_space.n)
        model = MaskablePPO(
            "MlpPolicy",
            vector_env,
            verbose=1,
            device=device,
            n_steps=config.algorithm.n_steps,
            batch_size=config.algorithm.batch_size,
            n_epochs=config.algorithm.n_epochs,
            gamma=config.algorithm.gamma,
            learning_rate=config.algorithm.learning_rate,
            ent_coef=config.algorithm.entropy_coefficient,
            policy_kwargs={"net_arch": {"pi": [256, 256], "vf": [128, 128]}},
            seed=args.ppo_seed,
        )
        if source_obs != tuple(model.observation_space.shape or ()):
            raise RuntimeError(
                f"pretrained observation space {source_obs} does not match "
                f"V2 learner {model.observation_space.shape}"
            )
        if source_actions != int(model.action_space.n):
            raise RuntimeError(
                f"pretrained action space {source_actions} does not match "
                f"V2 learner {model.action_space.n}"
            )
        model.policy.load_state_dict(source.policy.state_dict())
        del source

        callback_type = _callback_class(BaseCallback)

        class BulkCheckpointCallback(_HardBudgetStopMixin, callback_type):
            _hard_budget = hard_budget

            def _evaluate(self, seeds: list[int], split: str, checkpoint: Path):
                payload = super()._evaluate(seeds, split, checkpoint)
                payload["experimental"] = True
                payload["experiment_label"] = args.label
                return payload

        callback = BulkCheckpointCallback(
            stage=stage,
            stage_dir=stage_dir,
            env_factory=env_factory,
            checkpoint_seeds=config.partition("act1", "checkpoint").seeds(
                stage.checkpoint_eval_episodes
            ),
            promotion_seeds=config.partition("act1", "promotion").seeds(
                stage.promotion_eval_episodes
            ),
            resume_base_steps=0,
            observation_sha=_contract_sha(),
            reward_config=config.reward,
        )
        manifest.update(
            {
                "status": "training",
                "learner_initialized": True,
                "source_weights_loaded": True,
                "learner_n_steps": model.n_steps,
                "learner_batch_size": model.batch_size,
                "learner_n_epochs": model.n_epochs,
                "learner_gamma": model.gamma,
            }
        )
        atomic_write_json(manifest_path, manifest)
        print(
            json.dumps(
                {
                    "status": "training_started",
                    "run_root": str(run_root),
                    "stage_dir": str(stage_dir),
                    "timesteps_target": stage.timesteps,
                    "timesteps_budget": hard_budget,
                    "parallel_envs": stage.parallel_envs,
                    "device": device,
                    "learner": {
                        "n_steps": model.n_steps,
                        "batch_size": model.batch_size,
                        "n_epochs": model.n_epochs,
                        "gamma": model.gamma,
                    },
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

        if hard_budget:
            model.learn(total_timesteps=hard_budget, callback=callback)
        actual_timesteps = int(model.num_timesteps)
        actual_updates = int(getattr(model, "_n_updates", 0))
        if actual_timesteps > hard_budget or actual_timesteps > stage.timesteps:
            raise RuntimeError(
                "hard training budget exceeded: "
                f"actual={actual_timesteps}, budget={hard_budget}, "
                f"target={stage.timesteps}"
            )
        manifest.update(
            {
                "actual_timesteps": actual_timesteps,
                "actual_updates": actual_updates,
                "budget_exhausted": actual_timesteps == hard_budget,
            }
        )
        atomic_write_json(manifest_path, manifest)
        final_checkpoint = stage_dir / "checkpoints" / "final"
        final_checkpoint.parent.mkdir(parents=True, exist_ok=True)
        model.save(final_checkpoint)
        final_checkpoint = final_checkpoint.with_suffix(".zip")
        final_payload = callback._evaluate(
            config.partition("act1", "promotion").seeds(
                stage.promotion_eval_episodes
            ),
            "experiment-final",
            final_checkpoint,
        )
        atomic_write_json(
            stage_dir / "metrics" / "experiment-final.json", final_payload
        )
        manifest.update(
            {
                "status": "completed",
                "completed_at": datetime.now(UTC).isoformat(),
                "final_checkpoint": str(final_checkpoint),
                "final_checkpoint_sha256": _sha256_file(final_checkpoint),
                "final_evaluation": str(
                    stage_dir / "metrics" / "experiment-final.json"
                ),
                "promoted": False,
                "promotion_note": "experiment evidence only; no promotion performed",
            }
        )
        atomic_write_json(manifest_path, manifest)
        print(
            json.dumps(
                {
                    "status": "training_completed",
                    "run_root": str(run_root),
                    "timesteps_target": stage.timesteps,
                    "timesteps_budget": hard_budget,
                    "actual_timesteps": actual_timesteps,
                    "actual_updates": actual_updates,
                    "final_checkpoint": str(final_checkpoint),
                    "final_evaluation": str(
                        stage_dir / "metrics" / "experiment-final.json"
                    ),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 0
    except Exception as exc:
        manifest.update(
            {
                "status": "failed",
                "failed_at": datetime.now(UTC).isoformat(),
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        atomic_write_json(manifest_path, manifest)
        raise
    finally:
        if vector_env is not None:
            vector_env.close()


if __name__ == "__main__":
    raise SystemExit(main())
