"""V2 curriculum configuration: per-stage seeds, floor boundaries, reward.

Design rules encoded here (from the V2 training plan):

* every stage owns four *named* seed partitions --
  ``<stage>.train/checkpoint/promotion/final`` -- and one global
  :class:`SeedPartitions` validates that *no two partitions across all stages
  overlap*.  V1's single shared promotion partition meant every stage re-used
  the same seeds; V2 gives each curriculum level untouched seeds.
* ``max_floor`` is the stage's curriculum boundary enforced by the V2 run
  contract wrapper (the native ``max_floors`` parameter was V1's
  silently-ineffective knob and is not used here).
* the metric scope is always ``simulator_act1``-rooted so no artifact can be
  mistaken for real-game evidence.
"""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .config import AlgorithmConfig
from .seeds import SeedPartition, SeedPartitions
from .v2_run_wrapper import V2RewardConfig

V2_CONFIG_VERSION = 1

#: The full-run boundary of Act 1 on the locked emulator build.
ACT1_FINAL_FLOOR = 16


@dataclass(frozen=True)
class V2StageConfig:
    name: str
    timesteps: int
    parallel_envs: int
    checkpoint_every_steps: int
    checkpoint_eval_episodes: int
    promotion_eval_episodes: int
    max_episode_steps: int
    #: Curriculum boundary enforced by the V2 contract wrapper.  ``None`` means
    #: the stage runs to the natural Act 1 terminal state.
    max_floor: int | None
    initialize_from_previous: bool
    promotion_probe_every_steps: int
    #: Promotion gate: fraction of episodes reaching the boundary (or win).
    min_boundary_rate: float
    #: Wilson 95% lower bound required of the boundary rate itself
    #: (review item 4).  ``None`` keeps the historical point-only gate.
    min_boundary_wilson_lower: float | None
    min_win_rate: float
    min_wilson_lower: float
    max_truncation_rate: float
    max_illegal_actions: int
    min_episodes: int = 500
    #: Explicit train seed list (JSON ``{"seeds": [...], "generated_act": N}``).
    #: Used when a contiguous range cannot express the stage's population: the
    #: emulator picks the act per seed, so about half of any "Act 1" range
    #: actually generates Act 2.  Resolved against the config file's directory
    #: at load time.  Only *train* seeds are affected -- checkpoint, promotion
    #: and final partitions stay ranges, so a filtered arm remains comparable
    #: with the unfiltered ones.
    train_seeds_file: str | None = None

    @property
    def scope(self) -> str:
        return "simulator_act1"


@dataclass(frozen=True)
class V2TrainingConfig:
    version: int
    character: str
    ascension: int
    game_branch: str
    save_load: bool
    emulator_root: Path
    output_dir: Path
    stages: tuple[V2StageConfig, ...]
    algorithm: AlgorithmConfig
    reward: V2RewardConfig
    seeds: SeedPartitions

    def stage(self, name: str) -> V2StageConfig:
        for stage in self.stages:
            if stage.name == name:
                return stage
        raise KeyError(name)

    def partition(self, stage_name: str, split: str) -> SeedPartition:
        return self.seeds[f"{stage_name}.{split}"]


def _require(table: dict, key: str, expected: type):
    if key not in table:
        raise ValueError(f"missing required V2 configuration key: {key}")
    value = table[key]
    if not isinstance(value, expected):
        raise ValueError(f"V2 configuration key {key} must be {expected.__name__}")
    return value


def load_v2_training_config(path: Path) -> V2TrainingConfig:
    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    if int(raw.get("version", 0)) != V2_CONFIG_VERSION:
        raise ValueError(f"unsupported V2 config version {raw.get('version')!r}")

    target = raw.get("target", {})
    runtime = raw.get("runtime", {})
    algorithm_raw = raw.get("algorithm", {})
    reward_raw = raw.get("reward", {})
    curriculum = raw.get("curriculum", {})
    stage_tables = raw.get("stages", {})

    stage_names = _require(curriculum, "stages", list)
    stages: list[V2StageConfig] = []
    partitions: list[SeedPartition] = []
    for name in stage_names:
        if not isinstance(name, str) or name not in stage_tables:
            raise ValueError(f"V2 stage {name!r} has no [stages.{name}] table")
        table = stage_tables[name]
        seed_tables = _require(table, "seeds", dict)
        stage_partition_names = ("train", "checkpoint", "promotion", "final")
        for split in stage_partition_names:
            values = _require(seed_tables, split, dict)
            partitions.append(
                SeedPartition(
                    name=f"{name}.{split}",
                    start=int(_require(values, "start", int)),
                    count=int(_require(values, "count", int)),
                )
            )
        stage = V2StageConfig(
            name=name,
            timesteps=int(_require(table, "timesteps", int)),
            parallel_envs=int(_require(table, "parallel_envs", int)),
            checkpoint_every_steps=int(_require(table, "checkpoint_every_steps", int)),
            checkpoint_eval_episodes=int(
                _require(table, "checkpoint_eval_episodes", int)
            ),
            promotion_eval_episodes=int(
                _require(table, "promotion_eval_episodes", int)
            ),
            max_episode_steps=int(_require(table, "max_episode_steps", int)),
            max_floor=(int(table["max_floor"]) if "max_floor" in table else None),
            initialize_from_previous=bool(table.get("initialize_from_previous", False)),
            train_seeds_file=(
                None
                if "train_seeds_file" not in table
                else str(
                    (path.parent / str(table["train_seeds_file"])).resolve()
                    if not Path(str(table["train_seeds_file"])).is_absolute()
                    else Path(str(table["train_seeds_file"]))
                )
            ),
            promotion_probe_every_steps=int(
                table.get("promotion_probe_every_steps", 0)
            ),
            min_boundary_rate=float(_require(table, "min_boundary_rate", float)),
            min_boundary_wilson_lower=(
                float(table["min_boundary_wilson_lower"])
                if "min_boundary_wilson_lower" in table
                else None
            ),
            min_win_rate=float(table.get("min_win_rate", 0.0)),
            min_wilson_lower=float(table.get("min_wilson_lower", 0.0)),
            max_truncation_rate=float(
                _require(table, "max_truncation_rate", float)
            ),
            max_illegal_actions=int(_require(table, "max_illegal_actions", int)),
            min_episodes=int(table.get("min_episodes", 500)),
        )
        _validate_stage(stage)
        stages.append(stage)

    # A single global object validates pairwise disjointness across ALL stages.
    seed_partitions = SeedPartitions(partitions)

    reward = V2RewardConfig(
        gamma=float(reward_raw.get("gamma", 0.99)),
        combat_reward_scale=float(reward_raw.get("combat_reward_scale", 0.10)),
        floor_weight=float(reward_raw.get("floor_weight", 1.0)),
        hp_weight=float(reward_raw.get("hp_weight", 1.0)),
        terminal_win_reward=float(reward_raw.get("terminal_win_reward", 25.0)),
        terminal_death_reward=float(reward_raw.get("terminal_death_reward", -25.0)),
        boundary_success_reward=float(
            reward_raw.get("boundary_success_reward", 3.0)
        ),
        step_cost=float(reward_raw.get("step_cost", 0.0)),
        first_floor_advance_reward=float(
            reward_raw.get("first_floor_advance_reward", 0.0)
        ),
    )

    config = V2TrainingConfig(
        version=V2_CONFIG_VERSION,
        character=_require(target, "character", str),
        ascension=int(_require(target, "ascension", int)),
        game_branch=_require(target, "game_branch", str),
        save_load=bool(_require(target, "save_load", bool)),
        emulator_root=Path(_require(runtime, "emulator_root", str)),
        output_dir=Path(_require(runtime, "run_dir", str)),
        stages=tuple(stages),
        algorithm=AlgorithmConfig(
            device=_require(algorithm_raw, "device", str),
            n_steps=int(_require(algorithm_raw, "n_steps", int)),
            batch_size=int(_require(algorithm_raw, "batch_size", int)),
            n_epochs=int(_require(algorithm_raw, "n_epochs", int)),
            gamma=float(_require(algorithm_raw, "gamma", float)),
            learning_rate=float(_require(algorithm_raw, "learning_rate", float)),
            entropy_coefficient=float(
                _require(algorithm_raw, "entropy_coefficient", float)
            ),
        ),
        reward=reward,
        seeds=seed_partitions,
    )
    if config.save_load:
        raise ValueError("V2 training configuration must keep save_load=false")
    if config.character != "IRONCLAD" or config.ascension != 10:
        raise ValueError("this curriculum is locked to the emulator's IRONCLAD A10 target")

    # Review item 2: the shaping horizon inside the potential term must be
    # the SAME discount the PPO learner uses.  A mismatch (the 2026-09-01
    # state: shaping gamma=0.99 under PPO gamma=0.995) quietly biases every
    # shaped advantage, so it is a load-time error now, not a training-time
    # surprise.
    if not math.isclose(
        config.reward.gamma,
        config.algorithm.gamma,
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        raise ValueError(
            "reward.gamma (shaping) must equal algorithm.gamma (PPO); got "
            f"{config.reward.gamma} vs {config.algorithm.gamma}"
        )

    for stage in config.stages:
        if stage.parallel_envs > config.partition(stage.name, "train").count:
            raise ValueError(f"{stage.name}: train partition is too small for workers")
        if stage.checkpoint_eval_episodes > config.partition(
            stage.name, "checkpoint"
        ).count:
            raise ValueError(f"{stage.name}: checkpoint evaluation exceeds its partition")
        if stage.promotion_eval_episodes > config.partition(
            stage.name, "promotion"
        ).count:
            raise ValueError(f"{stage.name}: promotion evaluation exceeds its partition")
        if stage.min_episodes > stage.promotion_eval_episodes:
            raise ValueError(f"{stage.name}: promotion min_episodes exceeds evaluation")
    return config


def _validate_stage(stage: V2StageConfig) -> None:
    integer_values = (
        stage.timesteps,
        stage.parallel_envs,
        stage.checkpoint_every_steps,
        stage.checkpoint_eval_episodes,
        stage.promotion_eval_episodes,
        stage.max_episode_steps,
        stage.min_episodes,
    )
    if any(value <= 0 for value in integer_values):
        raise ValueError(f"stage {stage.name}: counts and step limits must be positive")
    rates = (
        stage.min_boundary_rate,
        stage.min_boundary_wilson_lower or 0.0,
        stage.min_win_rate,
        stage.min_wilson_lower,
        stage.max_truncation_rate,
    )
    if any(rate < 0 or rate > 1 for rate in rates):
        raise ValueError(f"stage {stage.name}: promotion rates must be in [0, 1]")
    if stage.max_floor is not None and not 1 <= stage.max_floor <= ACT1_FINAL_FLOOR:
        raise ValueError(f"stage {stage.name}: max_floor must lie inside Act 1")
    if stage.max_floor is None and stage.min_boundary_rate > 0:
        raise ValueError(
            f"stage {stage.name}: a terminal stage must gate on win rate, not boundary"
        )


__all__ = [
    "ACT1_FINAL_FLOOR",
    "V2_CONFIG_VERSION",
    "V2StageConfig",
    "V2TrainingConfig",
    "load_v2_training_config",
]
