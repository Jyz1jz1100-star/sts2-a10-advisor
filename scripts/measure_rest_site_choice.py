"""At a campfire the policy may heal or upgrade, and that choice is the deck-strength lever left to it.

Two earlier censuses bracket this one. Combat card rewards cannot produce an upgraded card at all
(``RollCardUpgrade`` returns false -- ``reward_upgrade_availability_20260919.json``), and the
event-driven routes are scarce: ten events promote a held card and seven grant a card
(``measure_event_upgrade_opportunity.py``), which between them realise roughly a third of the
upgrades runs end with. The rest of it has to come from the one place the player *decides*:
``StepRest`` (``RunEngine.cs``) offers ``RestHealAction = 0`` and ``RestUpgradeAction = 1``, the
upgrade entering ``RunPhase.TransformSelect`` for the card choice, and refuses the upgrade when no
card in the deck is upgradable.

That matters for the campaign's Act-1 wall because the two rest options trade against each other in
exactly the terms the wall is stated in: the report found the deficit is **how long the policy
survives the boss fight**, and healing buys survival now while an upgrade buys it later. If the
policy never picks option 1, its decks stay base cards by choice, not by scarcity -- learnable. If
it picks it and still ends near zero upgrades, the constraint is availability.

Pre-registered:

* population: the act1 promotion partition, first 3,500 seeds, the campaign checkpoint;
* one record per rest visit, read *before* the action: the floor, HP/max HP, which of the three
  options the native mask advertises (``base_action_mask()``, not the flat 225-wide one), the
  option taken; then, once the run leaves ``rest``/``transform_select``, the HP delta and the
  deck's upgraded-card delta -- the upgrade lands only after the select screen;
* **primary**: of the visits where the upgrade was advertised, the share the policy took. Near
  zero with HP deltas showing routine healing is a choice; a low share with the upgrade often
  unadvertised (no upgradable card) is availability;
* **secondary**: upgrades realised per rest visit, and how many visits occurred at all per run;
* accounting: rest upgrades + event upgrades should approach the census mean of 0.326 upgrades per
  episode; a large leftover names an unclassified source rather than being averaged away;
* a rest visit that ends the episode (death mid-select) is kept with delta None, not dropped.
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

REHEAL, REUPGRADE, SKIP = 0, 1, 3  # RunConstants.Rest*Action
REST_PHASES = {"rest", "transform_select"}


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

    visits: list[dict] = []
    per_seed: list[dict] = []
    for seed in window:
        env = factory(int(seed))
        observation, info = env.reset(seed=int(seed))
        terminal = truncated = False
        steps = 0
        pending: dict | None = None
        core = env.unwrapped
        visits_this_run = upgrades_from_rest = 0
        while not (terminal or truncated):
            mask = env.action_masks()
            if not any(bool(value) for value in mask):
                break
            state = core.state_info()
            phase = state.get("phase_name")
            deck = [int(card) for card in core._core.state_lists()["deck"]]
            upgraded = sum(1 for card in deck if card < 0)
            if pending is not None and phase not in REST_PHASES:
                pending.update(hp_delta=int(state["player_hp"]) - pending["hp_at_choice"],
                               upgrade_delta=upgraded - pending["up_at_choice"],
                               resolved=True)
                upgrades_from_rest += int((pending["upgrade_delta"] or 0) > 0)
                visits.append(pending)
                visits_this_run += 1
                pending = None
            if phase == "rest" and pending is None:
                base_mask = core.base_action_mask()
                pending = {"seed": int(seed), "floor": int(state["floor"]),
                           "hp_at_choice": int(state["player_hp"]),
                           "max_hp": int(state["player_max_hp"]),
                           "deck_size": len(deck), "up_at_choice": upgraded,
                           "legal": {name: bool(base_mask[option]) for name, option in
                                     (("heal", REHEAL), ("upgrade", REUPGRADE), ("skip", SKIP))},
                           "chosen": None}
            raw, _ = model.predict(observation, action_masks=mask, deterministic=True)
            action = int(raw.item() if hasattr(raw, "item") else raw)
            if pending is not None and phase == "rest" and pending["chosen"] is None:
                pending["chosen"] = action // TARGET_SLOTS
            _obs, _r, terminal, truncated, info = env.step(action)
            observation = _obs
            steps += 1
            if steps >= args.max_steps:
                truncated = True
        if pending is not None:
            pending.update(hp_delta=None, upgrade_delta=None, resolved=False)
            visits.append(pending)
            visits_this_run += 1
        final = core.state_info()
        deck = [int(card) for card in core._core.state_lists()["deck"]]
        per_seed.append({"seed": int(seed), "rest_visits": visits_this_run,
                         "upgrades_from_rest": upgrades_from_rest,
                         "upgraded_in_final_deck": sum(1 for card in deck if card < 0),
                         "terminal_floor": int(final["floor"])})
        env.close()

    offered_upgrade = [visit for visit in visits if visit["legal"].get("upgrade")]
    took_upgrade = [visit for visit in offered_upgrade if visit["chosen"] == REUPGRADE]
    realised = sum(1 for visit in visits if (visit.get("upgrade_delta") or 0) > 0)
    total_upgrades = sum(row["upgraded_in_final_deck"] for row in per_seed)
    payload = {
        "aggregates": {
            "episodes": len(per_seed),
            "rest_visits": len(visits),
            "rest_visits_per_episode": round(len(visits) / len(per_seed), 4),
            "visits_where_upgrade_advertised": len(offered_upgrade),
            "share_of_advertised_upgrades_taken": (
                round(len(took_upgrade) / len(offered_upgrade), 4) if offered_upgrade else None),
            "chosen_option_counts": dict(collections.Counter(
                str(visit["chosen"]) for visit in visits)),
            "upgrade_refused_for_lack_of_upgradable_card": sum(
                1 for visit in visits if not visit["legal"].get("upgrade")),
            "rest_visits_realising_an_upgrade": realised,
            "mean_hp_delta_where_heal_chosen": (statistics.fmean(
                [visit["hp_delta"] for visit in visits
                 if visit["chosen"] == REHEAL and visit.get("hp_delta") is not None])
                if any(visit["chosen"] == REHEAL and visit.get("hp_delta") is not None
                       for visit in visits) else None),
            "upgraded_cards_total": total_upgrades,
            "upgrades_from_rest_total": sum(row["upgrades_from_rest"] for row in per_seed),
            "mean_upgraded_in_final_deck": round(statistics.fmean(
                [row["upgraded_in_final_deck"] for row in per_seed]), 3),
            "episodes_never_reaching_a_campfire": sum(
                1 for row in per_seed if row["rest_visits"] == 0),
        },
        "checkpoint_sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "not_established": [
            "that the campfire is the only other upgrade source: relic pickups also promote cards "
            "(RunNonCombatEffects.cs:99-104 RelicPomander / RelicNeowsTalisman) and are not "
            "separated here",
            "that upgrading more would win more: this counts the choice, not a counterfactual run",
            "what happens inside transform_select beyond the net deck delta -- which card was "
            "promoted is not attributed"],
        "per_seed": per_seed,
        "reproduce": ("../third_party/slay-the-spire-2-emulator-main/.venv/Scripts/python.exe "
                      "scripts/measure_rest_site_choice.py --checkpoint <zip> --limit "
                      f"{args.limit} --start-offset {args.start_offset} --out <file.json>"),
        "scope": ("simulator_act1 label, argmax, one checkpoint, "
                  f"{len(per_seed)} seeds; campfire options advertised and the one taken"),
        "seed_window": {"offset": args.start_offset, "limit": args.limit,
                        "stage": args.stage, "split": args.split},
        "visits": visits,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")
    agg = payload["aggregates"]
    print(f"{agg['episodes']} episodes, {agg['rest_visits']} campfire visits "
          f"({agg['rest_visits_per_episode']} per episode), "
          f"{agg['episodes_never_reaching_a_campfire']} runs never saw one")
    print(f"  upgrade advertised in {agg['visits_where_upgrade_advertised']} visits, taken in "
          f"{len(took_upgrade)} -> share {agg['share_of_advertised_upgrades_taken']}; "
          f"options chosen {agg['chosen_option_counts']}")
    print(f"  visits realising an upgrade: {agg['rest_visits_realising_an_upgrade']}; "
          f"upgraded cards total {agg['upgraded_cards_total']} "
          f"(mean {agg['mean_upgraded_in_final_deck']} per episode)")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
