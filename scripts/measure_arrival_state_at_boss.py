"""Does the Act-1 boss deficit trace to what the run arrives with, or to the fight?

The campaign report compares Act 1 (0-3 wins per arrival set) with Act 2 (25 wins over the
same 3,500 seeds, same checkpoint) and locates the difference in the boss fight. That
comparison silently assumes the two acts' arrivals are comparable runs. The obvious
confound is maturity: a policy that reaches Act 2 has survived more floors, so it may have
bought more cards, picked up more relics and grown max HP -- and then "Act 2 is easier"
would be an artifact of arriving better resourced, not of the fight being softer.

This script measures that assumption rather than arguing with it. It also exploits a
power problem the Act-1 side cannot solve: Act 1 has almost no wins to condition on, while
Act 2 has enough to ask "does any arrival resource predict winning at all?" inside one act.

Pre-registered before any shard ran:

* population/checkpoint: the act1 promotion partition, first 3,500 seeds, the campaign
  checkpoint -- chosen so the arrival and clear counts must reproduce
  ``docs/evidence/potion_slot_cost_20260919.json`` (83 Act-1 + 79 Act-2 arrivals, 25 clears,
  all Act-2). **If that reproduction fails, the instrumentation is wrong, not the census**;
* arrival state is read at the *first* floor-17 boss decision: ``deck_size``,
  ``relic_count``, ``gold``, ``player_hp``/``player_max_hp``, usable potions (slots[:2], the
  capacity the engine implements -- slot 3 is advertised but unfillable), shops visited;
* primary cross-act test: Act-1 minus Act-2 mean for each resource, with a permutation p and
  a bootstrap CI. A positive, CI-excluding-zero difference revives the confound; a tight CI
  around zero retires it;
* primary predictive test, run *within* Act 2 because that is where wins exist: winners vs
  losers on the same resources, same interval machinery, pooled across the 7 shards. Zero
  here means arrival resources do not predict boss victory, so Act 1's deficit is not
  "arrived weaker" and the transferable lever is in-fight survival;
* partial coverage only, and recorded as such: deck *quality* is measured as upgrade count and
  distinct card definitions, which come from the signed deck list (``CombatFactory.cs:243`` --
  a negative id is that card upgraded). It is not measured as per-card power: no card table is
  joined here, so "two more upgrades" is a strength signal, not a damage calculation;
* unreadable outcome: fewer than 20 arrivals in an act, or fewer than 10 Act-2 wins, is
  reported as descriptive only with no directional claim.
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

from training.v2_constants import COMBAT_OBS_SIZE, NODE_BOSS, PHASE_COMPLETE  # noqa: E402

ACT_NAMES = {1: "act1_overgrowth", 2: "act2_underdocks"}
BOSS_FLOOR = 17
# The engine advertises three potion slots but fills only two (RunRewardGenerator.cs:1015), so
# "usable potions" is slots[:2]; slot 3 is read from the same run block, offset +25..+27.
POTION_SLOT_0 = 25
# Deck ids come back signed, not bare: CombatFactory.cs:243 builds `new CardInstance(Math.Abs(id),
# id < 0)`, so a negative entry is that card *upgraded*. That is what makes deck quality
# measurable at all -- deck_size alone is a count, and an 18-card deck can be 0 or 18 upgrades.
RESOURCES = ("deck_size", "upgraded_in_deck", "distinct_card_defs", "relic_count", "gold",
             "player_hp", "player_max_hp", "potions_at_boss", "shops_visited")


def fmean_or_none(values):
    return round(statistics.fmean(values), 3) if values else None


def bootstrap_mean_ci(samples, reps=4000, seed=20260919):
    if not samples:
        return []
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(samples, k=len(samples))) for _ in range(reps))
    return [round(means[int(0.025 * reps)], 3), round(means[int(0.975 * reps) - 1], 3)]


def two_sample(x, y, reps=20000, seed=20260919):
    """Difference of means with a permutation p and a bootstrap CI, both reproducible."""
    if not x or not y:
        return {"diff": None, "permutation_p": None, "bootstrap_ci_95": [], "n_x": len(x),
                "n_y": len(y)}
    obs = statistics.fmean(x) - statistics.fmean(y)
    rng = random.Random(seed)
    pool = list(x + y)
    as_bad = 0
    for _ in range(reps):
        rng.shuffle(pool)
        if abs(statistics.fmean(pool[:len(x)]) - statistics.fmean(pool[len(x):])) >= abs(obs) - 1e-12:
            as_bad += 1
    ci = sorted(statistics.fmean(rng.choices(x, k=len(x))) - statistics.fmean(rng.choices(y, k=len(y)))
                for _ in range(4000))
    return {"diff": round(obs, 3),
            "permutation_p": round(as_bad / reps, 4),
            "bootstrap_ci_95": [round(ci[int(0.025 * 4000)], 3), round(ci[int(0.975 * 4000) - 1], 3)],
            "n_x": len(x), "n_y": len(y)}


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
    open_stage = dataclasses.replace(stage, max_floor=None, max_episode_steps=args.max_steps)
    factory = _environment_factory(config, open_stage, sts2_gym)
    checkpoint = args.checkpoint.resolve()
    probe = DummyVecEnv([lambda: factory(window[0])])
    model = MaskablePPO.load(str(checkpoint), env=probe, device="cpu")
    probe.close()

    rows: list[dict] = []
    for seed in window:
        env = factory(int(seed))
        observation, info = env.reset(seed=int(seed))
        terminal = truncated = False
        steps = boss_steps = seen_at_boss = 0
        arrival: dict = {}
        shops = 0
        seen_shop_floors: set[int] = set()
        while not (terminal or truncated):
            mask = env.action_masks()
            if not any(bool(value) for value in mask):
                break
            state = env.unwrapped.state_info()
            floor, node = int(state["floor"]), int(state["current_node_type"])
            if state.get("phase_name") == "shop" and floor not in seen_shop_floors:
                seen_shop_floors.add(floor)
                shops += 1
            if not arrival and floor == BOSS_FLOOR and node == NODE_BOSS:
                run_block = env.unwrapped.raw_observation()[COMBAT_OBS_SIZE:]
                deck = [int(card) for card in env.unwrapped._core.state_lists()["deck"]]
                arrival = {"act": int(state["act"]), "floor": floor,
                           "encounter_id": int(state["encounter_id"]),
                           "deck_size": int(state["deck_size"]),
                           "upgraded_in_deck": sum(1 for card in deck if card < 0),
                           "distinct_card_defs": len({abs(card) for card in deck}),
                           "relic_count": int(state["relic_count"]),
                           "gold": int(state["gold"]),
                           "player_hp": int(state["player_hp"]),
                           "player_max_hp": int(state["player_max_hp"]),
                           "potions_at_boss": sum(
                               1 for slot in range(2)
                               if int(run_block[POTION_SLOT_0 + slot]) != 0)}
            raw, _ = model.predict(observation, action_masks=mask, deterministic=True)
            action = int(raw.item() if hasattr(raw, "item") else raw)
            _obs, _r, terminal, truncated, info = env.step(action)
            observation = _obs
            steps += 1
            if floor == BOSS_FLOOR and node == NODE_BOSS:
                boss_steps += 1
            if steps >= args.max_steps:
                truncated = True
            seen_at_boss = boss_steps
        final = env.unwrapped.state_info()
        rows.append({"seed": int(seed), **arrival, "arrived": bool(arrival),
                     "shops_visited": shops, "boss_decisions": seen_at_boss,
                     "steps": steps, "terminal_floor": int(final["floor"]),
                     # RunPhase.Complete == 6 is also written on a loss (RunEngine.cs:1286-1293),
                     # so a clear is this field AND the last-combat flag, never this field alone.
                     "terminal_engine_phase": int(
                         env.unwrapped.raw_observation()[COMBAT_OBS_SIZE]),
                     "player_won": bool(final.get("player_won"))})
        env.close()

    arrivals = [row for row in rows if row["arrived"]]
    for row in arrivals:
        row["cleared"] = (row["terminal_engine_phase"] == PHASE_COMPLETE and row["player_won"])
    payload = {
        "aggregates": {
            "episodes": len(rows),
            "arrivals": len(arrivals),
            "arrivals_by_generated_act": dict(collections.Counter(
                ACT_NAMES.get(row["act"], str(row["act"])) for row in arrivals)),
            "cleared": sum(1 for row in arrivals if row["cleared"]),
            "cleared_by_generated_act": dict(collections.Counter(
                ACT_NAMES.get(row["act"], str(row["act"])) for row in arrivals if row["cleared"])),
            "encounters_by_generated_act": {
                ACT_NAMES.get(act, str(act)): dict(collections.Counter(
                    row["encounter_id"] for row in arrivals if row["act"] == act))
                for act in (1, 2)},
            "max_hp_change_beyond_floor17": sum(
                1 for row in arrivals if row["terminal_floor"] > BOSS_FLOOR),
        },
        "cross_act_arrival_state": {
            resource: two_sample([row[resource] for row in arrivals if row["act"] == 1],
                                 [row[resource] for row in arrivals if row["act"] == 2])
            for resource in RESOURCES},
        "within_act2_win_vs_loss": {
            resource: two_sample([row[resource] for row in arrivals
                                  if row["act"] == 2 and row["cleared"]],
                                 [row[resource] for row in arrivals
                                  if row["act"] == 2 and not row["cleared"]])
            for resource in RESOURCES},
        "act1_by_outcome": {
            ACT_NAMES[1] + "|win" if won else ACT_NAMES[1] + "|loss": {
                "n": len(sample),
                **{resource: fmean_or_none([row[resource] for row in sample])
                   for resource in RESOURCES},
                "boss_decisions_median": (statistics.median(
                    [row["boss_decisions"] for row in sample]) if sample else None)}
            for won in (True, False)
            for sample in [[row for row in arrivals if row["act"] == 1 and row["cleared"] == won]]},
        "act2_by_outcome": {
            ACT_NAMES[2] + "|win" if won else ACT_NAMES[2] + "|loss": {
                "n": len(sample),
                **{resource: fmean_or_none([row[resource] for row in sample])
                   for resource in RESOURCES},
                "boss_decisions_median": (statistics.median(
                    [row["boss_decisions"] for row in sample]) if sample else None),
                "bootstrap_ci_95_relic_count": bootstrap_mean_ci(
                    [row["relic_count"] for row in sample])}
            for won in (True, False)
            for sample in [[row for row in arrivals if row["act"] == 2 and row["cleared"] == won]]},
        "readable": {
            "act1_arrivals": sum(1 for row in arrivals if row["act"] == 1),
            "act2_arrivals": sum(1 for row in arrivals if row["act"] == 2),
            "act2_wins": sum(1 for row in arrivals if row["act"] == 2 and row["cleared"]),
            "threshold_is_20_arrivals_per_act_and_10_act2_wins": None,
        },
        "checkpoint_sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "reproduce": ("../third_party/slay-the-spire-2-emulator-main/.venv/Scripts/python.exe "
                      "scripts/measure_arrival_state_at_boss.py --checkpoint <zip> --limit 500 "
                      "--start-offset <0..3000 step 500> --out <file.json>"),
        "rows": rows,
        "scope": ("simulator_act1 label, mixed generated acts, argmax, one checkpoint, "
                  "3,500 seeds; arrival quantities plus deck upgrade count and distinctness, "
                  "no per-card power"),
        "seed_window": {"offset": args.start_offset, "limit": args.limit,
                        "stage": args.stage, "split": args.split},
    }
    payload["readable"]["threshold_is_20_arrivals_per_act_and_10_act2_wins"] = bool(
        payload["readable"]["act1_arrivals"] >= 20 and payload["readable"]["act2_arrivals"] >= 20
        and payload["readable"]["act2_wins"] >= 10)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(f"{len(rows)} episodes, {len(arrivals)} arrivals "
          f"{payload['aggregates']['arrivals_by_generated_act']}, "
          f"{payload['aggregates']['cleared']} cleared "
          f"{payload['aggregates']['cleared_by_generated_act']}")
    for resource, block in payload["cross_act_arrival_state"].items():
        print(f"  act1-act2 {resource:16} diff={block['diff']} ci={block['bootstrap_ci_95']} "
              f"p={block['permutation_p']}")
    for resource, block in payload["within_act2_win_vs_loss"].items():
        print(f"  act2 win-loss {resource:16} diff={block['diff']} ci={block['bootstrap_ci_95']} "
              f"p={block['permutation_p']}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
