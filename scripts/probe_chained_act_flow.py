"""Roll a checkpoint through the emulator's only multi-act flow.

The simulator has two acts (``RunConstants.cs:35-36``) and exactly one act
chaining path: ``RunEngine.cs:1907-1920`` continues into Act 2 past the Act 1
boss only when ``StringSeed == "7MS1YN8NWB"``. That seed is a retained trace,
so its Act 1 map, encounters and boss are hardcoded
(``RunMapGenerator.cs:89-113`` and ``:792-947``).

So a victory here evidences "the harness can express and finish a two-act
flow", NOT "the policy generalises across acts" — a scripted act cannot
evidence generalisation. Both readings are printed in the output so the
artifact cannot be quoted out of that context.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sys
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "third_party" / "slay-the-spire-2-emulator-main" / "src"))
sys.path.insert(0, str(ROOT))

CHAINED_SEED = "7MS1YN8NWB"

from training.v2_constants import (  # noqa: E402
    COMBAT_OBS_SIZE,
    MAX_ENEMIES,
    MAX_HAND,
    PHASE_COMBAT,
)
from training.v2_flat_env import SENTINEL_FLAT, TARGET_SLOTS  # noqa: E402


def _tail_report(blocks) -> dict[str, object]:
    """Which combat integers still move when both sides' HP is frozen.

    A stalemate of 59,830 *distinct* states is only meaningful once you know what
    is still varying: if only a counter moves, the fight is not progressing and
    the mask never says so, which is an environment dead end rather than a policy
    choice.
    """
    rows = [list(b) for b in blocks]
    if not rows:
        return {}
    varying = [index for index in range(COMBAT_OBS_SIZE)
               if len({row[index] for row in rows}) > 1]
    return {
        "tail_blocks_kept": len(rows),
        "tail_varying_indices": varying,
        "tail_first_block": rows[0],
        "tail_last_block": rows[-1],
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config/training_v2.toml")
    parser.add_argument("--stage", default="act1")
    parser.add_argument("--checkpoint", type=Path, action="append", required=True)
    parser.add_argument("--seed", default=CHAINED_SEED,
                        help="comma list of seeds. A numeric seed is passed to the engine "
                             "as an int, so it matches how evaluation and the win ledger "
                             "reproduce a named seed; only the retained-trace string seed "
                             "can chain into Act 2.")
    parser.add_argument("--allow-non-chained", action="store_true",
                        help="permit seeds other than the chained demo seed; the output is "
                             "then scoped to single-act flows and cannot claim Act 2")
    parser.add_argument("--max-steps", type=int, default=60_000)
    parser.add_argument("--sampled", action="store_true",
                        help="sample instead of taking the argmax, to test whether the "
                             "argmax policy is what locks a boss fight into a stalemate")
    parser.add_argument("--sample-seed", type=int, default=0)
    parser.add_argument("--watch-floor", type=int, default=17,
                        help="floor whose action stream to record (default: the boss node)")
    parser.add_argument("--watch-steps", type=int, default=0,
                        help="record N chosen actions at --watch-floor so a stalemate can "
                             "be read as a behaviour, not inferred from a step count")
    parser.add_argument("--watch-raw", type=int, default=0,
                        help="keep the last N combat observation blocks and report which "
                             "integer positions still change while HP is frozen")
    parser.add_argument("--repeats", type=int, default=1,
                        help="roll the same seed N times; only meaningful with --sampled")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    seeds = [token.strip() for token in args.seed.split(",") if token.strip()]
    if not seeds:
        raise SystemExit("--seed had no usable entries")
    # Keep the engine's own type distinction: int seeds are what the campaign,
    # evaluation and the win ledger use, so re-running a named win here has to
    # pass the same type or it is a different run.
    seeds = [int(token) if token.lstrip("-").isdigit() else token for token in seeds]
    non_chained = [token for token in seeds if token != CHAINED_SEED]
    if non_chained and not args.allow_non_chained:
        raise SystemExit(
            f"{non_chained} cannot chain acts: only {CHAINED_SEED!r} reaches Act 2 "
            "(RunEngine.cs:1909). Pass --allow-non-chained to measure them anyway as "
            "single-act flows -- the artifact then cannot be read as a two-act result."
        )

    import sts2_gym
    from sb3_contrib import MaskablePPO
    from stable_baselines3.common.vec_env import DummyVecEnv

    from training.v2_config import load_v2_training_config
    from training.v2_curriculum import _environment_factory

    config = load_v2_training_config(args.config.resolve())
    stage = next((s for s in config.stages if s.name == args.stage), None)
    if stage is None:
        raise SystemExit(f"stage {args.stage!r} not in {args.config}")
    # A curriculum boundary would truncate at its own floor and hide the chain,
    # and the configured episode cap was sized for one act.
    open_stage = dataclasses.replace(stage, max_floor=None, max_episode_steps=args.max_steps)
    factory = _environment_factory(config, open_stage, sts2_gym)

    results = []
    # One flat loop over the cross product keeps the body's indentation (and so
    # the copied evaluation semantics) untouched.
    for checkpoint, seed in [(cp, sd) for cp in args.checkpoint for sd in seeds]:
        checkpoint = checkpoint.resolve()
        probe = DummyVecEnv([lambda seed=seed: factory(seed)])
        model = MaskablePPO.load(str(checkpoint), env=probe, device="cpu")
        for repeat in range(args.repeats):
            if args.sampled:
                import torch

                torch.manual_seed(args.sample_seed + repeat)
            deterministic = not args.sampled
            env = factory(seed)
            observation, info = env.reset(seed=seed)
            trace: list[dict[str, object]] = []
            window: list[dict[str, object]] = []
            tail_blocks: deque = deque(maxlen=args.watch_raw or None)
            state_shape = None
            last = None
            steps = 0
            terminal = truncated = False
            illegal_actions = 0
            dead_end = None
            # Termination, win and illegal-action semantics are copied from
            # training/evaluation.py so this probe cannot drift from the contract
            # the campaign numbers were measured under.
            while not (terminal or truncated):
                marker = (info.get("act"), info.get("floor"), info.get("phase_name"))
                if marker != last:
                    trace.append({"act": marker[0], "floor": marker[1], "phase": marker[2]})
                    last = marker
                mask = env.action_masks()
                if not any(bool(value) for value in mask):
                    truncated = True
                    dead_end = "empty_action_mask"
                    break
                action_raw, _ = model.predict(
                    observation, action_masks=mask, deterministic=deterministic
                )
                action = int(action_raw.item() if hasattr(action_raw, "item") else action_raw)
                if action < 0 or action >= len(mask) or not bool(mask[action]):
                    truncated = True
                    illegal_actions += 1
                    dead_end = "policy_illegal_action"
                    break
                # Record *before* stepping, against the state the choice was made
                # in: after a step the engine has already moved on and the numbers
                # would describe a different decision.
                if args.watch_steps and info.get("floor") == args.watch_floor \
                        and len(window) <= args.watch_steps:
                    # Decode from the raw observation with the same offsets the env
                    # uses to name actions (v2_flat_env._action_names).  Going
                    # through codec() would be wrong: it describes the engine's raw
                    # candidate set, which is not the index space the policy's mask
                    # was taken in.
                    flat_env = env.unwrapped
                    raw = flat_env.raw_observation()
                    base, slot = divmod(int(action), TARGET_SLOTS)
                    phase = int(raw[COMBAT_OBS_SIZE])
                    hand = [int(raw[8 + i * 2]) for i in range(MAX_HAND) if raw[8 + i * 2]]
                    if int(action) == SENTINEL_FLAT:
                        kind, detail = "sentinel_dead_end", ""
                    elif phase == PHASE_COMBAT:
                        if base < len(hand):
                            kind, detail = "play_card", f"card:{hand[base]}@hand{base}"
                        elif base == len(hand):
                            kind, detail = "end_turn", ""
                        elif base <= len(hand) + 3:
                            potions = [int(raw[28 + i * 2]) for i in range(3) if raw[28 + i * 2]]
                            idx = base - len(hand) - 1
                            kind = "potion"
                            detail = f"potion:{potions[idx]}" if idx < len(potions) else "potion:?"
                        else:
                            kind, detail = "other_combat", ""
                    else:
                        kind, detail = f"phase{phase}", ""
                    combat = tuple(int(v) for v in raw[:COMBAT_OBS_SIZE])
                    # Enemy slots: v2_observation.py:183-186 (base 54, stride 15,
                    # hp at +0, max_hp at +1).  Player HP declining is not enough to
                    # call this a stalemate -- whether the boss is being ground down
                    # is what separates "too slow" from "not fighting".
                    enemies = [
                        (int(raw[54 + slot * 15]), int(raw[54 + slot * 15 + 1]))
                        for slot in range(MAX_ENEMIES)
                        if int(raw[54 + slot * 15 + 1]) > 0
                    ]
                    # "It keeps ending its turn" only means something if it *could*
                    # have played.  Count the mask's playable-card candidates at
                    # this exact decision, so an energy-starved turn cannot be
                    # misread as reward exploitation.
                    flat_mask = flat_env.action_masks()
                    legal_plays = sum(
                        1 for index, on in enumerate(flat_mask)
                        if bool(on) and divmod(index, TARGET_SLOTS)[0] < len(hand)
                    )
                    window.append({
                        "step": steps,
                        "flat": int(action),
                        "kind": kind,
                        "detail": detail,
                        "target_slot": slot,
                        "hand_count": len(hand),
                        "legal_play_actions": legal_plays,
                        "legal_total": int(sum(1 for on in flat_mask if bool(on))),
                        "enemy_hp": [hp for hp, _ in enemies],
                        "enemy_max_hp": [m for _, m in enemies],
                        "player_hp": info.get("player_hp"),
                        # CombatObservation.cs:22 -- the block value is what turns
                        # "arrived with 61 HP" into an effective-HP statement, and it
                        # was the one quantity the loss model had to leave unmeasured.
                        "player_block": int(raw[2]),
                        "energy": int(raw[3]),
                        "combat_sig": hashlib.sha256(
                            repr(combat).encode("utf-8")).hexdigest()[:12],
                    })
                    tail_blocks.append(combat)
                    if state_shape is None:
                        state_shape = sorted(flat_env.state_info())
                observation, _reward, terminal, truncated, info = env.step(action)
                steps += 1
                if steps >= args.max_steps:
                    truncated = True
                    dead_end = "step_cap"
            # Native identity fields the combat block deliberately omits (see
            # v2_observation.py:19).  Without these, "something adds Wounds" cannot
            # be attributed to an encounter at all.
            final_state_info = dict(env.unwrapped.state_info()) if window else None
            won = bool(terminal and info.get("player_won", False))
            kinds = {}
            missed = 0
            alone = 0
            enemy_min = None
            for row in window:
                kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1
                if row["kind"] == "end_turn" and row["legal_play_actions"] > 0:
                    missed += 1
                if row["kind"] == "end_turn" and row["legal_total"] == 1:
                    alone += 1
                for hp in row["enemy_hp"]:
                    if enemy_min is None or hp < enemy_min:
                        enemy_min = hp
            # When did the fight stop being playable at all?  "It turtles" and
            # "it is locked out" look identical in a step count.
            last_actionable = next(
                (row["step"] for row in reversed(window) if row["legal_play_actions"] > 0),
                None,
            )
            first_locked = next(
                (row["step"] for row in window
                 if row["legal_play_actions"] == 0 and last_actionable is not None
                 and row["step"] > last_actionable),
                None,
            )
            signatures = [row["combat_sig"] for row in window]
            end_turn_sizes = {}
            for row in window:
                if row["kind"] == "end_turn":
                    end_turn_sizes[row["legal_total"]] = (
                        end_turn_sizes.get(row["legal_total"], 0) + 1
                    )
            final_window = {
                "recorded_steps": len(window),
                "chosen_action_kinds": dict(sorted(kinds.items(), key=lambda kv: -kv[1])),
                "end_turn_with_a_playable_card": missed,
                "end_turn_when_end_turn_was_the_only_legal_action": alone,
                "end_turn_legal_candidate_count_hist": dict(sorted(end_turn_sizes.items())),
                "last_step_with_a_playable_card": last_actionable,
                "decisions_after_playability_ended": (
                    None if last_actionable is None
                    else len(window) - 1 - next(
                        i for i, row in enumerate(window) if row["step"] == last_actionable)),
                "hand_locked_from_step": first_locked,
                "enemy_hp_first": window[0]["enemy_hp"],
                "enemy_hp_last": window[-1]["enemy_hp"],
                "enemy_hp_lowest_seen": enemy_min,
                "enemy_max_hp": window[0]["enemy_max_hp"],
                "distinct_combat_states": len(set(signatures)),
                "player_hp_values": sorted({row["player_hp"] for row in window},
                                           key=lambda v: (v is None, v)),
                "state_info_keys_at_first_record": state_shape,
                "first_rows": window[:12],
                "last_rows": window[-6:],
                **_tail_report(tail_blocks),
            } if window else None
            final = {
                "repeat": repeat,
                "policy_mode": "argmax" if deterministic else f"sampled({args.sample_seed}+{repeat})",
                "seed": seed,
                "checkpoint": str(checkpoint),
                "checkpoint_sha256": _sha256(checkpoint),
                "steps": steps,
                "illegal_actions": illegal_actions,
                "dead_end": dead_end,
                "final_act": info.get("act"),
                "final_floor": info.get("floor"),
                "final_phase": info.get("phase_name"),
                "run_won": won,
                "run_outcome": info.get("run_outcome"),
                "run_outcome_source": info.get("run_outcome_source"),
                "action_window": final_window,
                "final_state_info": final_state_info,
                # A run can stop without the policy dying: the retained-trace seed
                # carries an environment-side truncation signal of its own.  Without
                # these four fields "reached Act 2 then stopped" reads as a death.
                "final_player_hp": info.get("player_hp"),
                "final_player_max_hp": info.get("player_max_hp"),
                "run_terminated": info.get("run_terminated"),
                "run_truncated": info.get("run_truncated"),
                # Ambiguity that bit once already: a seed that *generates* Act 2
                # reaches act 2 without chaining, so "saw act 2" is not evidence of
                # the cross-act branch. Record the act the run started in and call
                # chaining only what it is -- act 1 followed by act 2.
                "started_in_act": next((int(t["act"]) for t in trace if t["act"]), 0),
                "chained_into_act_two": any(
                    t["act"] and int(t["act"]) >= 2
                    and trace[0]["act"] and int(trace[0]["act"]) == 1
                    for t in trace),
                "max_act": max((int(t["act"]) for t in trace if t["act"]), default=0),
                "max_floor": max((int(t["floor"]) for t in trace if t["floor"]), default=0),
                "trace": trace,
            }
            env.close()
            results.append(final)
            print(
                f"{checkpoint.name} r{repeat}: act={final['final_act']} "
                f"floor={final['final_floor']} phase={final['final_phase']} "
                f"hp={final['final_player_hp']}/{final['final_player_max_hp']} won={won} "
                f"illegal={illegal_actions} dead_end={dead_end} steps={steps} "
                f"started_in_act={final['started_in_act']} "
                f"chained={final['chained_into_act_two']}"
            )

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        # The label has to follow the seeds actually rolled: a file claiming a
        # "two-act flow" while containing ordinary seeds would read as if those
        # seeds chained, which only the retained-trace seed can do.
        "scope": ("simulator_chained_demo_seed" if not non_chained
                  else "simulator_act1_single_act_seeds"),
        "evidences": ("two-act flow reachable from the V2 harness" if not non_chained
                      else "Act 1 boss flows on non-scripted seeds"),
        "does_not_evidence": [
            "policy generalisation across acts (Act 1 of this seed is a hardcoded trace)",
            "a three-act clear (the emulator has no Act 3)",
            "real-game A10 acceptance",
        ] + ([] if not non_chained else [
            "a two-act flow: only the retained-trace seed chains (RunEngine.cs:1909); "
            "these seeds are generated normally rather than scripted",
        ]),
        "chained_branch_source": "third_party/.../RunEngine.cs:1907-1920",
        "results": results,
    }
    if non_chained:
        print(
            f"\nScope: {len(non_chained)} seed(s) here are not the retained-trace seed, so "
            "none of them reaches Act 2; these are ordinary Act 1 boss rolls."
        )
    else:
        print(
            "\nScope: this seed's Act 1 is scripted (RunMapGenerator.cs:792-947), so a win "
            "here shows the harness can finish a two-act flow, not that the policy transfers."
        )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
