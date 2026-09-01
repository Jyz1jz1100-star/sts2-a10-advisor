"""Empirically pin down SB3 resume semantics for the 10M act1 checkpoint.

Question answered: when calling model.learn(N) on a loaded MaskablePPO,
(a) does num_timesteps continue from the checkpoint or reset, and
(b) is N additional steps or an absolute target?
Tiny 1-iteration budget; checkpoint-split seeds not used (train seed).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "third_party" / "slay-the-spire-2-emulator-main" / "src"))

import gymnasium as gym  # noqa: E402
from sb3_contrib import MaskablePPO  # noqa: E402
from sb3_contrib.common.wrappers import ActionMasker  # noqa: E402
from stable_baselines3.common.vec_env import DummyVecEnv  # noqa: E402
from sts2_gym import Sts2RunEnv  # noqa: E402

CHECKPOINT = (
    ROOT / "runs/curriculum/curriculum-20260831T205351Z/act1/checkpoints/step_000010000020.zip"
)


def main() -> int:
    def make():
        env = Sts2RunEnv(seed=7_777_777, max_episode_steps=1200, max_floors=16)

        class W(gym.Wrapper):
            def reset(self, *, seed=None, options=None):
                return self.env.reset(seed=7_777_777)

        return ActionMasker(W(env), lambda e: e.unwrapped.action_masks())

    vec = DummyVecEnv([make])
    model = MaskablePPO.load(CHECKPOINT, env=vec, device="cpu")
    before = model.num_timesteps
    model.learn(total_timesteps=256)
    after = model.num_timesteps
    print(f"before={before} after={after} delta={after - before}")
    print(
        "semantics:",
        "num_timesteps RESET to 0-based (learn resets)"
        if after < before
        else "num_timesteps CONTINUES; learn(N) collects N more"
        if after - before >= 256
        else "num_timesteps continues; N is ABSOLUTE target",
    )
    vec.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
