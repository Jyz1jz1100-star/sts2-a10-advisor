"""Combat rewards cannot roll an upgrade, so events are the only ordinary source of one. Do they appear, and does the policy take them?

``measure_reward_upgrade_availability.py`` established the ceiling:
``RollCardUpgrade`` (``RunRewardGenerator.cs:1127-1131``) burns an RNG draw and returns false, so
across 12,677 card-reward decisions exactly two carried an upgrade and both came from the demo
reward table. Runs still end with 0.326 upgraded cards on average, which has to come from
somewhere else -- and the source says where, by two mechanisms:

* ten events call ``RunNonCombatEffects.UpgradeFirstCard(State)``, promoting a card already held;
* seven events call ``AddEventRewardCard``, four with ``upgraded: true`` and two with
  ``upgraded: action == 2``.

An earlier pass of this script looked only at the second mechanism and reported 48 realised
upgrades against ~1,141 in the population -- the arithmetic was the thing that caught it, so both
mechanisms are now classified and both call-site counts are checked against the source.

That splits "why are the decks so old" into two measurable questions, and this script answers both,
evaluation-only (the 2026-09-02 PPO freeze permits measurement, not training):

* **opportunity** -- how often is one of these seventeen events visited per run? If the answer is
  "far less than once", then 0.326 upgrades is an availability fact and no policy change can fix
  it, which is a different conclusion from "the policy turns upgrades down";
* **choice** -- for the two events whose upgrade depends on the option picked
  (``action == 2``), which option does the policy take? A policy that never picks option 2 there
  is leaving the only reachable upgrade on the table, and that *is* a learnable deficit.

Instrumentation notes, because both readings are easy to get wrong:

* the classification is parsed from the checked-in source at run time (via
  ``audit_event_mask_cases.step_case_bodies``), not transcribed, and the script refuses to run if
  either call-site count disagrees with what it classified -- a source change breaks it loudly
  instead of silently measuring the wrong events (``tests/test_event_upgrade_parser.py``);
* an event's card arrives through a *pending* card-reward screen, so an outcome delta is read
  only after the run leaves both the ``event`` and ``card_reward`` phases; measuring right after
  the event action would attribute nothing to every event;
* the deck list is read signed (``CombatFactory.cs:243``), so an upgraded card is a negative id;
* the flat action is divided by ``TARGET_SLOTS`` to get the option index, since option ids are
  split into target slots in the flat encoding.

Pre-registered outcome: mean final-deck upgrades must match the census (0.326 over the same 3,500
seeds) -- it is reproduced here by a different instrument -- and the realised event deltas plus a
named residual must account for the total, because a residual larger than the event contribution
means a third upgrade source is still unclassified.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import hashlib
import json
import re
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "third_party" / "slay-the-spire-2-emulator-main" / "src"))
sys.path.insert(0, str(ROOT))

from audit_event_mask_cases import DEFAULT_CONSTANTS, DEFAULT_ENGINE, step_case_bodies  # noqa: E402
from training.v2_constants import COMBAT_OBS_SIZE  # noqa: E402

UPGRADE_OPTION = 2  # the arm the source guards with `upgraded: action == 2`
OPEN_PHASES = {"event", "card_reward"}


def classify_events(engine_source: str, constants_source: str) -> dict[int, dict]:
    """Parse which events grant a card, and which upgrade a card already held.

    Two mechanisms, because the engine has both. ``AddEventRewardCard`` puts a card into the
    pending-reward flow, with an ``upgraded:`` argument; ``UpgradeFirstCard(State)`` promotes a
    card already in the deck, takes no option, and is what actually accounts for most of the
    upgrades a run ends with. Reading only the first mechanism understates the opportunity set by
    an order of magnitude, so both are collected and both counts are checked below.
    """
    arms = step_case_bodies(engine_source)
    card_calls = len(re.findall(r"AddEventRewardCard\(", engine_source)) - len(
        re.findall(r"private void AddEventRewardCard", engine_source))
    held_calls = len(re.findall(r"UpgradeFirstCard\(State\)", engine_source))
    out: dict[int, dict] = {}
    card_classified = held_classified = 0
    for name, body in arms.items():
        mechanisms: dict[str, int] = {}
        match = re.search(r"AddEventRewardCard\(([^)]*)\)", body)
        if match:
            argument = match.group(1).strip()
            if "upgraded: true" in argument:
                mechanisms["always_upgraded_card"] = 1
            elif re.search(r"upgraded:\s*action\s*==\s*2", argument):
                mechanisms["upgraded_card_if_option_two"] = 1
            elif not argument:
                mechanisms["plain_card"] = 1
            else:
                mechanisms[f"unclassified({argument})"] = 1
            card_classified += 1
        held = len(re.findall(r"UpgradeFirstCard\(State\)", body))
        if held:
            mechanisms["upgrades_a_held_card"] = held
            held_classified += held
        if not mechanisms:
            continue
        id_match = re.search(rf"\b{name}\s*=\s*(\d+)", constants_source)
        if not id_match:
            raise SystemExit(f"could not resolve event constant {name}")
        out[int(id_match.group(1))] = {"name": name, "mechanisms": mechanisms}
    if card_classified != card_calls:
        raise SystemExit(f"classified {card_classified} card-granting event arms but the source "
                         f"has {card_calls} AddEventRewardCard call sites -- the parser is stale")
    if held_classified != held_calls:
        raise SystemExit(f"classified {held_classified} held-card upgrade calls inside event arms "
                         f"but the source has {held_calls} UpgradeFirstCard(State) sites -- some "
                         f"are outside StepEvent, which this census does not cover")
    return out


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
    from training.v2_flat_env import TARGET_SLOTS

    events_by_id = classify_events(DEFAULT_ENGINE.read_text(encoding="utf-8"),
                                   DEFAULT_CONSTANTS.read_text(encoding="utf-8"))
    by_mechanism = collections.Counter(
        name for entry in events_by_id.values() for name in entry["mechanisms"])

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

    sightings: list[dict] = []
    per_seed: list[dict] = []
    for seed in window:
        env = factory(int(seed))
        observation, info = env.reset(seed=int(seed))
        terminal = truncated = False
        steps = 0
        pending: dict | None = None
        visits_this_run = 0
        core = env.unwrapped
        last_upgraded = sum(1 for card in core._core.state_lists()["deck"] if card < 0)
        while not (terminal or truncated):
            mask = env.action_masks()
            if not any(bool(value) for value in mask):
                break
            state = core.state_info()
            phase = state.get("phase_name")
            deck_upgraded = sum(1 for card in core._core.state_lists()["deck"] if card < 0)
            if pending is not None and phase not in OPEN_PHASES:
                pending.update(upgrade_delta=deck_upgraded - pending["up_before"],
                               deck_delta=int(state["deck_size"]) - pending["deck_before"])
                sightings.append(pending)
                visits_this_run += 1
                pending = None
            if phase == "event" and pending is None:
                event_id = int(state["event_id"])
                known = events_by_id.get(event_id)
                if known is not None:
                    pending = {"seed": int(seed), "floor": int(state["floor"]),
                               "event_id": event_id, "event_name": known["name"],
                               "mechanisms": dict(known["mechanisms"]),
                               "up_before": last_upgraded,
                               "deck_before": int(state["deck_size"]), "chosen": None}
            raw, _ = model.predict(observation, action_masks=mask, deterministic=True)
            action = int(raw.item() if hasattr(raw, "item") else raw)
            if pending is not None and phase == "event" and pending["chosen"] is None:
                pending["chosen"] = action // TARGET_SLOTS
            _obs, _r, terminal, truncated, info = env.step(action)
            observation = _obs
            steps += 1
            last_upgraded = deck_upgraded
            if steps >= args.max_steps:
                truncated = True
        if pending is not None:  # the episode ended mid-flow; keep what is known
            pending.update(upgrade_delta=None, deck_delta=None)
            sightings.append(pending)
            visits_this_run += 1
        final = core.state_info()
        deck = [int(card) for card in core._core.state_lists()["deck"]]
        per_seed.append({"seed": int(seed),
                         "upgraded_in_final_deck": sum(1 for card in deck if card < 0),
                         "card_granting_event_visits": visits_this_run,
                         "terminal_floor": int(final["floor"]),
                         "terminal_engine_phase": int(
                             core.raw_observation()[COMBAT_OBS_SIZE]),
                         "player_won": bool(final.get("player_won"))})
        env.close()

    realised = sum(1 for sighting in sightings if (sighting.get("upgrade_delta") or 0) > 0)
    conditional = [sighting for sighting in sightings
                   if "upgraded_card_if_option_two" in sighting["mechanisms"]]
    payload = {
        "aggregates": {
            "episodes": len(per_seed),
            "card_granting_event_visits": len(sightings),
            "visits_per_episode": round(len(sightings) / len(per_seed), 4),
            "visits_by_mechanism": dict(collections.Counter(
                mechanism for sighting in sightings
                for mechanism in sighting["mechanisms"])),
            "visits_by_event": dict(collections.Counter(
                sighting["event_name"] for sighting in sightings)),
            "events_that_realised_an_upgrade": realised,
            "upgrades_realised_per_visit": round(
                sum((s.get("upgrade_delta") or 0) for s in sightings) / len(sightings), 4)
                if sightings else None,
            "realised_upgrades_per_episode": round(realised / len(per_seed), 4),
            "option_picked_on_conditional_events": dict(collections.Counter(
                str(sighting["chosen"]) for sighting in conditional)),
            "conditional_event_visits": len(conditional),
            "mean_upgraded_in_final_deck": round(statistics.fmean(
                [row["upgraded_in_final_deck"] for row in per_seed]), 3),
            # What the realised event deltas do not account for. Neow's offer and the Silver
            # Crucible relic path also add upgrades and are not separated in this run.
            "upgrades_beyond_event_deltas": round(
                sum(row["upgraded_in_final_deck"] for row in per_seed) - realised, 1),
            "arrivals_at_boss": sum(1 for row in per_seed
                                    if row["terminal_floor"] >= 17),
        },
        "classified_events": {str(event_id): entry for event_id, entry in sorted(events_by_id.items())},
        "classified_mechanism_counts": dict(by_mechanism),
        "checkpoint_sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "not_established": [
            "that events are the only upgrade source: the Silver Crucible relic path "
            "(silverCrucibleUpgrade at RunRewardGenerator.cs:800) and Neow are not separated here",
            "that picking the upgraded option would raise win rate -- this counts opportunities "
            "and choices, not outcomes under a counterfactual policy",
            "anything about events outside these seven: only card-granting arms are classified"],
        "per_seed": per_seed,
        "reproduce": ("../third_party/slay-the-spire-2-emulator-main/.venv/Scripts/python.exe "
                      "scripts/measure_event_upgrade_opportunity.py --checkpoint <zip> --limit "
                      f"{args.limit} --start-offset {args.start_offset} --out <file.json>"),
        "sightings": sightings,
        "scope": ("simulator_act1 label, argmax, one checkpoint, "
                  f"{len(per_seed)} seeds; event card-grant opportunities and the option taken"),
        "seed_window": {"offset": args.start_offset, "limit": args.limit,
                        "stage": args.stage, "split": args.split},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")
    agg = payload["aggregates"]
    print(f"{agg['episodes']} episodes, {agg['card_granting_event_visits']} card-granting event "
          f"visits ({agg['visits_per_episode']} per episode)")
    print(f"  events that realised an upgrade: {agg['events_that_realised_an_upgrade']} "
          f"({agg['realised_upgrades_per_episode']} per episode); mean final-deck upgrades "
          f"{agg['mean_upgraded_in_final_deck']}")
    print(f"  visits by event: {agg['visits_by_event']}")
    print(f"  option picked on the two action==2 events: "
          f"{agg['option_picked_on_conditional_events']} of {agg['conditional_event_visits']}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
