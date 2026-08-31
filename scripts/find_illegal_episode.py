"""Find which checkpoint-split episodes had the 1 illegal action at 2M steps.

The 2M evaluation reported illegal_actions=1 over 100 episodes: the trained
policy picked an action outside env.action_masks() somewhere. Deterministic
replay of evaluate_policy per seed with detailed logging to identify the
seed/step/phase, so the follow-up (native mask edge or sb3_contrib bug) can
be pinned down. Read-only against run dirs; checkpoint partition only.
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

CHECKPOINT = (
    ROOT / "runs/curriculum/curriculum-20260831T205351Z/act1/checkpoints/step_000002000004.zip"
)
PHASE_NAMES = {
    getattr(constants, name): name.removeprefix("PHASE_").lower()
    for name in dir(constants)
    if name.startswith("PHASE_")
}


def main() -> int:
    config = load_training_config(ROOT / "config" / "training.toml")
    stage = config.stage("act1")
    model = MaskablePPO.load(CHECKPOINT)
    seeds = config.seeds["checkpoint"].seeds(stage.checkpoint_eval_episodes)
    hits = 0
    for seed in seeds:
        env = Sts2RunEnv(seed=seed, max_episode_steps=stage.max_episode_steps,
                         max_floors=stage.max_floors or 16)
        try:
            observation, info = env.reset(seed=seed)
            terminated = truncated = False
            steps = 0
            while not (terminated or truncated) and steps < stage.max_episode_steps:
                mask = env.action_masks()
                action, _ = model.predict(observation, action_masks=mask, deterministic=True)
                action = int(action)
                if action < 0 or action >= len(mask) or not bool(mask[action]):
                    hits += 1
                    legal = [int(i) for i in np.flatnonzero(mask)]
                    phase = PHASE_NAMES.get(int(info.get("phase", -1)), "?")
                    print(f"ILLEGAL seed={seed} step={steps} action={action} "
                          f"mask_legal={legal} mask_sum={int(mask.sum())} "
                          f"phase={phase} floor={info.get('floor')} "
                          f"node_type={info.get('current_node_type')}")
                    break
                observation, _, terminated, truncated, info = env.step(action)
                steps += 1
        finally:
            env.close()
        if hits >= 5:
            print("stopping after 5 hits")
            break
    print(f"total illegal episodes found: {hits}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
