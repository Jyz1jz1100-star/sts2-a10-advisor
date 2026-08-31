from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .seeds import SeedPartition, SeedPartitions


@dataclass(frozen=True)
class PromotionConfig:
    min_episodes: int
    min_win_rate: float
    min_wilson_lower: float
    max_truncation_rate: float
    max_illegal_actions: int
    min_mean_floor: float | None = None


@dataclass(frozen=True)
class StageConfig:
    name: str
    environment: str
    timesteps: int
    parallel_envs: int
    checkpoint_every_steps: int
    checkpoint_eval_episodes: int
    promotion_eval_episodes: int
    max_episode_steps: int
    max_floors: int | None
    initialize_from_previous: bool
    experimental: bool
    promotion: PromotionConfig


@dataclass(frozen=True)
class AlgorithmConfig:
    device: str
    n_steps: int
    batch_size: int
    n_epochs: int
    gamma: float
    learning_rate: float
    entropy_coefficient: float


@dataclass(frozen=True)
class TrainingConfig:
    character: str
    ascension: int
    game_branch: str
    save_load: bool
    emulator_root: Path
    output_dir: Path
    stages: tuple[StageConfig, ...]
    algorithm: AlgorithmConfig
    seeds: SeedPartitions

    def stage(self, name: str) -> StageConfig:
        for stage in self.stages:
            if stage.name == name:
                return stage
        raise KeyError(name)


def _require(table: dict[str, Any], key: str, expected: type) -> Any:
    if key not in table:
        raise ValueError(f"missing required configuration key: {key}")
    value = table[key]
    if not isinstance(value, expected):
        raise ValueError(f"configuration key {key} must be {expected.__name__}")
    return value


def load_training_config(path: Path) -> TrainingConfig:
    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    target = raw.get("target", {})
    runtime = raw.get("runtime", {})
    curriculum = raw.get("curriculum", {})
    algorithm_raw = raw.get("algorithm", {})
    stage_tables = raw.get("stages", {})

    stage_names = _require(curriculum, "stages", list)
    stages: list[StageConfig] = []
    for name in stage_names:
        if not isinstance(name, str) or name not in stage_tables:
            raise ValueError(f"curriculum stage {name!r} has no [stages.{name}] table")
        table = stage_tables[name]
        promotion_raw = _require(table, "promotion", dict)
        promotion = PromotionConfig(
            min_episodes=int(_require(promotion_raw, "min_episodes", int)),
            min_win_rate=float(_require(promotion_raw, "min_win_rate", float)),
            min_wilson_lower=float(_require(promotion_raw, "min_wilson_lower", float)),
            max_truncation_rate=float(
                _require(promotion_raw, "max_truncation_rate", float)
            ),
            max_illegal_actions=int(
                _require(promotion_raw, "max_illegal_actions", int)
            ),
            min_mean_floor=(
                float(promotion_raw["min_mean_floor"])
                if "min_mean_floor" in promotion_raw
                else None
            ),
        )
        stage = StageConfig(
            name=name,
            environment=_require(table, "environment", str),
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
            max_floors=(int(table["max_floors"]) if "max_floors" in table else None),
            initialize_from_previous=bool(table.get("initialize_from_previous", False)),
            experimental=bool(table.get("experimental", False)),
            promotion=promotion,
        )
        _validate_stage(stage)
        stages.append(stage)

    seed_tables = _require(raw, "seeds", dict)
    partitions = SeedPartitions(
        [
            SeedPartition(
                name=name, start=int(values["start"]), count=int(values["count"])
            )
            for name, values in seed_tables.items()
        ]
    )
    for required_split in ("train", "checkpoint", "promotion", "final"):
        try:
            partitions[required_split]
        except KeyError as exc:
            raise ValueError(f"missing seed partition {required_split!r}") from exc

    config = TrainingConfig(
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
        seeds=partitions,
    )
    if config.save_load:
        raise ValueError(
            "training and evaluation configuration must keep save_load=false"
        )
    if config.character != "IRONCLAD" or config.ascension != 10:
        raise ValueError(
            "this curriculum is locked to the emulator's IRONCLAD A10 target"
        )
    checkpoint_count = config.seeds["checkpoint"].count
    promotion_count = config.seeds["promotion"].count
    for stage in config.stages:
        if stage.parallel_envs > config.seeds["train"].count:
            raise ValueError(
                f"stage {stage.name}: train partition is too small for workers"
            )
        if stage.checkpoint_eval_episodes > checkpoint_count:
            raise ValueError(
                f"stage {stage.name}: checkpoint evaluation exceeds its seed partition"
            )
        if stage.promotion_eval_episodes > promotion_count:
            raise ValueError(
                f"stage {stage.name}: promotion evaluation exceeds its seed partition"
            )
        if stage.promotion.min_episodes > stage.promotion_eval_episodes:
            raise ValueError(
                f"stage {stage.name}: promotion min_episodes exceeds evaluation episodes"
            )
    return config


def _validate_stage(stage: StageConfig) -> None:
    if stage.environment not in {"combat", "run"}:
        raise ValueError(f"stage {stage.name}: environment must be combat or run")
    integer_values = (
        stage.timesteps,
        stage.parallel_envs,
        stage.checkpoint_every_steps,
        stage.checkpoint_eval_episodes,
        stage.promotion_eval_episodes,
        stage.max_episode_steps,
        stage.promotion.min_episodes,
    )
    if any(value <= 0 for value in integer_values):
        raise ValueError(f"stage {stage.name}: counts and step limits must be positive")
    rates = (
        stage.promotion.min_win_rate,
        stage.promotion.min_wilson_lower,
        stage.promotion.max_truncation_rate,
    )
    if any(rate < 0 or rate > 1 for rate in rates):
        raise ValueError(f"stage {stage.name}: promotion rates must be in [0, 1]")
    if stage.environment == "combat" and stage.max_floors is not None:
        raise ValueError(f"stage {stage.name}: combat stage cannot set max_floors")
