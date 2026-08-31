from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_training_config
from .evaluation import evaluate_policy
from .metrics import atomic_write_json


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate a curriculum checkpoint")
    parser.add_argument("--config", type=Path, default=Path("config/training.toml"))
    parser.add_argument("--emulator-root", type=Path)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument(
        "--stage", choices=("combat", "act1", "full_run"), required=True
    )
    parser.add_argument(
        "--split", choices=("checkpoint", "promotion", "final"), default="checkpoint"
    )
    parser.add_argument("--episodes", type=int, default=100)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    config_path = (
        args.config if args.config.is_absolute() else project_root / args.config
    )
    config = load_training_config(config_path)
    stage = config.stage(args.stage)
    emulator_root = args.emulator_root or config.emulator_root
    if not emulator_root.is_absolute():
        emulator_root = (project_root / emulator_root).resolve()
    sys.path.insert(0, str(emulator_root / "src"))
    from sb3_contrib import MaskablePPO
    from sts2_gym import Sts2CombatEnv, Sts2RunEnv

    model = MaskablePPO.load(args.checkpoint)

    def env_factory(seed: int):
        if stage.environment == "combat":
            return Sts2CombatEnv(seed=seed, max_episode_steps=stage.max_episode_steps)
        return Sts2RunEnv(
            seed=seed,
            max_episode_steps=stage.max_episode_steps,
            max_floors=stage.max_floors or 16,
        )

    seeds = config.seeds[args.split].seeds(args.episodes)
    metrics = evaluate_policy(
        model,
        env_factory=env_factory,
        seeds=seeds,
        stage=stage.name,
        split=args.split,
        scope=(
            "simulator_combat"
            if stage.environment == "combat"
            else f"simulator_{stage.name}{'_experimental' if stage.experimental else ''}"
        ),
        checkpoint=args.checkpoint,
        experimental=stage.experimental,
        max_steps_per_episode=stage.max_episode_steps,
    )
    payload = metrics.to_dict()
    if args.output:
        atomic_write_json(args.output, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
