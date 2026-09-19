"""Attribute the unexplained half of the upgrades: does picking up a relic promote a card?

The source-accounting thread left a number on the table: 1,140 upgraded cards across 3,500 runs,
of which events explain 512 and the campfire 29 -- **599 unattributed**. The one candidate already
read out of the source is relic pickup effects: ``RunNonCombatEffects.cs:99-104`` upgrades the
first held card for ``RelicPomander`` and one matching Basher/Strike for ``RelicNeowsTalisman``,
fired from ``ApplyRelicPickup``. This script measures that instead of asserting it, evaluation-only
under the 2026-09-02 PPO freeze.

Method, and why the read points are where they are:

* one record per ``relic_reward`` decision (``StepRelicReward``: action 0 takes the relic,
  ``RewardSkipAction = 3`` skips, 4..6 drop a potion), holding the relic the screen offers
  (``state_info()["relic_reward"]``), the relic set and upgraded-card count before, and the option
  taken;
* the outcome is read when the run leaves ``relic_reward``/``card_reward``, because the pickup
  effect fires on the step that grants the relic while the screen flow can still continue;
* the upgrade delta is attributed to the relic id that newly appears in the relic set, so the
  artifact names *which* relic promoted the card rather than only counting promotions;
* cross-check on entry to the screen: the offered relic id must equal the id that later shows up
  when the policy takes it, otherwise the attribution is pointing at the wrong object.

Pre-registered bookkeeping: this file plus the event and campfire censuses must be summed by the
claim over the same 3,500 seeds and one checkpoint, and **whatever remains unattributed has to be
printed as a number**. A residual that shrinks to near zero confirms the relic hypothesis; one that
stays large means another source is still unnamed, and that is the finding, not a failure.
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

RELIC_TAKE = 0
RELIC_SKIP = 3  # RunConstants.RewardSkipAction
RELIC_PHASES = {"relic_reward", "card_reward"}


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

    pickups: list[dict] = []
    per_seed: list[dict] = []
    for seed in window:
        env = factory(int(seed))
        observation, info = env.reset(seed=int(seed))
        terminal = truncated = False
        steps = taken_upgrades = screens_this_run = 0
        pending: dict | None = None
        core = env.unwrapped
        while not (terminal or truncated):
            mask = env.action_masks()
            if not any(bool(value) for value in mask):
                break
            state = core.state_info()
            phase = state.get("phase_name")
            relics = {int(relic) for relic in core._core.state_lists()["relics"]}
            upgraded = sum(1 for card in core._core.state_lists()["deck"] if card < 0)
            if pending is not None and phase not in RELIC_PHASES:
                gained = sorted(relics - set(pending["relics_before"]))
                pending.update(new_relics=gained,
                               upgrade_delta=upgraded - pending["up_before"],
                               resolved=True)
                if pending["upgrade_delta"]:
                    taken_upgrades += int(pending["upgrade_delta"])
                pickups.append(pending)
                screens_this_run += 1
                pending = None
            if phase == "relic_reward" and pending is None:
                pending = {"seed": int(seed), "floor": int(state["floor"]),
                           "offered_relic": int(state["relic_reward"]),
                           "relics_before": sorted(relics), "up_before": upgraded,
                           "deck_size": int(state["deck_size"]), "chosen": None}
            raw, _ = model.predict(observation, action_masks=mask, deterministic=True)
            action = int(raw.item() if hasattr(raw, "item") else raw)
            if pending is not None and phase == "relic_reward" and pending["chosen"] is None:
                pending["chosen"] = action // TARGET_SLOTS
            _obs, _r, terminal, truncated, info = env.step(action)
            observation = _obs
            steps += 1
            if steps >= args.max_steps:
                truncated = True
        if pending is not None:
            pending.update(new_relics=[], upgrade_delta=None, resolved=False)
            pickups.append(pending)
            screens_this_run += 1
        deck = [int(card) for card in core._core.state_lists()["deck"]]
        per_seed.append({"seed": int(seed),
                         "relic_screens": screens_this_run,
                         "upgrades_from_relic_pickup": taken_upgrades,
                         "upgraded_in_final_deck": sum(1 for card in deck if card < 0)})
        env.close()

    took = [pick for pick in pickups if pick["chosen"] == RELIC_TAKE]
    skipped = [pick for pick in pickups if pick["chosen"] == RELIC_SKIP]
    attributed = sum(1 for pick in pickups if (pick.get("upgrade_delta") or 0) > 0)
    per_relic = collections.Counter(
        relic for pick in pickups for relic in pick.get("new_relics", [])
        if (pick.get("upgrade_delta") or 0) > 0)
    payload = {
        "aggregates": {
            "episodes": len(per_seed),
            "relic_reward_decisions": len(pickups),
            "relics_taken": len(took),
            "relics_skipped": len(skipped),
            "screens_that_realised_an_upgrade": attributed,
            "upgrades_from_relic_pickup_total": sum(
                max(0, pick.get("upgrade_delta") or 0) for pick in pickups),
            "relics_that_promoted_a_card": dict(per_relic.most_common()),
            "offer_matches_acquired_relic_when_taken": sum(
                1 for pick in took
                if pick["offered_relic"] in (pick.get("new_relics") or [])),
            "taken_offers": len(took),
            "mean_upgraded_in_final_deck": round(statistics.fmean(
                [row["upgraded_in_final_deck"] for row in per_seed]), 3),
        },
        "checkpoint_sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "not_established": [
            "that relic *passive* effects never promote cards outside the pickup step; only the "
            "pickup transition is attributed here",
            "anything about the real game's relic pool -- only the emulator source is in evidence"],
        "per_seed": per_seed,
        "pickups": pickups,
        "reproduce": ("../third_party/slay-the-spire-2-emulator-main/.venv/Scripts/python.exe "
                      "scripts/measure_relic_pickup_upgrades.py --checkpoint <zip> --limit "
                      f"{args.limit} --start-offset {args.start_offset} --out <file.json>"),
        "scope": ("simulator_act1 label, argmax, one checkpoint, "
                  f"{len(per_seed)} seeds; relic-reward screens and the deck delta they cause"),
        "seed_window": {"offset": args.start_offset, "limit": args.limit,
                        "stage": args.stage, "split": args.split},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")
    agg = payload["aggregates"]
    print(f"{agg['episodes']} episodes, {agg['relic_reward_decisions']} relic screens, "
          f"{agg['relics_taken']} taken, {agg['relics_skipped']} skipped")
    print(f"  screens realising an upgrade: {agg['screens_that_realised_an_upgrade']}; "
          f"upgrades from pickup: {agg['upgrades_from_relic_pickup_total']}; "
          f"offer matched the acquired relic in "
          f"{agg['offer_matches_acquired_relic_when_taken']}/{agg['taken_offers']}")
    print(f"  relics that promoted a card: {agg['relics_that_promoted_a_card']}")
    print(f"  mean final-deck upgrades: {agg['mean_upgraded_in_final_deck']}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
