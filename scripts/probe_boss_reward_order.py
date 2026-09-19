"""Fork a boss-floor reward-screen decision and see what the engine does next.

Two generated act-2 runs on one checkpoint cleared the same boss node
(node_type=6, encounter_id=84) and ended differently: one reached
``phase=complete``, the other fell back to ``phase=map`` at floor 17 with an
empty action mask.  The two ways out of a boss node disagree:

  * ``RunEngine.cs:1905-1925`` AdvanceAfterRelicReward -> ``CurrentNodeType ==
    NodeBoss`` short-circuits to ``Complete`` for either act.
  * every other exit goes through ``AdvanceAfterNode``
    (``RunEngine.cs:1983-1997``), which tests only ``Floor >= terminalFloor`` --
    17 for overgrowth but ``MapBossRow*2+1`` = 33 for underdocks -- so a
    generated act-2 boss (whose map ends at row 17) re-enters Map and dead-ends.

Whether a boss win takes the first exit is decided on the reward screen:
``RunEngine.cs:1746-1781`` returns to ``RelicReward`` only while rewards are
still pending after the card is taken, otherwise it calls ``AdvanceAfterNode``.
That makes run completion a function of the *order* rewards are claimed, so this
script measures it instead of reasoning about it: replay the real action prefix,
substitute each legal action at one named boss-floor state, and continue with
the same argmax policy.  Run it with ``--fork-phase`` set to both
``card_reward`` and ``relic_reward`` -- the first showed the card screen is not
the deciding state, the second showed the relic screen is.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "third_party" / "slay-the-spire-2-emulator-main" / "src"))
sys.path.insert(0, str(ROOT))

from training.v2_constants import COMBAT_OBS_SIZE  # noqa: E402


def roll(env, model, seed, prefix, max_steps, fork_phase, fork_floor):
    """Step env with `prefix` then argmax, and stop at the first fork state.

    Returns (report, actions, fork).  `actions` is every action actually taken;
    `fork` is (index, chosen_action, legal_actions) at the first state whose
    phase/floor match the requested fork point, or None.
    """
    observation, info = env.reset(seed=seed)
    actions: list[int] = []
    trace: list[tuple[int, int, str]] = []
    last = None
    terminal = truncated = False
    dead_end = None
    illegal = 0
    fork = None
    while not (terminal or truncated):
        marker = (info.get("act"), info.get("floor"), info.get("phase_name"))
        if marker != last:
            trace.append(marker)
            last = marker
        mask = [index for index, on in enumerate(env.action_masks()) if bool(on)]
        if not mask:
            truncated = True
            dead_end = "empty_action_mask"
            break
        if len(actions) < len(prefix):
            action = prefix[len(actions)]
            if action not in mask:
                return ({"diverged": True, "step": len(actions), "mask": mask},
                        actions, None)
        else:
            raw, _ = model.predict(observation, action_masks=env.action_masks(),
                                   deterministic=True)
            action = int(raw.item() if hasattr(raw, "item") else raw)
            if action not in mask:
                truncated = True
                dead_end = "policy_illegal_action"
                illegal += 1
                break
        if fork is None and info.get("phase_name") == fork_phase \
                and info.get("floor") == fork_floor:
            fork = (len(actions), action, mask, dict(env.unwrapped.state_info()))
        observation, _reward, terminal, truncated, info = env.step(action)
        actions.append(action)
        if len(actions) >= max_steps:
            truncated = True
            dead_end = "step_cap"
    report = {
        "steps": len(actions),
        "terminal": terminal,
        "truncated": truncated,
        "run_won": bool(terminal and info.get("player_won", False)),
        "dead_end": dead_end,
        "illegal": illegal,
        "final_phase": info.get("phase_name"),
        "final_floor": info.get("floor"),
        "final_state_info": dict(env.unwrapped.state_info()),
        "trace_tail": [{"act": a, "floor": f, "phase": p} for a, f, p in trace[-6:]],
    }
    return report, actions, fork


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--config", type=Path,
                        default=ROOT / "config" / "training_v2.toml")
    parser.add_argument("--stage", default="act1")
    parser.add_argument("--seeds", default="130012038")
    parser.add_argument("--max-steps", type=int, default=20000)
    parser.add_argument("--fork-phase", default="card_reward")
    parser.add_argument("--fork-floor", type=int, default=17)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    import sts2_gym
    from sb3_contrib import MaskablePPO
    from stable_baselines3.common.vec_env import DummyVecEnv

    from training.v2_config import load_v2_training_config
    from training.v2_curriculum import _environment_factory

    config = load_v2_training_config(args.config.resolve())
    stage = next(s for s in config.stages if s.name == args.stage)
    open_stage = dataclasses.replace(stage, max_floor=None, max_episode_steps=args.max_steps)
    factory = _environment_factory(config, open_stage, sts2_gym)
    seeds = [int(t) if t.lstrip("-").isdigit() else t for t in args.seeds.split(",")]

    checkpoint = args.checkpoint.resolve()
    probe = DummyVecEnv([lambda: factory(seeds[0])])
    model = MaskablePPO.load(str(checkpoint), env=probe, device="cpu")

    payload = {
        "_comment": [
            "Counterfactual at the floor-17 card_reward decision: the real action",
            "prefix is replayed, then every legal action at that one state is",
            "substituted and the rest of the run is driven by the same argmax policy.",
            "Only the resulting engine phase is of interest; the fight is already over.",
        ],
        "checkpoint": str(checkpoint),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "phase_offsets": {"combat_obs_size": COMBAT_OBS_SIZE},
        "seeds": [],
    }

    for seed in seeds:
        env = factory(seed)
        base, actions, fork = roll(env, model, seed, [], args.max_steps,
                                   args.fork_phase, args.fork_floor)
        entry = {"seed": seed, "as_played": base, "fork_found": fork is not None}
        if fork is None:
            payload["seeds"].append(entry)
            print(f"{seed}: no {args.fork_phase} state at floor {args.fork_floor}")
            continue
        index, chosen, legal, fork_state = fork
        entry["fork_step"] = index
        entry["fork_state_info"] = fork_state
        entry["legal_actions"] = legal
        entry["policy_chosen"] = chosen
        variants = []
        for action in legal:
            env = factory(seed)
            report, _used, _f = roll(env, model, seed, actions[:index] + [action],
                                     args.max_steps, args.fork_phase, args.fork_floor)
            if report.get("diverged"):
                # Replay is deterministic, so this should not happen -- but a sweep
                # that dies halfway reports nothing at all, which is worse than a
                # partial artifact with the broken row marked.
                variants.append({"action": action, "is_policy_choice": action == chosen,
                                 "diverged": True})
                print(f"{seed} fork action {action}: DIVERGED")
                continue
            variants.append({"action": action, "is_policy_choice": action == chosen,
                             "outcome": {k: report[k] for k in
                                         ("final_phase", "run_won", "truncated",
                                          "terminal", "dead_end", "steps",
                                          "final_floor")} |
                                        {"current_node_type":
                                         report["final_state_info"].get(
                                             "current_node_type")},
                             "trace_tail": report["trace_tail"],
                             "diverged": report.get("diverged", False)})
            print(f"{seed} fork action {action}: {report['final_phase']} "
                  f"won={report['run_won']} floor={report['final_floor']}")
        entry["variants"] = variants
        payload["seeds"].append(entry)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
