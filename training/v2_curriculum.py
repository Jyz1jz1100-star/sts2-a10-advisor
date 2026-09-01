"""V2 floor-curriculum training entry point (independent of V1 artifacts).

Stage ladder (from the V2 plan)::

    floor 3 -> floor 6 -> floor 10 -> floor 13 -> Act 1 complete

Every stage:

* trains MaskablePPO over the V2 contract stack
  (``NativeRunCore`` -> ``V2FlatActionEnv`` -> ``V2RunEnvWrapper`` ->
  ``ActionMasker``), so ``max_floor`` truncation, terminal-only win
  attribution, sentinel empty-mask handling, and shaped run reward are the
  single shared definition used by evaluation and by the search teacher;
* draws seeds from *its own* train/checkpoint/promotion/final partitions, all
  pairwise disjoint across every stage (validated at config load);
* writes under ``runs/curriculum_v2/<run id>/<stage>/`` — V1's
  ``runs/curriculum`` tree is never touched;
* records every metric with ``scope = simulator_act1``.

Usage::

    python -m training.v2_curriculum --config config/training_v2.toml --dry-run
    python -m training.v2_curriculum --config config/training_v2.toml --only-stage floor3
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import PromotionConfig
from .evaluation import evaluate_policy
from .metrics import atomic_write_json
from .promotion import decide_promotion
from .seeds import SeedStream
from .v2_config import V2StageConfig, V2TrainingConfig, load_v2_training_config
from .v2_flat_env import SENTINEL_FLAT, V2FlatActionEnv
from .v2_observation import observation_contract
from .v2_run_wrapper import V2RunEnvWrapper


def _resolve(project_root: Path, path: Path) -> Path:
    return path if path.is_absolute() else (project_root / path).resolve()


def _promotion_config(stage: V2StageConfig) -> PromotionConfig:
    return PromotionConfig(
        min_episodes=stage.min_episodes,
        min_win_rate=stage.min_win_rate,
        min_wilson_lower=stage.min_wilson_lower,
        max_truncation_rate=stage.max_truncation_rate,
        max_illegal_actions=stage.max_illegal_actions,
        min_mean_floor=None,
        min_boundary_rate=stage.min_boundary_rate,
        min_boundary_wilson_lower=stage.min_boundary_wilson_lower,
    )


def _environment_factory(config: V2TrainingConfig, stage: V2StageConfig, sts2_gym: Any):
    """Build the full V2 contract stack for one seed (used by evaluation)."""

    from .v2_native_env import NativeRunCore

    def build(seed: int):
        core = NativeRunCore(
            sts2_gym.native, max_episode_steps=stage.max_episode_steps
        )
        flat = V2FlatActionEnv(core)
        return V2RunEnvWrapper(
            flat,
            max_floor=stage.max_floor,
            reward_config=config.reward,
            sentinel_action=SENTINEL_FLAT,
        )

    return build


def _training_environment_factory(
    config: V2TrainingConfig,
    stage: V2StageConfig,
    sts2_gym: Any,
    rank: int,
    workers: int,
    stream_suffix: str = "",
):
    import gymnasium as gym
    from sb3_contrib.common.wrappers import ActionMasker

    train_partition = config.partition(stage.name, "train")
    worker_partition = train_partition.shard(rank, workers)
    stream = SeedStream(worker_partition, f"v2:{stage.name}:worker:{rank}{stream_suffix}")
    base_factory = _environment_factory(config, stage, sts2_gym)

    class SeedRotationWrapper(gym.Wrapper):
        """One untouched partition seed per episode, inside one worker shard.

        SB3 vector envs call ``reset()`` without arguments between episodes;
        the V2 flat env refuses an implicit seed, so this wrapper supplies the
        deterministic stream before the contract wrapper sees it.
        """

        def reset(self, *, seed=None, options=None):
            actual_seed = stream.next() if seed is None else seed
            return self.env.reset(seed=actual_seed, options=options)

        def action_masks(self):
            # gymnasium 1.x does not forward unknown attributes through
            # Wrapper.__getattr__, and sb3_contrib's ActionMasker calls this
            # on the wrapped env directly.
            return self.env.action_masks()

    def initialize():
        env = SeedRotationWrapper(base_factory(worker_partition.start))
        return ActionMasker(env, lambda environment: environment.action_masks())

    return initialize


def _callback_class(base_callback: type):
    class V2CheckpointEvaluationCallback(base_callback):
        """Checkpoint + boundary-probe cadence on the V2 contract stack.

        Numbering rules mirror V1 (filename-derived global steps across a
        resume), but every metric records V2-specific fields: boundary rate,
        per-reason dead-end counts, and the expanded-observation contract
        hash.  A stage may only pass through its boundary gate.
        """

        def __init__(
            self,
            *,
            stage: V2StageConfig,
            stage_dir: Path,
            env_factory: Any,
            checkpoint_seeds: list[int],
            promotion_seeds: list[int],
            resume_base_steps: int,
            observation_sha: str,
            reward_config: Any,
        ):
            super().__init__(verbose=0)
            self.stage = stage
            self.stage_dir = stage_dir
            self.env_factory = env_factory
            self.checkpoint_seeds = checkpoint_seeds
            self.promotion_seeds = promotion_seeds
            self.base_steps = resume_base_steps
            self.observation_sha = observation_sha
            self.reward_config = reward_config
            self.last_checkpoint = (
                resume_base_steps // stage.checkpoint_every_steps
            ) * stage.checkpoint_every_steps
            if stage.promotion_probe_every_steps:
                self.last_probe = (
                    resume_base_steps // stage.promotion_probe_every_steps
                ) * stage.promotion_probe_every_steps
            else:
                self.last_probe = 0
            self.early_promotion: dict[str, Any] | None = None

        @property
        def global_steps(self) -> int:
            return self.base_steps + self.num_timesteps

        def _evaluate(self, seeds: list[int], split: str, checkpoint: Path) -> Any:
            metrics = evaluate_policy(
                self.model,
                env_factory=self.env_factory,
                seeds=seeds,
                stage=self.stage.name,
                split=split,
                scope=self.stage.scope,
                checkpoint=checkpoint,
                max_steps_per_episode=self.stage.max_episode_steps,
            )
            payload = metrics.to_dict()
            payload["v2"] = {
                "observation_contract_sha256": self.observation_sha,
                "max_floor": self.stage.max_floor,
                "flat_action_space": True,
                "reward": dataclasses.asdict(self.reward_config),
            }
            return payload

        def _probe_promotion(self) -> None:
            global_steps = self.global_steps
            # Filename carries the exact global step: an "M"-rounded stem
            # would collide whenever the probe interval is below 1M (the
            # 500k-probe floor3 stage would otherwise overwrite 1M with 1.5M
            # evidence).
            probe_stem = f"early-promotion-step_{global_steps:012d}"
            checkpoint = (
                self.stage_dir / "checkpoints" / f"step_{global_steps:012d}.zip"
            )
            if not checkpoint.is_file():
                return
            payload = self._evaluate(
                self.promotion_seeds, "promotion", checkpoint
            )
            metrics_path = self.stage_dir / "metrics" / f"{probe_stem}.json"
            atomic_write_json(metrics_path, payload)
            decision = decide_promotion(
                _metrics_from_payload(payload), _promotion_config(self.stage)
            )
            if not decision.promoted:
                return
            record = decision.to_dict()
            record["metrics_path"] = str(metrics_path)
            record["checkpoint"] = str(checkpoint)
            record["checkpoint_sha256"] = payload["checkpoint_sha256"]
            atomic_write_json(
                self.stage_dir / "early-promotion-decision.json", record
            )
            promotion_metrics = self.stage_dir / "metrics" / "promotion.json"
            promotion_metrics.write_bytes(metrics_path.read_bytes())
            self.early_promotion = {"checkpoint": checkpoint, "decision": decision}

        def _on_step(self) -> bool:
            if self.early_promotion is not None:
                return False
            global_steps = self.global_steps
            if (
                global_steps - self.last_checkpoint
                < self.stage.checkpoint_every_steps
            ):
                return True
            self.last_checkpoint = global_steps
            stem = f"step_{global_steps:012d}"
            checkpoint = self.stage_dir / "checkpoints" / stem
            checkpoint.parent.mkdir(parents=True, exist_ok=True)
            self.model.save(checkpoint)
            payload = self._evaluate(
                self.checkpoint_seeds, "checkpoint", checkpoint.with_suffix(".zip")
            )
            atomic_write_json(self.stage_dir / "metrics" / f"{stem}.json", payload)
            if (
                self.stage.promotion_probe_every_steps
                and global_steps - self.last_probe
                >= self.stage.promotion_probe_every_steps
            ):
                self.last_probe = global_steps
                self._probe_promotion()
                if self.early_promotion is not None:
                    return False
            return True

    return V2CheckpointEvaluationCallback


def _metrics_from_payload(payload: dict[str, Any]):
    """Rehydrate EvaluationMetrics from its own to_dict() projection."""

    from .metrics import EvaluationMetrics

    fields = {
        key: value
        for key, value in payload.items()
        if key in EvaluationMetrics.__dataclass_fields__
    }
    return EvaluationMetrics(**fields)


def _train_stage(
    config: V2TrainingConfig,
    stage: V2StageConfig,
    stage_dir: Path,
    previous_checkpoint: Path | None,
    sts2_gym: Any,
    resume: bool = False,
    ppo_seed: int | None = None,
) -> tuple[bool, Path]:
    from sb3_contrib import MaskablePPO
    from stable_baselines3.common.callbacks import BaseCallback
    from stable_baselines3.common.vec_env import DummyVecEnv

    from .curriculum import _checkpoint_global_steps

    stream_suffix = ":resume" if resume else ""
    env_factory = _environment_factory(config, stage, sts2_gym)
    vector_env = DummyVecEnv(
        [
            _training_environment_factory(
                config,
                stage,
                sts2_gym,
                rank,
                stage.parallel_envs,
                stream_suffix=stream_suffix,
            )
            for rank in range(stage.parallel_envs)
        ]
    )
    contract_sha = hashlib.sha256(
        json.dumps(observation_contract(), sort_keys=True).encode("utf-8")
    ).hexdigest()
    try:
        resume_base_steps = 0
        if previous_checkpoint is not None and (resume or stage.initialize_from_previous):
            try:
                model = MaskablePPO.load(
                    previous_checkpoint,
                    env=vector_env,
                    device=config.algorithm.device,
                )
            except ValueError as exc:
                raise RuntimeError(
                    f"V2 stage {stage.name} cannot initialize from "
                    f"{previous_checkpoint}; observation/action spaces must match"
                ) from exc
            if resume:
                resume_base_steps = _checkpoint_global_steps(previous_checkpoint)
        elif stage.initialize_from_previous:
            raise RuntimeError(
                f"V2 stage {stage.name} requires a previous checkpoint but none exists"
            )
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

        if ppo_seed is not None:
            # Seed only the PPO side (network init + sampling entropy).  The
            # env SeedStream namespace is untouched, so ablation arms that
            # share a config see byte-identical training episode seeds.
            model.set_random_seed(int(ppo_seed))

        checkpoint_seeds = config.partition(stage.name, "checkpoint").seeds(
            stage.checkpoint_eval_episodes
        )
        promotion_seeds = config.partition(stage.name, "promotion").seeds(
            stage.promotion_eval_episodes
        )
        callback_type = _callback_class(BaseCallback)
        callback = callback_type(
            stage=stage,
            stage_dir=stage_dir,
            env_factory=env_factory,
            checkpoint_seeds=checkpoint_seeds,
            promotion_seeds=promotion_seeds,
            resume_base_steps=resume_base_steps,
            observation_sha=contract_sha,
            reward_config=config.reward,
        )
        remaining = max(stage.timesteps - resume_base_steps, 0)
        if remaining:
            model.learn(total_timesteps=remaining, callback=callback)

        if callback.early_promotion is not None:
            promoted_checkpoint: Path = callback.early_promotion["checkpoint"]
            decision = callback.early_promotion["decision"]
            atomic_write_json(stage_dir / "promotion_decision.json", decision.to_dict())
            return decision.promoted, promoted_checkpoint

        final_checkpoint = stage_dir / "checkpoints" / "final"
        final_checkpoint.parent.mkdir(parents=True, exist_ok=True)
        model.save(final_checkpoint)
        final_checkpoint = final_checkpoint.with_suffix(".zip")
        payload = callback._evaluate(promotion_seeds, "promotion", final_checkpoint)
        atomic_write_json(stage_dir / "metrics" / "promotion.json", payload)
        decision = decide_promotion(
            _metrics_from_payload(payload), _promotion_config(stage)
        )
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


def _emulator_provenance(config: V2TrainingConfig, project_root: Path) -> dict:
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
        "run_native_api_version": 8,
    }


def plan(config: V2TrainingConfig, project_root: Path) -> dict:
    return {
        "v2_config_version": config.version,
        "character": config.character,
        "ascension": config.ascension,
        "game_branch": config.game_branch,
        "save_load": config.save_load,
        "emulator": _emulator_provenance(config, project_root),
        "observation_contract": observation_contract(),
        "reward": dataclasses.asdict(config.reward),
        "stages": [
            {
                "name": stage.name,
                "timesteps": stage.timesteps,
                "parallel_envs": stage.parallel_envs,
                "max_floor": stage.max_floor,
                "checkpoint_every_steps": stage.checkpoint_every_steps,
                "promotion_probe_every_steps": stage.promotion_probe_every_steps,
                "initialize_from_previous": stage.initialize_from_previous,
                "scope": stage.scope,
                "promotion": {
                    "episodes": stage.promotion_eval_episodes,
                    "min_boundary_rate": stage.min_boundary_rate,
                    "min_boundary_wilson_lower": stage.min_boundary_wilson_lower,
                    "min_win_rate": stage.min_win_rate,
                    "min_wilson_lower": stage.min_wilson_lower,
                    "max_truncation_rate": stage.max_truncation_rate,
                    "max_illegal_actions": stage.max_illegal_actions,
                    "unclassified_dead_ends_allowed": 0,
                },
                "seeds": {
                    split: {
                        "start": config.partition(stage.name, split).start,
                        "count": config.partition(stage.name, split).count,
                    }
                    for split in ("train", "checkpoint", "promotion", "final")
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
        description="V2 floor-curriculum training (Act 1, simulator only)"
    )
    parser.add_argument(
        "--config", type=Path, default=Path("config/training_v2.toml")
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--only-stage", type=str)
    parser.add_argument("--initial-checkpoint", type=Path)
    parser.add_argument("--resume-run", type=Path)
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    config_path = _resolve(project_root, args.config)
    config = load_v2_training_config(config_path)
    selected = tuple(
        stage
        for stage in config.stages
        if args.only_stage is None or stage.name == args.only_stage
    )
    if args.only_stage is not None and not selected:
        raise SystemExit(f"--only-stage {args.only_stage!r} is not a configured stage")
    if args.dry_run:
        print(json.dumps(plan(config, project_root), ensure_ascii=False, indent=2))
        return

    emulator_root = _resolve(project_root, config.emulator_root)
    sys.path.insert(0, str(emulator_root / "src"))
    import sts2_gym

    resume = args.resume_run is not None
    if resume:
        run_root = _resolve(project_root, args.resume_run)
        if not run_root.is_dir():
            raise SystemExit(f"--resume-run directory does not exist: {run_root}")
        if (run_root / "plan.json").is_file():
            recorded = json.loads((run_root / "plan.json").read_text(encoding="utf-8"))
            current = plan(config, project_root)
            for key in ("stages", "seed_partitions", "observation_contract"):
                if recorded.get(key) != current.get(key):
                    raise SystemExit(
                        f"--resume-run plan.json disagrees with the current config "
                        f"on {key!r}; refusing to mix incompatible V2 plans"
                    )
    else:
        run_id = datetime.now(UTC).strftime("v2curriculum-%Y%m%dT%H%M%SZ")
        run_root = _resolve(project_root, config.output_dir) / run_id
        run_root.mkdir(parents=True, exist_ok=False)
        atomic_write_json(run_root / "plan.json", plan(config, project_root))

    previous_checkpoint = (
        _resolve(project_root, args.initial_checkpoint)
        if args.initial_checkpoint is not None
        else None
    )
    for stage in selected:
        stage_dir = run_root / stage.name
        stage_dir.mkdir(parents=True, exist_ok=resume)
        if resume:
            existing = sorted(
                (stage_dir / "checkpoints").glob("step_*.zip"),
                key=lambda path: int(path.stem.split("_", 1)[1]),
            )
            if existing:
                previous_checkpoint = existing[-1]
                atomic_write_json(
                    stage_dir
                    / f"resume-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json",
                    {
                        "resumed_from": str(previous_checkpoint),
                        "resumed_from_sha256": _sha256_file(previous_checkpoint),
                        "resumed_at": datetime.now(UTC).isoformat(),
                    },
                )
        promoted, previous_checkpoint = _train_stage(
            config, stage, stage_dir, previous_checkpoint, sts2_gym, resume=resume
        )
        if not promoted:
            raise SystemExit(
                f"V2 stage {stage.name} did not meet its promotion gate; see "
                f"{stage_dir / 'promotion_decision.json'}"
            )


if __name__ == "__main__":
    main()
