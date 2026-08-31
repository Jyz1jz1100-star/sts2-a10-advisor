"""Time one deterministic episode on a saved checkpoint (diagnostic only).

Loads the live run's 2M-step checkpoint against one checkpoint-split seed and
measures ms/step of the exact predict loop evaluation uses, to project the
in-flight 100-episode evaluation's wall time. No writes, no promotion/final
seeds.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "third_party" / "slay-the-spire-2-emulator-main" / "src"))

from sb3_contrib import MaskablePPO  # noqa: E402
from sts2_gym import Sts2RunEnv  # noqa: E402

CHECKPOINT = ROOT / "runs/curriculum/curriculum-20260831T163118Z/act1/checkpoints/step_000002000004.zip"
SEED = 20_000_005  # checkpoint partition: diagnostics only


def main() -> int:
    model = MaskablePPO.load(CHECKPOINT)
    env = Sts2RunEnv(seed=SEED, max_episode_steps=1200, max_floors=16)
    observation, _ = env.reset(seed=SEED)
    steps = 0
    terminated = truncated = False
    started = time.perf_counter()
    try:
        while not (terminated or truncated) and steps < 1200:
            mask = env.action_masks()
            action, _ = model.predict(observation, action_masks=mask, deterministic=True)
            observation, _, terminated, truncated, info = env.step(int(action))
            steps += 1
    finally:
        env.close()
    elapsed = time.perf_counter() - started
    ms = elapsed / max(steps, 1) * 1000
    print(f"device={model.device} steps={steps} seconds={elapsed:.1f} ms_per_step={ms:.1f}")
    print(f"floor={info.get('floor')} won={info.get('player_won')} truncated={truncated}")
    print(f"projection for 120k-step worst case: {120000 * ms / 1000 / 60:.0f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
