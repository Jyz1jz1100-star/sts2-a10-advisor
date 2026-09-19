"""Where can an upgraded card come from in this emulator, and what do decks end up with?

``measure_arrival_state_at_boss.py`` found the policy reaches a boss with an 18-card deck holding
**0.63 upgrades on average**, and 58% of Act-1 arrivals carry none at all. That reads like a
policy behaviour until the offer side is checked -- so this script counts offers as well as
outcomes:

* every ``card_reward`` decision records the engine's own ``RewardUpgraded`` flags for the three
  cards on offer, and the option the policy took;
* the outcome side records, per seed, how many upgraded cards ended up in the deck.

The reason it is worth a run is what the source says about the first of those.
``RunRewardGenerator.cs:800`` sets ``RewardUpgraded[i] = silverCrucibleUpgrade ||
RollCardUpgrade(...)``, and ``RollCardUpgrade`` (``RunRewardGenerator.cs:1127-1131``) is

    private static bool RollCardUpgrade(RunState state, int cardId, GameRng rng)
    {
        _ = rng.NextDouble();
        return false;
    }

-- it consumes an RNG draw and returns false unconditionally. So the upgraded-reward pathway is
implemented in *shape* (a flag array, an RNG slot, a take path at ``RunEngine.cs:1737`` that
honours it) and disabled in fact, and the only remaining sources are events
(``AddEventRewardCard(upgraded: ...)``), Neow, the Silver Crucible relic, and the hard-coded
demo-seed branches in ``ApplyRetainedTraceCardReward``.

What this measures, then, is the *ceiling that pathway imposes*: if ordinary rewards never carry
an upgrade, the deck the policy builds for a 183-324 HP boss is capped near its base cards
regardless of how well it plays, and that is a property of the simulator, not of the policy.

Pre-registered:

* population: the act1 promotion partition, first 3,500 seeds, the campaign checkpoint;
* expected if the stub reading is right: **zero** upgraded offers over all card-reward
  decisions, and a final-deck upgrade mean far below deck growth (~8 cards taken per run);
* what would falsify the reading: any card-reward decision with an upgraded offer at a floor/HP
  combination that is not one of the hard-coded demo branches -- that would mean a live roll;
* not established by this run: what the shipped game does. Only the emulator is in evidence here.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import hashlib
import json
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "third_party" / "slay-the-spire-2-emulator-main" / "src"))
sys.path.insert(0, str(ROOT))

REWARD_SKIP = 3  # RunConstants.RewardSkipAction

# ApplyRetainedTraceCardReward (RunRewardGenerator.cs:806 onward) hands out fixed card triples when
# the run state matches these guards. Only the floor-3 branch also requires the demo string seed;
# these five are guarded on mutable values alone, so an ordinary seed that passes through them is
# served scripted content. {floor: (player_hp, gold)}.
SCRIPTED_GUARDS = {4: (69, 111), 5: (74, 120), 6: (80, 129), 7: (60, 138), 9: (56, 168)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "config/training_v2.toml")
    parser.add_argument("--stage", default="act1")
    parser.add_argument("--split", default="promotion")
    parser.add_argument("--limit", type=int, default=3500)
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
    open_stage = dataclasses.replace(stage, max_floor=None, max_episode_steps=args.max_steps)
    factory = _environment_factory(config, open_stage, sts2_gym)
    checkpoint = args.checkpoint.resolve()
    probe = DummyVecEnv([lambda: factory(window[0])])
    model = MaskablePPO.load(str(checkpoint), env=probe, device="cpu")
    probe.close()

    events: list[dict] = []
    per_seed: list[dict] = []
    for seed in window:
        env = factory(int(seed))
        observation, info = env.reset(seed=int(seed))
        terminal = truncated = False
        steps = taken_upgraded = rewards_seen = 0
        core = env.unwrapped
        while not (terminal or truncated):
            mask = env.action_masks()
            if not any(bool(value) for value in mask):
                break
            state = core.state_info()
            phase = state.get("phase_name")
            flags: list[int] = []
            if phase == "card_reward":
                flags = [int(flag) for flag in core._core.state_lists()["reward_upgraded"]]
            raw, _ = model.predict(observation, action_masks=mask, deterministic=True)
            action = int(raw.item() if hasattr(raw, "item") else raw)
            if phase == "card_reward":
                # Reward screens are unsplit -- target slots only exist in combat -- so the flat
                # action is the option index, and anything outside 0..2 is the skip button.
                chosen = action if 0 <= action <= 2 else REWARD_SKIP
                took_upgraded = chosen <= 2 and bool(flags[chosen])
                # hp and gold are recorded because the scripted reward branches in
                # ApplyRetainedTraceCardReward are guarded on (Floor, PlayerHp, Gold) -- those
                # three values, not the offered card ids, are what identifies a scripted screen.
                # (``pending_rewards`` was tried as a source of the offered ids and is not them:
                # it reads like reward *counters*, e.g. [9,0,0,0] where the engine held
                # RewardCards = 20/508/238.)
                events.append({"seed": int(seed), "floor": int(state["floor"]),
                               "hp": int(state["player_hp"]), "gold": int(state["gold"]),
                               "offered_upgraded": list(flags),
                               "chosen": chosen, "took_upgraded": took_upgraded,
                               "upgraded_options": [i for i, flag in enumerate(flags) if flag]})
                rewards_seen += 1
                taken_upgraded += int(took_upgraded)
            _obs, _r, terminal, truncated, info = env.step(action)
            observation = _obs
            steps += 1
            if steps >= args.max_steps:
                truncated = True
        deck = [int(card) for card in core._core.state_lists()["deck"]]
        per_seed.append({"seed": int(seed), "rewards_seen": rewards_seen,
                         "upgrades_taken_at_rewards": taken_upgraded,
                         "upgraded_in_final_deck": sum(1 for card in deck if card < 0),
                         "deck_size_final": len(deck),
                         "distinct_defs_final": len({abs(card) for card in deck})})
        env.close()

    offered = [event for event in events if any(event["offered_upgraded"])]
    guard_states = {(floor, hp, gold) for floor, (hp, gold) in SCRIPTED_GUARDS.items()}
    offered_on_guard = sum(1 for event in offered
                           if (event["floor"], event["hp"], event["gold"]) in guard_states)
    all_offers_explained = offered_on_guard == len(offered)
    scripted_hits = {floor: [event for event in events
                             if event["floor"] == floor
                             and (event["hp"], event["gold"]) == guard]
                     for floor, guard in SCRIPTED_GUARDS.items()}
    upgrade_hist = dict(sorted(collections.Counter(
        row["upgraded_in_final_deck"] for row in per_seed).items()))
    payload = {
        "aggregates": {
            "card_reward_decisions": len(events),
            "decisions_with_an_upgraded_offer": len(offered),
            "share_of_rewards_offering_an_upgrade": round(
                len(offered) / len(events), 5) if events else None,
            "took_an_upgraded_offer": sum(1 for event in offered if event["took_upgraded"]),
            "episodes": len(per_seed),
            "mean_rewards_per_episode": round(statistics.fmean(
                [row["rewards_seen"] for row in per_seed]), 3),
            "mean_deck_size_final": round(statistics.fmean(
                [row["deck_size_final"] for row in per_seed]), 3),
            "mean_upgraded_in_final_deck": round(statistics.fmean(
                [row["upgraded_in_final_deck"] for row in per_seed]), 3),
            "share_of_episodes_with_zero_upgrades": round(
                sum(1 for row in per_seed if row["upgraded_in_final_deck"] == 0)
                / len(per_seed), 4),
            "final_deck_upgrade_histogram": upgrade_hist,
        },
        "checkpoint_sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
        "scripted_reward_screens": {
            "guards": {str(floor): {"hp": hp, "gold": gold}
                       for floor, (hp, gold) in SCRIPTED_GUARDS.items()},
            "hits_by_floor": {str(floor): len(hits) for floor, hits in scripted_hits.items()},
            "total_hits": sum(len(hits) for hits in scripted_hits.values()),
            "share_of_reward_decisions": round(
                sum(len(hits) for hits in scripted_hits.values()) / len(events), 6) if events else None,
            "hit_events": [event for hits in scripted_hits.values() for event in hits],
            "reading": ("a hit means this reward screen was served from the hard-coded table "
                        "rather than drawn from the Ironclad reward pool -- and the guard is "
                        "(floor, hp, gold) only, so these are ordinary seeds, not the demo seed"),
        },
        "established": ([
            f"the roll produced no upgraded card offer anywhere in this population: "
            f"{offered_on_guard} of {len(events)} card-reward decisions had one and all "
            f"{len(offered)} such decisions sit on a (floor, hp, gold) state that the hard-coded "
            f"reward table guards -- consistent with RollCardUpgrade returning false always",
            "the upgrades a run does end with are therefore not coming from combat rewards",
        ] if all_offers_explained else [
            "NOT as expected: an upgraded reward appeared at a state the scripted table does not "
            "guard, which means RollCardUpgrade is not the only source and the stub reading needs "
            "revisiting"]),
        "events": events,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "not_established": [
            "what the shipped game does with this roll -- only the emulator is in evidence",
            "that removing the stub would raise win rate: this measures an availability ceiling, "
            "not a counterfactual outcome",
            "where the observed upgrades come from event by event; the per-seed counts bound the "
            "total non-reward contribution, they do not attribute it"],
        "per_seed": per_seed,
        "reproduce": ("../third_party/slay-the-spire-2-emulator-main/.venv/Scripts/python.exe "
                      "scripts/measure_reward_upgrade_availability.py --checkpoint <zip> "
                      f"--limit {args.limit} --start-offset {args.start_offset} --out <file.json>"),
        "scope": ("simulator_act1 label, argmax, one checkpoint, "
                  f"{len(per_seed)} seeds; card-reward offer census plus final deck state"),
        "seed_window": {"offset": args.start_offset, "limit": args.limit,
                        "stage": args.stage, "split": args.split},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")
    agg = payload["aggregates"]
    print(f"{agg['episodes']} episodes, {agg['card_reward_decisions']} card-reward decisions, "
          f"{agg['decisions_with_an_upgraded_offer']} with an upgraded offer "
          f"(share={agg['share_of_rewards_offering_an_upgrade']})")
    print(f"  final deck: mean size {agg['mean_deck_size_final']}, mean upgrades "
          f"{agg['mean_upgraded_in_final_deck']}, zero-upgrade share "
          f"{agg['share_of_episodes_with_zero_upgrades']}")
    print(f"  upgrade histogram: {upgrade_hist}")
    print("  scripted reward-table hits (ordinary seeds on a guarded state): "
          f"{payload['scripted_reward_screens']['hits_by_floor']} "
          f"total={payload['scripted_reward_screens']['total_hits']} of "
          f"{agg['card_reward_decisions']} decisions")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
