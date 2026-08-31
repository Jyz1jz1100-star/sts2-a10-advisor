"""Reproduce the in-flight checkpoint evaluation to find the slow phase.

Loads the live run's 2M-step checkpoint and runs evaluate-style episodes on
checkpoint-split seeds (diagnostics only), printing per-episode wall time plus
a separate reset timing, to explain why the live evaluation has been running
~3.5h while a single timed episode measured 0.7s. Read-only against run dirs.
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
SEED_START = 20_000_000  # checkpoint partition (same seeds the live eval uses)
EPISODES = int(sys.argv[1]) if len(sys.argv) > 1 else 20


def one_episode(model, seed: int) -> dict[str, object]:
    env = Sts2RunEnv(seed=seed, max_episode_steps=1200, max_floors=16)
    started = time.perf_counter()
    observation, _ = env.reset(seed=seed)
    reset_seconds = time.perf_counter() - started
    steps = 0
    terminated = truncated = False
    predict_seconds = 0.0
    step_seconds = 0.0
    try:
        while not (terminated or truncated) and steps < 1200:
            tick = time.perf_counter()
            mask = env.action_masks()
            action, _ = model.predict(observation, action_masks=mask, deterministic=True)
            predict_seconds += time.perf_counter() - tick
            tick = time.perf_counter()
            observation, _, terminated, truncated, info = env.step(int(action))
            step_seconds += time.perf_counter() - tick
            steps += 1
    finally:
        env.close()
    return {
        "seed": seed,
        "steps": steps,
        "reset_s": round(reset_seconds, 3),
        "predict_s": round(predict_seconds, 3),
        "step_s": round(step_seconds, 3),
        "total_s": round(time.perf_counter() - started, 3),
        "floor": info.get("floor"),
        "won": info.get("player_won"),
        "truncated": truncated,
    }


def main() -> int:
    model = MaskablePPO.load(CHECKPOINT)
    print(f"device={model.device}")
    totals = {"reset": 0.0, "predict": 0.0, "step": 0.0, "all": 0.0, "steps": 0}
    for index in range(EPISODES):
        row = one_episode(model, SEED_START + index)
        totals["reset"] += float(row["reset_s"])
        totals["predict"] += float(row["predict_s"])
        totals["step"] += float(row["step_s"])
        totals["all"] += float(row["total_s"])
        totals["steps"] += int(row["steps"])
        print(row, flush=True)
    print(
        f"SUMMARY episodes={EPISODES} env_steps={totals['steps']} "
        f"reset={totals['reset']:.1f}s predict={totals['predict']:.1f}s "
        f"step={totals['step']:.1f}s total={totals['all']:.1f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
