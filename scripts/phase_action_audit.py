"""What is the PPO actually being asked to decide? Steps per phase, campaign rollouts.

The delivery contract says combat on the real client belongs to a third-party solver and our policy
only decides between fights. That is a claim about the training objective, so it gets measured on the
action stream rather than argued: every step of a campaign rollout is tagged with the phase the
engine offered it in, and the shares are reported. A policy whose gradient is mostly card selection
is not training the thing the product ships.

The phase is read from the info of the state the action was taken in, not of the state the action
landed in. That is what makes the number mean what it says: the loss is built from
(decision state, action, next) triples, so an arm behind ``FrozenCombatExecutor`` reports
``combat: 0`` in ``steps_by_phase`` while still carrying every fight it absorbed in
``absorbed_combat_steps``. Zero in the first with zero in the second would mean the fights went
missing rather than moved, which is why both are recorded and the claim reads both.

Run it through a stage config, never through a hand-assembled stack: the factory is what training
and evaluation both use, so an audit that wraps the layers itself measures a simulator nobody
trains on.

    ../third_party/slay-the-spire-2-emulator-main/.venv/Scripts/python.exe \
        scripts/phase_action_audit.py --episodes 60 --seed-start 2008010000 \
        --out docs/evidence/phase_action_share_20261007.json

    ../third_party/slay-the-spire-2-emulator-main/.venv/Scripts/python.exe \
        scripts/phase_action_audit.py --config config/production_campaign_v5_g1.toml \
        --episodes 60 --seed-start 2008010000 \
        --out docs/evidence/phase_action_share_frozen_20261007.json
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
EMULATOR = ROOT.parent / "third_party" / "slay-the-spire-2-emulator-main"
sys.path.insert(0, str(EMULATOR / "src"))

#: Carried into the artifact so a share can never be quoted for the wrong world.
NOT_ESTABLISHED = [
    "that a policy trained only on the out-of-combat steps reaches any win rate: this is an "
    "accounting of where the gradient currently goes, not a capability claim",
    "that the share is stage-independent -- the campaign horizon (4,800) and the single-act "
    "stage (1,600) spend steps differently, so a share quoted for one is not a share for the "
    "other",
]

def _relative(path: pathlib.Path) -> str:
    """Repo-relative posix path, or the path as given when it lives outside the repo."""
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()



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

    from training.campaign_content import CAMPAIGN_ENVIRONMENT_VERSION
    from training.v2_config import load_v2_training_config
    from training.v2_curriculum import _environment_factory

    config_path = args.config.resolve()
    checkpoint_path = args.checkpoint.resolve()
    config = load_v2_training_config(config_path)
    stage = next(s for s in config.stages if s.name == args.stage)
    factory = _environment_factory(config, stage, sts2_gym)
    frozen_combat = stage.combat_executor == "frozen"

    seeds = [args.seed_start + i for i in range(args.episodes)]
    probe = DummyVecEnv([lambda: factory(seeds[0])])
    try:
        model = MaskablePPO.load(str(checkpoint_path), env=probe, device="cpu")
    finally:
        probe.close()

    steps_by_phase: collections.Counter = collections.Counter()
    episodes_by_phase: collections.Counter = collections.Counter()
    per_episode = []
    absorbed_steps_total = 0
    absorbed_episodes = 0
    capped = 0
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
            absorbed = 0
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
                absorbed += int((info or {}).get("combat_steps") or 0)
                if (info or {}).get("combat_executor_step_cap"):
                    capped += 1
            for phase in phases:
                episodes_by_phase[phase] += 1
            completed += 1
            absorbed_steps_total += absorbed
            absorbed_episodes += 1 if absorbed else 0
            per_episode.append({"seed": seed, "steps": sum(phases.values()),
                                "phases": dict(phases),
                                "absorbed_combat_steps": absorbed,
                                "won": bool((info or {}).get("player_won")),
                                "run_cleared": bool((info or {}).get("run_cleared"))})
        finally:
            env.close()

    total = sum(steps_by_phase.values()) or 1
    ordered = dict(sorted(steps_by_phase.items(), key=lambda kv: -kv[1]))
    combat = int(steps_by_phase.get("combat") or 0)
    payload = {
        "episodes": completed,
        "stage": args.stage,
        "config": _relative(config_path),
        "checkpoint": _relative(checkpoint_path),
        "checkpoint_sha256": hashlib.sha256(checkpoint_path.read_bytes()).hexdigest(),
        "combat_executor": "frozen" if frozen_combat else "agent",
        "engine_environment_version": CAMPAIGN_ENVIRONMENT_VERSION,
        "generated_by": "scripts/phase_action_audit.py",
        "how_to_recheck": " ".join((
            "../third_party/slay-the-spire-2-emulator-main/.venv/Scripts/python.exe",
            "scripts/phase_action_audit.py",
            f"--config {_relative(config_path)}",
            f"--episodes {args.episodes}",
            f"--seed-start {args.seed_start}",
            f"--out {args.out.as_posix()}")),
        "total_steps": total,
        "steps_by_phase": ordered,
        "step_share_by_phase": {k: round(v / total, 4) for k, v in ordered.items()},
        "combat_step_share": round(combat / total, 4),
        "out_of_combat_step_share": round((total - combat) / total, 4),
        "absorbed_combat_steps": absorbed_steps_total,
        "episodes_that_absorbed_combat": absorbed_episodes,
        "executor_step_capped_transitions": capped,
        "episodes_that_touched_phase": dict(sorted(episodes_by_phase.items())),
        "per_episode": per_episode,
        "scope_note": (
            "every step of a campaign rollout, tagged with the phase the engine offered it in, "
            "under the frozen production v5 checkpoint; the question is whose decisions the PPO "
            "loss is built from, because on the real client combat belongs to a third-party "
            "solver"
            + ("; this arm plays its fights with a frozen executor, so a combat decision appears "
               "only in absorbed_combat_steps and never in steps_by_phase"
               if frozen_combat else "")),
        "not_established": NOT_ESTABLISHED,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    print(json.dumps({k: payload[k] for k in
                      ("episodes", "total_steps", "combat_step_share",
                       "absorbed_combat_steps", "executor_step_capped_transitions")},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
