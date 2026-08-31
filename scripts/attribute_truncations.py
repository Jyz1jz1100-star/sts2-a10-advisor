"""Attribute truncated episodes in a checkpoint evaluation to causes.

Replays the checkpoint-split evaluation for a saved act1 checkpoint and, for
each episode that hits the step cap, classifies the ending:

- ``native-stall``: the final >=TRAIL steps were consecutive -1.0 rewards on
  an unchanged observation (mask/native disagreement soft-lock, e.g.
  event_id=31 or empty-mask NODE_SHOP map states);
- ``long-episode``: genuine failure to finish 16 floors inside the cap.

Prints per-episode rows plus a summary counts block. Checkpoint partition
seeds only; read-only; complements find_illegal_episode.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "third_party" / "slay-the-spire-2-emulator-main" / "src"))

from sb3_contrib import MaskablePPO  # noqa: E402
from sts2_gym import Sts2RunEnv  # noqa: E402
import sts2_gym.run_constants as constants  # noqa: E402
from training.config import load_training_config  # noqa: E402

TRAIL = 60  # consecutive rejected steps to call a stall
PHASE_NAMES = {
    getattr(constants, name): name.removeprefix("PHASE_").lower()
    for name in dir(constants)
    if name.startswith("PHASE_")
}


def classify(checkpoint: Path, episodes: int) -> int:
    config = load_training_config(ROOT / "config" / "training.toml")
    stage = config.stage("act1")
    model = MaskablePPO.load(checkpoint)
    seeds = config.seeds["checkpoint"].seeds(episodes)
    counts = {"win": 0, "death": 0, "native-stall": 0, "long-episode": 0}
    for seed in seeds:
        env = Sts2RunEnv(seed=seed, max_episode_steps=stage.max_episode_steps,
                         max_floors=stage.max_floors or 16)
        reward_tail: list[float] = []
        obs_prev = None
        stall_at = None
        try:
            observation, info = env.reset(seed=seed)
            terminated = truncated = False
            steps = 0
            while not (terminated or truncated) and steps < stage.max_episode_steps:
                mask = env.action_masks()
                action, _ = model.predict(observation, action_masks=mask, deterministic=True)
                action = int(action)
                if action < 0 or action >= len(mask) or not bool(mask[action]):
                    stall_at = ("empty-or-violating-mask", PHASE_NAMES.get(int(info.get("phase", -1)), "?"))
                    truncated = True
                    break
                obs_prev = observation
                observation, reward, terminated, truncated, info = env.step(action)
                reward_tail.append(float(reward))
                if len(reward_tail) > TRAIL:
                    reward_tail.pop(0)
                steps += 1
                if (
                    len(reward_tail) == TRAIL
                    and all(item == -1.0 for item in reward_tail)
                    and np.array_equal(observation, obs_prev)
                ):
                    stall_at = ("native-rejection-loop", PHASE_NAMES.get(int(info.get("phase", -1)), "?"), int(info.get("floor", -1)))
        finally:
            final_info = info
            final_steps = steps
            env.close()
        if terminated:
            key = "win" if final_info.get("player_won") else "death"
        elif stall_at is not None:
            key = "native-stall"
            print(f"  stall seed={seed} steps={final_steps} detail={stall_at}")
        else:
            key = "long-episode"
            print(f"  long  seed={seed} steps={final_steps} floor={final_info.get('floor')} "
                  f"phase={PHASE_NAMES.get(int(final_info.get('phase', -1)), '?')} "
                  f"hp={final_info.get('player_hp')}")
        counts[key] += 1
    print(f"SUMMARY episodes={episodes} {counts}")
    return 0


if __name__ == "__main__":
    ckpt = (
        ROOT
        / "runs/curriculum/curriculum-20260831T205351Z/act1/checkpoints/step_000002000004.zip"
    )
    raise SystemExit(classify(ckpt, 100))
