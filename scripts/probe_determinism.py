"""Quick determinism probe of the native run engine (diagnostic only).

Same checkpoint + same seed:
  1. twice inside one process;
  2. once here (a fresh process).
If in-process replays agree but differ from a previous process's result, the
native engine carries cross-episode process-global state: per-seed outcomes
are not reproducible across processes. Checkpoint partition seeds only.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "third_party" / "slay-the-spire-2-emulator-main" / "src"))

from sb3_contrib import MaskablePPO  # noqa: E402
from sts2_gym import Sts2RunEnv  # noqa: E402
from training.config import load_training_config  # noqa: E402

CHECKPOINT = (
    ROOT / "runs/curriculum/curriculum-20260831T205351Z/act1/checkpoints/step_000002000004.zip"
)
SEED = 20_000_000


def play(model, config, seed: int, label: str) -> dict[str, object]:
    stage = config.stage("act1")
    env = Sts2RunEnv(seed=seed, max_episode_steps=stage.max_episode_steps,
                     max_floors=stage.max_floors or 16)
    observation, info = env.reset(seed=seed)
    steps = 0
    terminated = truncated = False
    trajectory: list[tuple[int, int]] = []
    try:
        while not (terminated or truncated) and steps < stage.max_episode_steps:
            mask = env.action_masks()
            action, _ = model.predict(observation, action_masks=mask, deterministic=True)
            action = int(action)
            if action < 0 or action >= len(mask) or not bool(mask[action]):
                truncated = True
                break
            observation, _, terminated, truncated, info = env.step(action)
            trajectory.append((steps, action))
            steps += 1
    finally:
        env.close()
    result = {
        "label": label,
        "won": bool(info.get("player_won")),
        "floor": int(info.get("floor", -1)),
        "steps": steps,
        "terminated": terminated,
        "truncated": truncated,
        "action_hash": hash(tuple(trajectory)) & 0xFFFFFFFF,
    }
    print(result, flush=True)
    return result


def main() -> int:
    config = load_training_config(ROOT / "config" / "training.toml")
    model = MaskablePPO.load(CHECKPOINT)
    first = play(model, config, SEED, "in-process#1")
    time.sleep(0.2)
    second = play(model, config, SEED, "in-process#2")
    print(f"in-process identical: {first['action_hash'] == second['action_hash']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
