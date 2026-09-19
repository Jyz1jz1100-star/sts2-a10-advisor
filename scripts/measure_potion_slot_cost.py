"""Does the phantom third potion slot cost the policy potions at the boss?

``probe_shop_refusals.py`` found that all 641 shop refusals in 1,500 episodes are one engine
inconsistency: ``WriteActionMask`` advertises a potion purchase whenever *any* of the three
``State.PotionSlots`` is empty (RunEngine.cs:727) while ``AddPotion`` fills only
``min(2, len)`` of them (RunRewardGenerator.cs:1015). So a policy that holds two potions is
offered a third purchase it can never make.

The reason that is worth measuring rather than filing: the campaign's Act-1 deficit was
located in **in-fight mitigation rate**, and potions are a mitigation resource. This script
tests the link evaluation-only, which is what the 2026-09-02 PPO freeze permits.

Pre-registered before the artifact existed, because the sample is small and the honest
version of this question is "direction and rough size", not "effect on wins":

* population: the act1 promotion partition, seeds ``[offset, offset+limit)`` -- the same
  checkpoint and windows every census figure in the report uses;
* exposure: an episode is *refused* if at least one potion-capacity refusal occurs **before**
  its first floor-17 decision, so the comparison is not contaminated by post-boss states;
* primary outcome: usable potions held at that first boss decision, ``slots[:2]`` non-empty,
  i.e. the capacity the engine actually implements, split by *generated* act;
* secondary: successful potion purchases per episode, and the phantom third slot's occupancy;
* internal validation: the arrival and win counts must reproduce the committed census
  (162 arrivals, 25 boss wins over 3,500 seeds) or the instrumentation is wrong, not the
  census;
* unreadable outcome: fewer than 15 refused-exposure arrivals in an act means the comparison
  is reported as descriptive only, with the bootstrap interval printed and no directional claim.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import hashlib
import json
import random
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "third_party" / "slay-the-spire-2-emulator-main" / "src"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from probe_shop_refusals import (O_DECK, O_GOLD, O_POTION_0, O_SLOT_0,  # noqa: E402
                                 classify)
from training.v2_constants import (COMBAT_OBS_SIZE, NODE_BOSS,  # noqa: E402
                           PHASE_COMPLETE)
from training.v2_flat_env import TARGET_SLOTS  # noqa: E402

ACT_NAMES = {1: "act1_overgrowth", 2: "act2_underdocks"}


def bootstrap_ci(samples: list[int], reps: int = 4000, seed: int = 20260919) -> list[float]:
    if not samples:
        return []
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(samples, k=len(samples))) for _ in range(reps))
    return [round(means[int(0.025 * reps)], 3), round(means[int(0.975 * reps) - 1], 3)]


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

    rows: list[dict] = []
    for seed in window:
        env = factory(int(seed))
        observation, info = env.reset(seed=int(seed))
        terminal = truncated = False
        steps = 0
        state = {"refusals_before_boss": 0, "refusals_total": 0, "potions_bought": 0,
                 "boss_seen": False, "potions_at_boss": None, "phantom_at_boss": None,
                 "act_at_boss": None, "floor_at_boss": None, "decks_at_boss": None,
                 "terminal_floor": None, "terminal_phase": None, "player_won": False,
                 "terminal_engine_phase": None,
                 "shops_visited": 0}
        seen_shop_floors: set[int] = set()
        core = env.unwrapped
        while not (terminal or truncated):
            mask = env.action_masks()
            if not any(bool(v) for v in mask):
                break
            run = core.raw_observation()[COMBAT_OBS_SIZE:]
            phase = core.state_info().get("phase_name")
            floor, node = int(run[1]), int(run[8])
            slots = [int(run[O_SLOT_0 + i]) for i in range(3)]
            if not state["boss_seen"] and floor == 17 and node == NODE_BOSS:
                state.update(boss_seen=True, potions_at_boss=sum(s != 0 for s in slots[:2]),
                             phantom_at_boss=slots[2] != 0, act_at_boss=int(run[2]),
                             floor_at_boss=floor, decks_at_boss=int(run[O_DECK]))
            if phase == "shop" and floor not in seen_shop_floors:
                seen_shop_floors.add(floor)
                state["shops_visited"] += 1
            raw, _ = model.predict(observation, action_masks=mask, deterministic=True)
            action = int(raw.item() if hasattr(raw, "item") else raw)
            _obs, _r, terminal, truncated, info = env.step(action)
            steps += 1
            base = action // TARGET_SLOTS
            if info.get("native_rejection_filtered"):
                lists = core._core.state_lists()
                why = classify(phase, base, int(run[O_GOLD]), int(run[O_DECK]),
                               list(lists["shop_cards"]), list(lists["shop_costs"]),
                               [int(run[28 + i]) for i in range(3)],
                               [int(run[O_POTION_0 + i]) for i in range(3)], slots)
                if why == "potion_capacity_mask_mismatch":
                    state["refusals_total"] += 1
                    if not state["boss_seen"]:
                        state["refusals_before_boss"] += 1
            elif 10 <= base <= 12:
                state["potions_bought"] += 1
            observation = _obs
            if steps >= args.max_steps:
                truncated = True
        final = core.state_info()
        # The wrapper's phase_name labels the *episode* (a dead end also reads "complete"), so
        # the clear test has to come from the engine's own phase field: RunPhase.Complete == 6.
        state.update(terminal_floor=final.get("floor"), terminal_phase=final.get("phase_name"),
                     player_won=bool(final.get("player_won")),
                     terminal_engine_phase=int(core.raw_observation()[COMBAT_OBS_SIZE]))
        rows.append({"seed": int(seed), **state})
        env.close()

    arrivals = [r for r in rows if r["boss_seen"]]
    payload = {
        "aggregates": {
            "arrivals": len(arrivals),
            "arrivals_by_generated_act": dict(collections.Counter(
                ACT_NAMES.get(r["act_at_boss"], str(r["act_at_boss"])) for r in arrivals)),
            "episodes": len(rows),
            "phantom_third_slot_occupied_at_boss": sum(
                1 for r in arrivals if r["phantom_at_boss"]),
            # A terminal RunPhase.Complete is NOT a clear: the engine sets it on a loss as
            # well (RunEngine.cs:1286-1293 sets it in the else-branch of `if (result
            # .PlayerWon)`), which is the field-level reason floor 17 reports the same value
            # for dying at the boss and beating it. So the clear test is Complete AND the
            # last-combat flag (State.LastPlayerWon, RunEngine.cs:1282). The wrapper's own
            # phase_name is kept only to show it labels the episode, not the engine phase.
            "arrivals_ending_in_engine_phase_complete": sum(
                1 for r in arrivals if r["terminal_engine_phase"] == PHASE_COMPLETE),
            "arrivals_cleared": sum(1 for r in arrivals
                                    if r["terminal_engine_phase"] == PHASE_COMPLETE
                                    and r["player_won"]),
            "cleared_by_generated_act": dict(collections.Counter(
                ACT_NAMES.get(r["act_at_boss"], str(r["act_at_boss"])) for r in arrivals
                if r["terminal_engine_phase"] == PHASE_COMPLETE and r["player_won"])),
            "arrivals_with_last_combat_flag": sum(1 for r in arrivals if r["player_won"]),
            "arrivals_whose_wrapper_phase_name_is_complete": sum(
                1 for r in arrivals if r["terminal_phase"] == "complete"),
            "refused_exposure_arrivals_by_act": dict(collections.Counter(
                ACT_NAMES.get(r["act_at_boss"], str(r["act_at_boss"])) for r in arrivals
                if r["refusals_before_boss"])),
        },
        "by_act_and_exposure": {
            f"{ACT_NAMES.get(act, act)}|{group}": {
                "n": len(sample),
                "mean_potions_at_boss": round(statistics.fmean(
                    [r["potions_at_boss"] for r in sample]), 3) if sample else None,
                "bootstrap_ci_95": bootstrap_ci(
                    [r["potions_at_boss"] for r in sample]) if sample else [],
                "mean_potions_bought_per_episode": round(statistics.fmean(
                    [r["potions_bought"] for r in sample]), 3) if sample else None,
                "mean_shops_visited": round(statistics.fmean(
                    [r["shops_visited"] for r in sample]), 3) if sample else None,
            }
            for act in (1, 2)
            for group, flag in (("refused_before_boss", lambda r: r["refusals_before_boss"] > 0),
                                ("no_refusal_before_boss", lambda r: not r["refusals_before_boss"]))
            for sample in [[r for r in arrivals
                            if (r["act_at_boss"] == act) and flag(r)]]
        },
        "checkpoint_sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "rows": rows,
        "scope": "simulator_act1 label, mixed-act population, argmax, one checkpoint",
        "seed_window": {"offset": args.start_offset, "limit": args.limit,
                        "stage": args.stage, "split": args.split},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(f"{len(rows)} episodes, {len(arrivals)} boss arrivals "
          f"{payload['aggregates']['arrivals_by_generated_act']}")
    print("arrivals in engine Complete:",
          payload["aggregates"]["arrivals_ending_in_engine_phase_complete"],
          "| cleared:", payload["aggregates"]["arrivals_cleared"],
          payload["aggregates"]["cleared_by_generated_act"],
          "| wrapper-name complete:",
          payload["aggregates"]["arrivals_whose_wrapper_phase_name_is_complete"])
    for key, block in payload["by_act_and_exposure"].items():
        print(f"  {key}: n={block['n']} mean_potions={block['mean_potions_at_boss']} "
              f"ci={block['bootstrap_ci_95']} bought={block['mean_potions_bought_per_episode']}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
