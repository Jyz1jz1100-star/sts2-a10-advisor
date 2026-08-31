"""Verify the step-cap guard against the real hang seed (diagnostic only).

Uses the aborted run's checkpoint (seed partition 20M, diagnostics) and the
exact seed that produced the 9.17M-step evaluation loop, through the fixed
evaluate_policy path. Expected before-fix: infinite loop; after-fix: one
episode truncated at stage.max_episode_steps in seconds.
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
from training.evaluation import evaluate_policy  # noqa: E402

CHECKPOINT = (
    ROOT
    / "runs/curriculum/curriculum-20260831T163118Z/act1/checkpoints/step_000002000004.zip"
)
HANG_SEED = 20_000_043


def main() -> int:
    config = load_training_config(ROOT / "config" / "training.toml")
    stage = config.stage("act1")
    model = MaskablePPO.load(CHECKPOINT)

    def env_factory(seed: int) -> Sts2RunEnv:
        return Sts2RunEnv(
            seed=seed,
            max_episode_steps=stage.max_episode_steps,
            max_floors=stage.max_floors or 16,
        )

    started = time.perf_counter()
    metrics = evaluate_policy(
        model,
        env_factory=env_factory,
        seeds=[HANG_SEED],
        stage="act1",
        split="checkpoint",
        scope="simulator_act1",
        checkpoint=str(CHECKPOINT),
        max_steps_per_episode=stage.max_episode_steps,
    )
    elapsed = time.perf_counter() - started
    print(
        f"hang seed {HANG_SEED}: episodes={metrics.episodes} steps={metrics.mean_steps:.0f} "
        f"truncation_rate={metrics.truncation_rate} in {elapsed:.1f}s "
        f"-> guard {'WORKS' if elapsed < 120 else 'SLOW?'}"
    )
    return 0 if elapsed < 120 else 1


if __name__ == "__main__":
    raise SystemExit(main())
