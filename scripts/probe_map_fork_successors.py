"""Which map options really continue a run, at the decision where a chained Act 2 stopped.

Three checkpoints chain into Act 2 on the retained-trace seed. Two of them end **alive** -- 37/77 HP,
phase `map`, floor 19, far short of any step cap -- and one walks on to floor 22 and dies in combat.
"Lost map successors" is the label those two carry, but the label alone cannot tell the two possible
causes apart: either the policy picked an option the engine would not walk (a live choice the policy
made wrong), or the mask advertised an option that `StepMap` refuses (`RunEngine.cs:966` returns -1
through `ChooseMapNode`, `RunMapGenerator.cs:1017-1029`), which is the same mask-versus-step
inconsistency already documented for the shop potion slot and the event `default:` arm -- and which
would mean the ceiling on the only expressible two-act flow is partly the engine's.

So this rolls the checkpoint deterministically, replays the prefix to the last `map` decision, and
then takes **every** mask-legal option from that state, reporting per option whether the episode
ended immediately and how far the run goes within a lookahead. The roll semantics are the probe's
(`probe_chained_act_flow.py`), so a fork cannot disagree with the sweep about what a step means.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "third_party" / "slay-the-spire-2-emulator-main" / "src"))

TARGET_SLOTS = 7  # training/v2_run_wrapper.py: flat action = base * TARGET_SLOTS + target
SENTINEL_FLAT = 224  # the target-less action, whose base index is past the native 32-entry mask
FORK_PHASE = "map"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "config/training_v2.toml")
    parser.add_argument("--stage", default="act1")
    parser.add_argument("--seed", default="7MS1YN8NWB")
    parser.add_argument("--max-steps", type=int, default=4_000)
    parser.add_argument("--lookahead", type=int, default=600,
                        help="steps to keep rolling after the forked choice before calling it 'continued'")
    parser.add_argument("--judge_with_evaluator", action="store_true",
                        help="also roll the same seed through training/evaluation.py and record how "
                             "the campaign's own classifier labels how it ended")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    import sts2_gym
    from sb3_contrib import MaskablePPO
    from stable_baselines3.common.vec_env import DummyVecEnv

    from training.v2_config import load_v2_training_config
    from training.v2_curriculum import _environment_factory

    config = load_v2_training_config(args.config.resolve())
    stage = next((s for s in config.stages if s.name == args.stage), None)
    if stage is None:
        raise SystemExit(f"stage {args.stage!r} not in {args.config}")
    open_stage = dataclasses.replace(stage, max_floor=None, max_episode_steps=args.max_steps)
    factory = _environment_factory(config, open_stage, sts2_gym)
    seed = int(args.seed) if args.seed.lstrip("-").isdigit() else args.seed
    probe = DummyVecEnv([lambda: factory(seed)])
    model = MaskablePPO.load(str(args.checkpoint.resolve()), env=probe, device="cpu")

    def roll(policy, fork_at: int | None):
        """Deterministic roll; with fork_at set, replay that many recorded actions then stop."""
        env = factory(seed)
        observation, info = env.reset(seed=seed)
        actions: list[int] = []
        markers: list[tuple] = []
        last = None
        terminal = truncated = False
        while not (terminal or truncated):
            marker = (info.get("act"), info.get("floor"), info.get("phase_name"))
            if marker != last:
                markers.append(marker)
                last = marker
            if fork_at is not None and len(actions) == fork_at:
                return env, observation, info, actions, markers
            mask = env.action_masks()
            if not any(bool(value) for value in mask):
                return env, observation, info, actions, markers
            raw, _ = model.predict(observation, action_masks=mask, deterministic=True)
            action = int(raw.item() if hasattr(raw, "item") else raw)
            actions.append(action)
            observation, _reward, terminal, truncated, info = env.step(action)
        return env, observation, info, actions, markers

    _env, _obs, _info, actions, markers = roll(model, None)
    # The fork point is the step that entered the last map decision: everything before it is a
    # deterministic prefix, so replaying it reproduces the state the run actually faced.
    map_steps = [index for index, marker in enumerate(markers) if marker[2] == FORK_PHASE]
    if not map_steps:
        raise SystemExit("no map decision in this roll -- nothing to fork on")
    target = markers[map_steps[-1]]
    fork_at = len(actions) - 1 if markers[-1][2] == FORK_PHASE else None
    if fork_at is None:
        raise SystemExit("the roll did not end at a map decision; pass a checkpoint whose "
                         f"chained run stops alive (last marker was {markers[-1]})")

    env, observation, info, _prefix, _ = roll(model, fork_at)
    native_mask = env.unwrapped.base_action_mask()
    flat_mask = env.action_masks()
    options = {}
    for flat in range(len(flat_mask)):
        if flat_mask[flat]:
            options.setdefault(flat // TARGET_SLOTS, flat)
    branches = []
    for base in sorted(options):
        flat = options[base]
        branch_env = factory(seed)
        branch_env.reset(seed=seed)
        for action in actions[:fork_at]:
            branch_env.step(action)
        # Re-derive the mask at the fork in the replayed environment: if the replay is faithful the
        # option is legal there too, and a disagreement would be the finding, not a detail.
        replay_flat_mask = branch_env.action_masks()
        replay_native_mask = branch_env.unwrapped.base_action_mask()
        advertised = bool(replay_flat_mask[flat])
        _o, _r, terminal, truncated, binfo = branch_env.step(flat)
        deepest = (binfo.get("act"), binfo.get("floor"))
        steps_after = 0
        # The wrapper drops the episode as soon as the engine refuses the map option, so
        # asking for a mask on a finished episode is not possible -- that itself is the signal.
        stalled = not (terminal or truncated) and not any(
            bool(value) for value in branch_env.action_masks())
        while not (terminal or truncated) and steps_after < args.lookahead:
            branch_mask = branch_env.action_masks()
            if not any(bool(value) for value in branch_mask):
                break
            raw, _ = model.predict(_o, action_masks=branch_mask, deterministic=True)
            _o, _r, terminal, truncated, binfo = branch_env.step(
                int(raw.item() if hasattr(raw, "item") else raw))
            steps_after += 1
            deepest = max(deepest, (binfo.get("act"), binfo.get("floor")))
            # The wrapper refuses action_masks() once the episode is over, so emptiness is
            # sampled at the top of the next pass instead of after the loop.
            stalled = (not terminal and not truncated
                       and not any(bool(value) for value in branch_env.action_masks()))
        branches.append({
            "base_action": base,
            "flat_action": flat,
            "advertised_by_mask": advertised,
            "is_sentinel_action": flat == SENTINEL_FLAT,
            "legal_in_native_mask": base < len(replay_native_mask)
            and bool(replay_native_mask[base]),
            "ended_on_the_step_itself": bool(terminal or truncated),
            "outcome": ("win" if binfo.get("player_won") and terminal
                        else "death" if terminal else "stalled_no_legal_action" if stalled
                        else "stopped_at_lookahead" if steps_after >= args.lookahead
                        else "ended_elsewhere"),
            "hp_after": binfo.get("player_hp"),
            "lookahead_steps": steps_after,
            "deepest_act_floor": list(deepest),
        })
        print(f"base {base}: advertised={advertised} ended_immediately="
              f"{branches[-1]['ended_on_the_step_itself']} -> {branches[-1]['outcome']} "
              f"deepest={deepest} hp={binfo.get('player_hp')}")

    judgement = None
    if args.judge_with_evaluator:
        # The campaign's dead-end vocabulary is owned by training/evaluation.py, so the question
        # "is this state classified, and how" is answered by calling it, not by reading the trace.
        from training.evaluation import evaluate_policy

        judged = evaluate_policy(
            model, env_factory=lambda chosen: factory(chosen), seeds=[seed], stage=args.stage,
            split="promotion", scope="simulator_act1", checkpoint=str(args.checkpoint.resolve()),
            experimental=False, max_steps_per_episode=args.max_steps).to_dict()
        judgement = {key: judged[key] for key in (
            "episodes", "wins", "truncations", "truncation_rate", "defect_truncation_rate",
            "illegal_actions", "rejection_events", "unclassified_dead_ends", "dead_end_reasons",
            "boundary_rate", "boundary_hits", "max_final_floor")}

    payload = {
        "evaluator_judgement": judgement,
        "checkpoint": str(args.checkpoint.resolve()),
        "checkpoint_sha256": __import__("hashlib").sha256(
            args.checkpoint.resolve().read_bytes()).hexdigest(),
        "fork_at_step": fork_at,
        "fork_masks_at_state": {
            "flat_bases": sorted(options),
            "native_bases": sorted(base for base in range(len(native_mask)) if native_mask[base]),
        },
        "fork_state": {"act": target[0], "floor": target[1], "phase": target[2]},
        "prefix_length": len(actions[:fork_at]),
        "roll_outcome_before_forking": {"steps": len(actions), "markers": len(markers),
                                        "last_marker": list(markers[-1])},
        "branches": branches,
        "lookahead": args.lookahead,
        "seed": args.seed,
        "not_established": [
            "that any branch wins the act: the lookahead bounds how far each was rolled",
            "anything about the shipped game"],
        "scope": "the last map decision of one deterministic chained roll on the retained-trace seed",
    }
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
        print(f"wrote {args.out}")
    continued = [b for b in branches if not b["ended_on_the_step_itself"]]
    print(f"{len(branches)} mask-legal options, {len(continued)} continue past the fork")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
