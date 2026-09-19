"""Attribute mask/engine refusals to a phase, and record what the re-ask executes.

``census_contract_channels.py`` established that the native layer refuses mask-advertised
actions ~0.53 times per episode on the act1 stage while ``illegal_actions`` stays 0. Two
questions were left open there and are answered here by observation rather than argument:

1. Where do the refusals land? A refusal inside the boss fight and a refusal on a reward
   screen change different numbers, so a single per-episode rate cannot be interpreted.
2. What does the policy actually do after being re-asked? Filter mode returns the *same*
   observation with the refused action excluded (v2_flat_env.py:294-300), so every reported
   "the policy chose X" is really "the first choice was refused n times, then X executed".

The second question matters for tonight's boss-relic-screen fork results specifically: those
claims are about action choice on a reward screen, which is a phase where a refusal could
plausibly be hiding in the executed-action counts.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "third_party" / "slay-the-spire-2-emulator-main" / "src"))
sys.path.insert(0, str(ROOT))

from training.v2_constants import COMBAT_OBS_SIZE  # noqa: E402
from training.v2_flat_env import TARGET_SLOTS  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "config/training_v2.toml")
    parser.add_argument("--stage", default="act1")
    parser.add_argument("--split", default="promotion")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--start-offset", type=int, default=0)
    parser.add_argument("--max-steps", type=int, default=1600)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    import sts2_gym
    from sb3_contrib import MaskablePPO
    from stable_baselines3.common.vec_env import DummyVecEnv

    from training.v2_config import load_v2_training_config
    from training.v2_curriculum import _environment_factory

    config = load_v2_training_config(args.config.resolve())
    stage = next(s for s in config.stages if s.name == args.stage)
    window = config.partition(stage.name, args.split).seeds()[
        args.start_offset:args.start_offset + args.limit]
    open_stage = dataclasses.replace(stage, max_floor=None,
                                     max_episode_steps=args.max_steps)
    factory = _environment_factory(config, open_stage, sts2_gym)

    checkpoint = args.checkpoint.resolve()
    probe = DummyVecEnv([lambda: factory(window[0])])
    model = MaskablePPO.load(str(checkpoint), env=probe, device="cpu")

    by_phase: collections.Counter = collections.Counter()
    decisions_by_phase: collections.Counter = collections.Counter()
    states_with_refusals = 0
    executed_after_refusal_differs = 0
    relic_screen_rows: list[dict] = []
    episodes = 0
    total_refusals = 0

    for seed in window:
        env = factory(int(seed))
        observation, info = env.reset(seed=int(seed))
        terminal = truncated = False
        steps = 0
        pending: dict[bytes, dict] = {}
        while not (terminal or truncated):
            mask = env.action_masks()
            if not any(bool(v) for v in mask):
                break
            # Key everything to the state the decision is made *in*: after a step the
            # engine has already moved on, and a refusal must be matched to the state
            # where it happened or the re-ask pairing is meaningless.
            pre_state = bytes(env.unwrapped.raw_observation()[COMBAT_OBS_SIZE:])
            pre_phase = env.unwrapped.state_info().get("phase_name")
            pre_floor = env.unwrapped.state_info().get("floor")
            pre_node = int(env.unwrapped.raw_observation()[COMBAT_OBS_SIZE + 8])
            raw, _ = model.predict(observation, action_masks=mask, deterministic=True)
            action = int(raw.item() if hasattr(raw, "item") else raw)
            _obs, _reward, terminal, truncated, info = env.step(action)
            steps += 1
            decisions_by_phase[pre_phase] += 1
            if info.get("native_rejection_filtered"):
                by_phase[pre_phase] += 1
                total_refusals += 1
                entry = pending.setdefault(pre_state, {"refused": [], "phase": pre_phase,
                                                      "floor": pre_floor, "node_type": pre_node})
                entry["refused"].append(action)
                if pre_phase in ("relic_reward", "card_reward", "potion_reward"):
                    relic_screen_rows.append({
                        "seed": int(seed), "step": steps, "phase": pre_phase,
                        "refused_flat": action, "refused_base": action // TARGET_SLOTS,
                        "floor": pre_floor,
                    })
            else:
                entry_done = pending.pop(pre_state, None)
                if entry_done:
                    states_with_refusals += 1
                    if action not in entry_done["refused"]:
                        executed_after_refusal_differs += 1
                    if entry_done["phase"] in ("relic_reward", "card_reward", "potion_reward"):
                        relic_screen_rows.append({
                            "seed": int(seed), "step": steps, "phase": entry_done["phase"],
                            "executed_flat": action, "executed_base": action // TARGET_SLOTS,
                            "refused_before_execution": [
                                {"flat": f, "base": f // TARGET_SLOTS}
                                for f in entry_done["refused"]],
                        })
            observation = _obs
            if steps >= args.max_steps:
                truncated = True
        env.close()
        episodes += 1

    payload = {
        "_comment": [
            "Refusals are counted with info['native_rejection_filtered'], the filter-mode",
            "signal: same observation back, refused action removed from that state's mask.",
            "A 'state with refusals' is one where the policy was re-asked and eventually",
            "had an action executed; 'executed_after_refusal_differs' counts the cases where",
            "the executed action is not one of the refused ones (i.e. the re-ask genuinely",
            "changed the realised action).",
        ],
        "checkpoint": str(checkpoint),
        "checkpoint_sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
        "decisions_by_phase": dict(decisions_by_phase),
        "episodes": episodes,
        "executed_after_refusal_differs": executed_after_refusal_differs,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "refusals_by_phase": dict(by_phase),
        "reward_screen_events": relic_screen_rows,
        "scope": "simulator_act1 label, mixed-act population, argmax",
        "seeds_enumerated": len(window),
        "states_with_refusals": states_with_refusals,
        "total_refusals": total_refusals,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(f"{episodes} episodes, {total_refusals} refusals in {states_with_refusals} states, "
          f"{executed_after_refusal_differs} where the re-ask changed the action")
    print("by phase:", dict(by_phase))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
