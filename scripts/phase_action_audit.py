"""What is the PPO actually being asked to decide? Steps per phase, campaign rollouts.

The delivery contract says combat on the real client belongs to a third-party solver and our policy
only decides between fights. That is a claim about the training objective, so it gets measured on the
action stream rather than argued: every step of a campaign rollout is tagged with the phase the
engine offered it in, and the shares are reported. A policy whose gradient is mostly card selection
is not training the thing the product ships.

    ../third_party/slay-the-spire-2-emulator-main/.venv/Scripts/python.exe \
        scripts/phase_action_audit.py --episodes 60 --seed-start 2008010000 \
        --out docs/evidence/phase_action_share_20261007.json
"""
from __future__ import annotations

import argparse
import collections
import dataclasses
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
EMULATOR = ROOT.parent / "third_party" / "slay-the-spire-2-emulator-main"
sys.path.insert(0, str(EMULATOR / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=pathlib.Path,
                        default=ROOT / "config/production_campaign_v5.toml")
    parser.add_argument("--stage", default="full_run")
    parser.add_argument("--checkpoint", type=pathlib.Path,
                        default=ROOT / "runtime/production_campaign_v5/"
                        "v2curriculum-20260923T061352Z/full_run/checkpoints/"
                        "step_000040000032.zip")
    parser.add_argument("--episodes", type=int, default=20)
    parser.add_argument("--seed-start", type=int, default=2008010000)
    parser.add_argument("--out", type=pathlib.Path,
                        default=ROOT / "runtime/phase_action_audit.json")
    args = parser.parse_args()

    import sts2_gym  # noqa: F401
    from sb3_contrib import MaskablePPO
    from stable_baselines3.common.vec_env import DummyVecEnv

    from training.v2_config import load_v2_training_config
    from training.v2_curriculum import _environment_factory

    config = load_v2_training_config(args.config.resolve())
    stage = next(s for s in config.stages if s.name == args.stage)
    factory = _environment_factory(config, stage, sts2_gym)

    seeds = [args.seed_start + i for i in range(args.episodes)]
    probe = DummyVecEnv([lambda: factory(seeds[0])])
    try:
        model = MaskablePPO.load(str(args.checkpoint), env=probe, device="cpu")
    finally:
        probe.close()

    steps_by_phase: collections.Counter = collections.Counter()
    episodes_by_phase: collections.Counter = collections.Counter()
    per_episode = []
    completed = 0
    for seed in seeds:
        env = factory(seed)
        try:
            observation, info = env.reset(seed=seed, options={"campaign": True})
            # The campaign horizon lives in the stage config, but the contract wrapper also carries
            # its own inner limit (v2_curriculum passes max_episode_steps into the native env).
            # Trust whichever is smaller, and stop on the env's own truncated signal too.
            inner = getattr(getattr(env, "unwrapped", None), "max_episode_steps", None)
            budget = min(int(inner) if inner else stage.max_episode_steps,
                         stage.max_episode_steps)
            done = False
            phases: collections.Counter = collections.Counter()
            steps = 0
            while not done and steps < budget:
                mask = env.action_masks()
                if not any(bool(value) for value in mask):
                    break
                raw, _ = model.predict(observation, action_masks=mask, deterministic=True)
                phase = str((info or {}).get("phase_name") or "unknown")
                steps_by_phase[phase] += 1
                phases[phase] += 1
                steps += 1
                observation, _r, terminated, truncated, info = env.step(
                    int(raw.item() if hasattr(raw, "item") else raw))
                done = bool(terminated or truncated)
            for phase in phases:
                episodes_by_phase[phase] += 1
            completed += 1
            per_episode.append({"seed": seed, "steps": sum(phases.values()),
                                "phases": dict(phases),
                                "won": bool((info or {}).get("player_won")),
                                "run_cleared": bool((info or {}).get("run_cleared"))})
        finally:
            env.close()

    total = sum(steps_by_phase.values()) or 1
    payload = {
        "episodes": completed,
        "stage": args.stage,
        "checkpoint": args.checkpoint.relative_to(ROOT).as_posix(),
        "total_steps": total,
        "steps_by_phase": dict(sorted(steps_by_phase.items(), key=lambda kv: -kv[1])),
        "step_share_by_phase": {k: round(v / total, 4)
                                for k, v in sorted(steps_by_phase.items(), key=lambda kv: -kv[1])},
        "episodes_that_touched_phase": dict(sorted(episodes_by_phase.items())),
        "per_episode": per_episode,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    print(json.dumps({k: payload[k] for k in
                      ("episodes", "total_steps", "step_share_by_phase")},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
