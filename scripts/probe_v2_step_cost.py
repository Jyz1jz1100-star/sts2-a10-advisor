"""Attribute V2 training steps/second to its layers (diagnostic only).

Steady-state V2 training measures ~370 fps at 12 parallel envs, and raising
``parallel_envs`` barely moves that (331/364/388 fps at 4/12/24), so the cost is
per-step work rather than a lack of environment parallelism. This probe runs a
fixed wall-clock budget at equal seed and reports steps/s for:

  raw     - the native ``Sts2RunEnv`` only
  stack   - the full V2 contract stack (flat (action,target) space + expanded
            1739-int observation + reward/floor wrapper)
  policy  - the stack plus a MaskablePPO ``predict`` per step, CPU and GPU

Random legal-ish actions; every episode reset is counted inside the rate, which
is what a trainer actually pays for. No writes, no checkpoints, checkpoint
seeds only.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "third_party" / "slay-the-spire-2-emulator-main" / "src"))
sys.path.insert(0, str(ROOT))

SEED = 20_000_000  # checkpoint/diagnostics partition only


def _measure(build, seconds: float, device: str = "cpu") -> dict[str, float]:
    env = build(SEED)
    rng = __import__("random").Random(SEED)
    steps = resets = 0
    started = time.perf_counter()
    obs, info = env.reset(seed=SEED)
    resets += 1
    model = None
    if device != "none":
        from sb3_contrib import MaskablePPO

        try:
            model = MaskablePPO("MlpPolicy", env, device=device, n_steps=64,
                                batch_size=64, verbose=0, seed=SEED)
        except Exception as exc:  # an unusable policy shape is a result, not a crash
            return {"error": f"{type(exc).__name__}: {exc}"}
    try:
        while time.perf_counter() - started < seconds:
            if model is not None:
                mask_fn = getattr(env, "action_masks", None)
                action, _ = model.predict(
                    obs,
                    action_masks=mask_fn() if mask_fn is not None else None,
                    deterministic=False,
                )
            else:
                masks = getattr(env, "action_masks", None)
                if masks is None:
                    action = rng.randrange(env.action_space.n)
                else:
                    legal = masks().nonzero()[0]
                    if len(legal) == 0:
                        obs, _info = env.reset(seed=SEED + resets)
                        resets += 1
                        continue
                    action = int(legal[rng.randrange(len(legal))])
            obs, _r, term, trunc, _info = env.step(int(action))
            steps += 1
            if term or trunc:
                obs, _info = env.reset(seed=SEED + resets)
                resets += 1
    finally:
        env.close()
    elapsed = time.perf_counter() - started
    return {"seconds": elapsed, "steps": steps, "resets": resets,
            "steps_per_second": steps / elapsed, "ms_per_step": 1000.0 * elapsed / max(steps, 1)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=20.0)
    args = parser.parse_args()

    import sts2_gym

    from training.v2_config import load_v2_training_config

    config = load_v2_training_config(ROOT / "config" / "training_v2_smoke.toml")
    stage = next(s for s in config.stages if s.name == "floor3")

    def build_raw(seed: int):
        env = sts2_gym.Sts2RunEnv()
        return env

    def build_stack(seed: int):
        from training.v2_curriculum import _training_environment_factory

        return _training_environment_factory(config, stage, sts2_gym, 0, 1)()

    rows = [("raw", _measure(build_raw, args.seconds)),
            ("stack", _measure(build_stack, args.seconds)),
            ("stack+ppo cpu", _measure(build_stack, args.seconds, device="cpu")),
            ("stack+ppo cuda", _measure(build_stack, args.seconds, device="cuda"))]
    print(f"{'layer':>14} {'steps':>8} {'resets':>7} {'steps/s':>9} {'ms/step':>9}")
    for name, m in rows:
        if "error" in m:
            print(f"{name:>14} {'-':>8} {'-':>7} {'n/a':>9}  {m['error']}")
            continue
        print(f"{name:>14} {m['steps']:>8.0f} {m['resets']:>7.0f} "
              f"{m['steps_per_second']:>9.1f} {m['ms_per_step']:>9.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
