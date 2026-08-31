"""Micro-benchmark of Sts2RunEnv step throughput (diagnostic only).

The act1 checkpoint evaluation has been running far longer than the combat
stage's equivalent. This script measures the raw environment speed that the
evaluation loop is bottlenecked on, using the *checkpoint* seed partition
(diagnostics are allowed there; final/promotion seeds are never touched) and
a trivial first-legal-action policy — no model, no writes into any run
directory. Keep the budget small so it barely competes with the live trainer.

    python scripts/eval_microbenchmark.py [--steps 2000] [--episodes 3]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EMULATOR_SRC = ROOT.parent / "third_party" / "slay-the-spire-2-emulator-main" / "src"
sys.path.insert(0, str(EMULATOR_SRC))

CHECKPOINT_SEED_START = 20_000_000  # diagnostics partition only


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--steps", type=int, default=2000, help="total env steps budget")
    parser.add_argument("--episodes", type=int, default=3, help="episode budget")
    parser.add_argument("--max-steps-per-episode", type=int, default=1200)
    args = parser.parse_args()

    import numpy as np  # noqa: F401
    from sts2_gym import Sts2RunEnv

    total_steps = 0
    episode_stats: list[dict[str, object]] = []
    wall = 0.0
    for index in range(args.episodes):
        if total_steps >= args.steps:
            break
        seed = CHECKPOINT_SEED_START + index
        env = Sts2RunEnv(seed=seed, max_episode_steps=args.max_steps_per_episode, max_floors=16)
        started = time.perf_counter()
        try:
            observation, info = env.reset(seed=seed)
            steps = 0
            terminated = truncated = False
            while not (terminated or truncated) and total_steps < args.steps:
                mask = env.action_masks()
                legal = np.flatnonzero(mask)
                action = int(legal[0]) if len(legal) else 0
                observation, reward, terminated, truncated, info = env.step(action)
                steps += 1
                total_steps += 1
        finally:
            elapsed = time.perf_counter() - started
            env.close()
        wall += elapsed
        episode_stats.append(
            {
                "seed": seed,
                "steps": steps,
                "seconds": round(elapsed, 2),
                "steps_per_second": round(steps / elapsed, 1) if elapsed else None,
                "terminated": terminated,
                "truncated": truncated,
                "floor": info.get("floor"),
                "player_won": info.get("player_won"),
            }
        )

    print(f"total env steps: {total_steps} in {wall:.1f}s => {total_steps / wall:.1f} steps/s")
    for stat in episode_stats:
        print(f"  {stat}")
    if total_steps and wall:
        rate = total_steps / wall
        # A 100-episode checkpoint eval worst case is 100 * 1200 steps,
        # plus deterministic-policy GPU sync overhead (unknown here).
        print(
            f"projection (random policy, no GPU sync): 120k-step worst case "
            f"= {120_000 / rate / 60:.0f} min; a real 100-episode eval adds "
            "model.predict time per step on top"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
