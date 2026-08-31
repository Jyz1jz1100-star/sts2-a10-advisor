"""Pinpoint the mask/native disagreement on the hang seed (diagnostic only).

The aborted run hung on checkpoint seed 20000043: action_masks() reported a
legal action that the native run_step rejected forever (reward=-1, no
termination). This replays the exact episode with the saved 2M checkpoint and
prints the FIRST rejection: step index, chosen action id, mask summary and
phase, so the parity phase has a concrete reproduction instead of a TODO.

Checkpoint partition only; no promotion/final seeds; no writes to run dirs.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "third_party" / "slay-the-spire-2-emulator-main" / "src"))

from sb3_contrib import MaskablePPO  # noqa: E402
from sts2_gym import Sts2RunEnv, native  # noqa: E402
import sts2_gym.run_constants as constants  # noqa: E402

CHECKPOINT = (
    ROOT / "runs/curriculum/curriculum-20260831T163118Z/act1/checkpoints/step_000002000004.zip"
)
SEED = 20_000_043
PHASE_NAMES = {
    getattr(constants, name): name.removeprefix("PHASE_").lower()
    for name in dir(constants)
    if name.startswith("PHASE_")
}


def main() -> int:
    model = MaskablePPO.load(CHECKPOINT)
    env = Sts2RunEnv(seed=SEED, max_episode_steps=1200, max_floors=16)
    observation, info = env.reset(seed=SEED)
    for step in range(1200):
        mask = env.action_masks()
        action, _ = model.predict(observation, action_masks=mask, deterministic=True)
        action = int(action)
        before = observation.copy()
        phase = PHASE_NAMES.get(int(info.get("phase", -1)), info.get("phase"))
        floor = info.get("floor")
        observation, reward, terminated, truncated, info = env.step(action)
        rejected = (
            reward == -1.0
            and not terminated
            and not truncated
            and np.array_equal(observation, before)
        )
        if rejected:
            legal = [int(i) for i in np.flatnonzero(mask)]
            print(f"REJECTION at step {step} (floor={floor}, phase={phase})")
            print(f"  chosen action: {action}")
            print(f"  legal mask ids ({len(legal)}): {legal}")
            target_map = getattr(env, "_target_map", None)
            print(f"  run obs head: {observation[:24].tolist()}")
            return 0
        if terminated or truncated:
            print(f"episode ended at step {step}: terminated={terminated} "
                  f"truncated={truncated} floor={info.get('floor')} won={info.get('player_won')}")
            print("no rejection within cap — hang needs more steps than the cap allows")
            return 0
    print("reached 1200 steps with no rejection")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
