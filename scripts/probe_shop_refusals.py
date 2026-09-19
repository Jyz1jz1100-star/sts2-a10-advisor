"""Classify every shop/event mask-vs-engine refusal against the engine's own predicates.

``census_rejection_phases.py`` established that all refusals land in ``shop`` (641/2,866)
and ``event`` (112/4,305) and nowhere else, and left "why" open.  The candidates are
enumerable, because the emulator's shop step is one flat function of the state it just
advertised a mask for:

* card   (base 0-6)   refuses on ``ShopCards[base] == 0 || Gold < ShopCosts[base]``
  (RunEngine.cs:1836-1844)
* relic  (base 7-9)   refuses on ``ShopRelics[i] == 0 || Gold < ShopCosts[base]``
  (RunEngine.cs:1849-1858)
* potion (base 10-12) refuses on ``ShopPotions[i] == 0 || Gold < cost || !AddPotion(...)``
  (RunEngine.cs:1868-1880), and the mask's ``hasPotionSlot`` tests *any* of the three
  slots while ``AddPotion`` only fills ``min(2, len)`` of them
  (RunEngine.cs:727 vs RunRewardGenerator.cs:1015)
* remove (base 13)    refuses on ``Gold < ShopCosts[13] || Deck.Count <= 1``
  (RunEngine.cs:1885-1891)
* skip   (base 14)    cannot refuse: ``AdvanceAfterNode`` always returns 0
  (RunEngine.cs:1983-1998)
* base 15-31          falls through to an unconditional ``return -1`` (RunEngine.cs:1897)

So each refusal is either explained by one of those branches, or it is a mask/step
disagreement inside the emulator, or -- the branch worth distinguishing first -- the
refused base was never in the *native* mask and the disagreement is in this repo's flat
expansion rather than in the engine.  This script re-evaluates the predicates from the
state it observed, before the step, and reports which of the three it is.
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
from training.v2_flat_env import SENTINEL_FLAT, TARGET_SLOTS  # noqa: E402

# Offsets inside the native run block, from RunEngine.WriteRunObservation (RunEngine.cs:826-858).
O_PHASE, O_FLOOR, O_DECK, O_GOLD, O_NODE = 0, 1, 3, 4, 8
O_SHOP_CARD_0, O_RELIC_0, O_POTION_0, O_REMOVE_COST, O_EVENT = 20, 28, 31, 34, 24
O_SLOT_0 = 25

SHOP_LABELS = (["buy_card_%d" % i for i in range(7)] + ["buy_relic_%d" % i for i in range(3)]
               + ["buy_potion_%d" % i for i in range(3)] + ["remove_card", "skip"]
               + [f"unhandled_base_{i}" for i in range(15, 32)])


def classify(phase: str, base: int, gold: int, deck: int, cards: list[int], costs: list[int],
             relics: list[int], potions: list[int], slots: list[int]) -> str:
    """Which engine branch refuses this action, evaluated from the observed state."""
    if phase == "event":
        return "event_option_invalid_for_this_event"
    cost = costs[base] if base < len(costs) else None
    if base <= 6:
        if cards[base] == 0:
            return "card_slot_empty"
        return "unaffordable" if gold < cost else "PREDICATE_SAYS_LEGAL"
    if 7 <= base <= 9:
        if relics[base - 7] == 0:
            return "relic_slot_empty"
        return "unaffordable" if gold < cost else "PREDICATE_SAYS_LEGAL"
    if 10 <= base <= 12:
        if potions[base - 10] == 0:
            return "potion_slot_empty"
        if gold < cost:
            return "unaffordable"
        # AddPotion fills only the first min(2, len(PotionSlots)) entries.
        return ("potion_capacity_mask_mismatch" if all(s != 0 for s in slots[:2])
                else "PREDICATE_SAYS_LEGAL")
    if base == 13:
        if deck <= 1:
            return "remove_deck_too_small"
        return "unaffordable" if gold < costs[13] else "PREDICATE_SAYS_LEGAL"
    if base == 14:
        return "skip_cannot_refuse"
    return "base_outside_shop_handler"


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
    flat_mask_wider_than_native = 0
    executed_not_in_native_mask = 0
    decisions = 0
    episodes = 0

    for seed in window:
        env = factory(int(seed))
        observation, info = env.reset(seed=int(seed))
        terminal = truncated = False
        steps = 0
        while not (terminal or truncated):
            flat_mask = env.action_masks()
            if not any(bool(v) for v in flat_mask):
                break
            core = env.unwrapped
            raw = core.raw_observation()
            native = [int(b) for b in core.base_action_mask()]
            run = raw[COMBAT_OBS_SIZE:]
            pre_phase = core.state_info().get("phase_name")
            decisions += 1
            advertised_bases = {int(i) // TARGET_SLOTS for i, v in enumerate(flat_mask)
                                if v and int(i) != SENTINEL_FLAT}
            if any(not native[b] for b in advertised_bases if b < len(native)):
                flat_mask_wider_than_native += 1
            raw_action, _ = model.predict(observation, action_masks=flat_mask,
                                           deterministic=True)
            action = int(raw_action.item() if hasattr(raw_action, "item") else raw_action)
            _obs, _reward, terminal, truncated, info = env.step(action)
            steps += 1
            if info.get("native_rejection_filtered"):
                base = action // TARGET_SLOTS
                # V2FlatActionEnv has no state_lists passthrough; the native core does.
                lists = core._core.state_lists()
                costs = list(lists["shop_costs"])
                cards = list(lists["shop_cards"])
                row = {
                    "seed": int(seed), "step": steps, "phase": pre_phase,
                    "floor": int(run[O_FLOOR]), "refused_flat": action, "refused_base": base,
                    # Bases mean different things per phase: base 0 is a card buy in a shop
                    # and option 0 of the event.  Labelling both from the shop table would
                    # invent "buy_card_0" rows that are really event options.
                    "label": (f"event_option_{base}" if pre_phase == "event"
                              else SHOP_LABELS[base] if base < len(SHOP_LABELS)
                              else f"base_{base}"),
                    "in_native_mask": bool(native[base]) if base < len(native) else None,
                    "native_ons": [i for i, v in enumerate(native) if v],
                    "gold": int(run[O_GOLD]), "deck": int(run[O_DECK]),
                    "event_id": int(run[O_EVENT]),
                    "shop_costs": costs, "shop_cards": cards,
                    "shop_relics": [int(run[O_RELIC_0 + i]) for i in range(3)],
                    "shop_potions": [int(run[O_POTION_0 + i]) for i in range(3)],
                    "potion_slots": [int(run[O_SLOT_0 + i]) for i in range(3)],
                }
                row["because"] = classify(pre_phase, base, row["gold"], row["deck"], cards,
                                          costs, row["shop_relics"], row["shop_potions"],
                                          row["potion_slots"])
                rows.append(row)
            else:
                base = action // TARGET_SLOTS
                if base < len(native) and not native[base]:
                    executed_not_in_native_mask += 1
            observation = _obs
            if steps >= args.max_steps:
                truncated = True
        env.close()
        episodes += 1

    reasons = collections.Counter(r["because"] for r in rows)
    labels = collections.Counter(r["label"] for r in rows)
    by_phase = collections.Counter(r["phase"] for r in rows)
    payload = {
        "_comment": [
            "Every refusal the filter mode absorbs, classified by the engine's own branch.",
            "PREDICATE_SAYS_LEGAL means the state as observed satisfies the mask and the step",
            "predicate alike, so the -1 came from something not visible in this repo's view of",
            "the state; skip_cannot_refuse and base_outside_shop_handler name the two extremes.",
            "flat_mask_advertised_a_base_the_native_mask_has_off counts wrapper-vs-engine drift",
            "on every decision, refused or not.",
        ],
        "aggregates": {
            "decisions": decisions,
            "episodes": episodes,
            "by_phase": dict(by_phase),
            "flat_mask_advertised_a_base_the_native_mask_has_off": flat_mask_wider_than_native,
            "executed_actions_outside_the_native_mask": executed_not_in_native_mask,
            "labels": dict(labels),
            "native_mask_advertised_the_refused_base": sum(
                1 for r in rows if r["in_native_mask"]),
            "reasons": dict(reasons),
            "refusals": len(rows),
            "refusals_explained_by_a_predicate": sum(
                v for k, v in reasons.items() if k != "PREDICATE_SAYS_LEGAL"),
        },
        "checkpoint": checkpoint.name,
        "checkpoint_sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "rows": rows,
        "scope": "simulator_act1 label, mixed-act population, argmax",
        "seed_window": {"offset": args.start_offset, "limit": args.limit,
                        "stage": args.stage, "split": args.split},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(f"{episodes} episodes, {decisions} decisions, {len(rows)} refusals")
    print("reasons:", dict(reasons))
    print("labels:", dict(labels))
    print("event (event_id, base) pairs:",
          dict(collections.Counter((r["event_id"], r["refused_base"]) for r in rows
                                   if r["phase"] == "event")))
    print("native mask advertised the refused base in",
          sum(1 for r in rows if r["in_native_mask"]), "of", len(rows), "refusals")
    print(f"flat mask wider than native on {flat_mask_wider_than_native} decisions; "
          f"{executed_not_in_native_mask} executed actions outside the native mask")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
