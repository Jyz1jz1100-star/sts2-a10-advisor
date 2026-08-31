from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import StageConfig, TrainingConfig, load_training_config
from .evaluation import evaluate_policy
from .metrics import atomic_write_json
from .promotion import decide_promotion
from .seeds import SeedStream


def _resolve(project_root: Path, path: Path) -> Path:
    return path if path.is_absolute() else (project_root / path).resolve()


def _scope(stage: StageConfig) -> str:
    if stage.environment == "combat":
        return "simulator_combat"
    suffix = "_experimental" if stage.experimental else ""
    return f"simulator_{stage.name}{suffix}"


def _environment_factory(stage: StageConfig, sts2_gym: Any):
    def build(seed: int):
        if stage.environment == "combat":
            return sts2_gym.Sts2CombatEnv(
                seed=seed,
                max_episode_steps=stage.max_episode_steps,
            )
        return sts2_gym.Sts2RunEnv(
            seed=seed,
            max_episode_steps=stage.max_episode_steps,
            max_floors=stage.max_floors or 16,
        )

    return build


def _training_environment_factory(
    stage: StageConfig,
    sts2_gym: Any,
    train_partition: Any,
    rank: int,
    workers: int,
):
    import gymnasium as gym
    from sb3_contrib.common.wrappers import ActionMasker

    worker_partition = train_partition.shard(rank, workers)
    stream = SeedStream(worker_partition, f"{stage.name}:worker:{rank}")
    base_factory = _environment_factory(stage, sts2_gym)

    class SeededEpisodeWrapper(gym.Wrapper):
        """Seed rotation plus a defensive external step cap.

        The step cap mirrors the env's own episode limit because the
        simulator's native invalid-action path returns without applying that
        internal limit (deterministic-eval hang, 2026-09-01). Rollouts must
        not be able to spin on rejected actions forever.
        """

        def __init__(self, env, max_steps: int):
            super().__init__(env)
            self._max_steps = max_steps
            self._steps = 0

        def reset(self, *, seed=None, options=None):
            actual_seed = stream.next() if seed is None else seed
            self._steps = 0
            return self.env.reset(seed=actual_seed, options=options)

        def step(self, action):
            observation, reward, terminated, truncated, info = self.env.step(action)
            self._steps += 1
            if not terminated and self._steps >= self._max_steps:
                truncated = True
            return observation, reward, terminated, truncated, info

    def initialize():
        wrapped = SeededEpisodeWrapper(
            base_factory(worker_partition.start), stage.max_episode_steps
        )
        return ActionMasker(wrapped, lambda env: env.unwrapped.action_masks())

    return initialize


def _callback_class(base_callback: type):
    class CheckpointEvaluationCallback(base_callback):
        def __init__(
            self,
            *,
            stage: StageConfig,
            stage_dir: Path,
            env_factory: Any,
            seeds: list[int],
            promotion_seeds: list[int] | None = None,
            promotion_probe_every_steps: int | None = None,
        ):
            super().__init__(verbose=0)
            self.stage = stage
            self.stage_dir = stage_dir
            self.env_factory = env_factory
            self.seeds = seeds
            self.promotion_seeds = promotion_seeds
            self.promotion_probe_every_steps = promotion_probe_every_steps
            self.last_checkpoint = 0
            self.last_probe = 0
            self.early_promotion: dict[str, Any] | None = None

        def _probe_promotion(self) -> None:
            assert self.promotion_seeds is not None
            stem = f"early-promotion-{self.num_timesteps // 1_000_000}m"
            checkpoint_stem = f"step_{self.num_timesteps:012d}"
            checkpoint = self.stage_dir / "checkpoints" / f"{checkpoint_stem}.zip"
            if not checkpoint.is_file():
                return
            metrics = evaluate_policy(
                self.model,
                env_factory=self.env_factory,
                seeds=self.promotion_seeds,
                stage=self.stage.name,
                split="promotion",
                scope=_scope(self.stage),
                checkpoint=checkpoint,
                experimental=self.stage.experimental,
                max_steps_per_episode=self.stage.max_episode_steps,
            )
            metrics_path = self.stage_dir / "metrics" / f"{stem}.json"
            atomic_write_json(metrics_path, metrics.to_dict())
            decision = decide_promotion(metrics, self.stage.promotion)
            if not decision.promoted:
                return
            payload = decision.to_dict()
            payload["metrics_path"] = str(metrics_path)
            payload["checkpoint"] = str(checkpoint)
            payload["checkpoint_sha256"] = metrics.checkpoint_sha256
            atomic_write_json(
                self.stage_dir / "early-promotion-decision.json", payload
            )
            promotion_metrics = self.stage_dir / "metrics" / "promotion.json"
            promotion_metrics.write_bytes(metrics_path.read_bytes())
            self.early_promotion = {"checkpoint": checkpoint, "decision": decision}

        def _on_step(self) -> bool:
            if self.early_promotion is not None:
                return False
            if (
                self.num_timesteps - self.last_checkpoint
                < self.stage.checkpoint_every_steps
            ):
                return True
            self.last_checkpoint = self.num_timesteps
            stem = f"step_{self.num_timesteps:012d}"
            checkpoint = self.stage_dir / "checkpoints" / stem
            checkpoint.parent.mkdir(parents=True, exist_ok=True)
            self.model.save(checkpoint)
            metrics = evaluate_policy(
                self.model,
                env_factory=self.env_factory,
                seeds=self.seeds,
                stage=self.stage.name,
                split="checkpoint",
                scope=_scope(self.stage),
                checkpoint=checkpoint.with_suffix(".zip"),
                experimental=self.stage.experimental,
                max_steps_per_episode=self.stage.max_episode_steps,
            )
            atomic_write_json(
                self.stage_dir / "metrics" / f"{stem}.json", metrics.to_dict()
            )
            if (
                self.promotion_probe_every_steps
                and self.num_timesteps - self.last_probe
                >= self.promotion_probe_every_steps
            ):
                self.last_probe = self.num_timesteps
                self._probe_promotion()
                if self.early_promotion is not None:
                    return False
            return True

    return CheckpointEvaluationCallback


def _train_stage(
    config: TrainingConfig,
    stage: StageConfig,
    stage_dir: Path,
    previous_checkpoint: Path | None,
    sts2_gym: Any,
) -> tuple[bool, Path]:
    from sb3_contrib import MaskablePPO
    from stable_baselines3.common.callbacks import BaseCallback
    from stable_baselines3.common.vec_env import DummyVecEnv

    env_factory = _environment_factory(stage, sts2_gym)
    vector_env = DummyVecEnv(
        [
            _training_environment_factory(
                stage,
                sts2_gym,
                config.seeds["train"],
                rank,
                stage.parallel_envs,
            )
            for rank in range(stage.parallel_envs)
        ]
    )
    try:
        if stage.initialize_from_previous:
            if previous_checkpoint is None:
                raise RuntimeError(
                    f"stage {stage.name} requires a previous checkpoint but none exists"
                )
            try:
                model = MaskablePPO.load(
                    previous_checkpoint,
                    env=vector_env,
                    device=config.algorithm.device,
                )
            except ValueError as exc:
                raise RuntimeError(
                    f"stage {stage.name} cannot initialize from {previous_checkpoint}; "
                    "observation/action spaces must match"
                ) from exc
        else:
            model = MaskablePPO(
                "MlpPolicy",
                vector_env,
                verbose=1,
                device=config.algorithm.device,
                n_steps=config.algorithm.n_steps,
                batch_size=config.algorithm.batch_size,
                n_epochs=config.algorithm.n_epochs,
                gamma=config.algorithm.gamma,
                learning_rate=config.algorithm.learning_rate,
                ent_coef=config.algorithm.entropy_coefficient,
            )

        checkpoint_seeds = config.seeds["checkpoint"].seeds(
            stage.checkpoint_eval_episodes
        )
        promotion_seeds = config.seeds["promotion"].seeds(
            stage.promotion_eval_episodes
        )
        callback_type = _callback_class(BaseCallback)
        callback = callback_type(
            stage=stage,
            stage_dir=stage_dir,
            env_factory=env_factory,
            seeds=checkpoint_seeds,
            promotion_seeds=promotion_seeds,
            promotion_probe_every_steps=stage.promotion_probe_every_steps,
        )
        model.learn(total_timesteps=stage.timesteps, callback=callback)

        if callback.early_promotion is not None:
            promoted_checkpoint: Path = callback.early_promotion["checkpoint"]
            decision = callback.early_promotion["decision"]
            atomic_write_json(stage_dir / "promotion_decision.json", decision.to_dict())
            return decision.promoted, promoted_checkpoint

        final_checkpoint = stage_dir / "checkpoints" / "final"
        final_checkpoint.parent.mkdir(parents=True, exist_ok=True)
        model.save(final_checkpoint)
        final_checkpoint = final_checkpoint.with_suffix(".zip")

        metrics = evaluate_policy(
            model,
            env_factory=env_factory,
            seeds=promotion_seeds,
            stage=stage.name,
            split="promotion",
            scope=_scope(stage),
            checkpoint=final_checkpoint,
            experimental=stage.experimental,
            max_steps_per_episode=stage.max_episode_steps,
        )
        atomic_write_json(stage_dir / "metrics" / "promotion.json", metrics.to_dict())
        decision = decide_promotion(metrics, stage.promotion)
        atomic_write_json(stage_dir / "promotion_decision.json", decision.to_dict())
        return decision.promoted, final_checkpoint
    finally:
        vector_env.close()


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _emulator_provenance(config: TrainingConfig, project_root: Path) -> dict:
    root = _resolve(project_root, config.emulator_root)
    revision = None
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        revision = completed.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        pass
    return {
        "root": str(root),
        "git_revision": revision,
        "native_sha256": _sha256_file(root / "out" / "Sts2Emulator.dll"),
    }


def _plan(config: TrainingConfig, project_root: Path) -> dict:
    return {
        "character": config.character,
        "ascension": config.ascension,
        "game_branch": config.game_branch,
        "save_load": config.save_load,
        "emulator": _emulator_provenance(config, project_root),
        "stages": [
            {
                "name": stage.name,
                "environment": stage.environment,
                "timesteps": stage.timesteps,
                "parallel_envs": stage.parallel_envs,
                "checkpoint_every_steps": stage.checkpoint_every_steps,
                "promotion_probe_every_steps": stage.promotion_probe_every_steps,
                "experimental": stage.experimental,
                "initialize_from_previous": stage.initialize_from_previous,
                "scope": _scope(stage),
                "promotion": {
                    "episodes": stage.promotion_eval_episodes,
                    "min_win_rate": stage.promotion.min_win_rate,
                    "min_wilson_lower": stage.promotion.min_wilson_lower,
                    "max_truncation_rate": stage.promotion.max_truncation_rate,
                    "max_illegal_actions": stage.promotion.max_illegal_actions,
                    "min_mean_floor": stage.promotion.min_mean_floor,
                },
            }
            for stage in config.stages
        ],
        "seed_partitions": [
            {
                "name": partition.name,
                "start": partition.start,
                "count": partition.count,
            }
            for partition in config.seeds.as_list()
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the staged MaskablePPO curriculum with isolated seeds"
    )
    parser.add_argument("--config", type=Path, default=Path("config/training.toml"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-experimental-full-run", action="store_true")
    parser.add_argument("--only-stage", choices=("combat", "act1", "full_run"))
    parser.add_argument("--initial-checkpoint", type=Path)
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    config_path = _resolve(project_root, args.config)
    config = load_training_config(config_path)
    selected = tuple(
        stage
        for stage in config.stages
        if args.only_stage is None or stage.name == args.only_stage
    )
    if args.dry_run:
        print(json.dumps(_plan(config, project_root), ensure_ascii=False, indent=2))
        return
    for stage in selected:
        if stage.experimental and not args.allow_experimental_full_run:
            raise SystemExit(
                f"stage {stage.name} is experimental because the local emulator has not "
                "proven multi-act parity; pass --allow-experimental-full-run explicitly"
            )

    emulator_root = _resolve(project_root, config.emulator_root)
    sys.path.insert(0, str(emulator_root / "src"))
    import sts2_gym

    run_id = datetime.now(UTC).strftime("curriculum-%Y%m%dT%H%M%SZ")
    run_root = _resolve(project_root, config.output_dir) / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    atomic_write_json(run_root / "plan.json", _plan(config, project_root))

    previous_checkpoint = (
        _resolve(project_root, args.initial_checkpoint)
        if args.initial_checkpoint is not None
        else None
    )
    for stage in selected:
        stage_dir = run_root / stage.name
        stage_dir.mkdir(parents=True, exist_ok=False)
        promoted, previous_checkpoint = _train_stage(
            config, stage, stage_dir, previous_checkpoint, sts2_gym
        )
        if not promoted:
            raise SystemExit(
                f"stage {stage.name} did not meet its promotion gate; see "
                f"{stage_dir / 'promotion_decision.json'}"
            )


if __name__ == "__main__":
    main()
