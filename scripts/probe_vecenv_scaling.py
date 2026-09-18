"""Measure V2 stack step throughput vs vector-env worker count (diagnostic only).

Why this exists: ``training/curriculum.py`` and ``training/v2_curriculum.py``
build training envs with ``DummyVecEnv``, so ``parallel_envs`` instances are
stepped *serially in one process*, and every instance shares that process's
native (C#) engine state — the documented suspect behind the in-training vs
fresh-process evaluation divergence (docs/STATUS.md, "Cross-process replay
determinism flag").

A first version of this probe stepped dead envs after termination and therefore
reported ~8000 steps/s and "parallelism does not help". That was an artefact:
episode resets (run generation) are a large share of real cost. This version
resets on terminal/truncated and measures the real V2 contract stack, so the
Dummy-vs-Subproc comparison reflects what a trainer pays.

Random legal actions, no model, no writes, diagnostics partition seeds only.

    python scripts/probe_vecenv_scaling.py [--steps-per-env 2000]
                                           [--counts 1 4 8 12]
                                           [--kinds dummy subproc]
"""
from __future__ import annotations

import argparse
import functools
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EMULATOR_SRC = ROOT.parent / "third_party" / "slay-the-spire-2-emulator-main" / "src"
sys.path.insert(0, str(EMULATOR_SRC))
sys.path.insert(0, str(ROOT))

BASE_SEED = 20_000_000  # checkpoint/diagnostics partition only
CONFIG = str(ROOT / "config" / "training_v2_smoke.toml")
STAGE = "floor3"


def _make_raw_env(seed: int):
    import sts2_gym

    env = sts2_gym.Sts2RunEnv()
    env.reset(seed=seed)
    return env


def _make_stack_env(rank: int, workers: int):
    """Module-level so SubprocVecEnv children can reconstruct it."""
    import sts2_gym

    from training.v2_config import load_v2_training_config
    from training.v2_curriculum import _training_environment_factory

    config = load_v2_training_config(Path(CONFIG))
    stage = next(s for s in config.stages if s.name == STAGE)
    env = _training_environment_factory(config, stage, sts2_gym, rank, workers)()
    env.reset()
    return env


def _run(kind: str, stack: bool, n_envs: int, steps_per_env: int):
    from stable_baselines3.common.vec_env import DummyVecEnv, SubprocVecEnv

    if stack:
        factories = [functools.partial(_make_stack_env, rank, n_envs) for rank in range(n_envs)]
    else:
        factories = [functools.partial(_make_raw_env, BASE_SEED + n_envs * 1000 + rank)
                     for rank in range(n_envs)]
    builder = DummyVecEnv if kind == "dummy" else SubprocVecEnv
    vector_env = builder(factories)
    try:
        import random

        rng = random.Random(BASE_SEED + n_envs)
        total = steps_per_env
        started = time.perf_counter()
        steps = resets = 0
        while steps < total:
            masks = vector_env.env_method("action_masks") if stack else None
            actions = []
            for mask in (masks or [None] * n_envs):
                if mask is None:
                    actions.append(rng.randrange(int(vector_env.single_action_space.n)))
                else:
                    legal = mask.nonzero()[0]
                    actions.append(int(legal[rng.randrange(len(legal))]) if len(legal) else 0)
            result = vector_env.step(actions)
            if len(result) == 5:
                _obs, _rew, terms, truncs, _info = result
                done = [bool(t) or bool(u) for t, u in zip(terms, truncs)]
            else:
                _obs, _rew, done, _info = result
                done = [bool(d) for d in done]
            steps += n_envs
            resets += sum(done)
        elapsed = time.perf_counter() - started
        return elapsed, steps, resets
    finally:
        vector_env.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--steps-per-env", type=int, default=2000)
    parser.add_argument("--counts", type=int, nargs="+", default=[1, 4, 8, 12])
    parser.add_argument("--kinds", nargs="+", default=["dummy", "subproc"])
    parser.add_argument("--raw", action="store_true", help="measure the native env only")
    args = parser.parse_args()


    stack = not args.raw
    print(f"{'kind':>7} {'envs':>5} {'steps':>8} {'seconds':>9} {'steps/s':>9} {'resets/s':>9} {'vs 1 env':>9}")
    for kind in args.kinds:
        baseline = 0.0
        for count in args.counts:
            try:
                seconds, steps, resets = _run(kind, stack, count, args.steps_per_env)
            except Exception as exc:
                print(f"{kind:>7} {count:>5} {'FAILED':>8} {type(exc).__name__}: {str(exc)[:90]}")
                continue
            rate = steps / seconds
            baseline = baseline or rate
            print(f"{kind:>7} {count:>5} {steps:>8} {seconds:>9.1f} {rate:>9.1f} "
                  f"{resets / seconds:>9.2f} {rate / baseline:>9.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
