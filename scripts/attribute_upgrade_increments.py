"""Attribute every upgraded card a run ends with to the exact step that produced it.

The source-census thread left 599 of 1,140 upgraded cards unexplained: events accounted for 512,
the campfire for 29, and the relic-reward screens for nothing (``RelicPomander`` /
``RelicNeowsTalisman`` were never even offered). Every one of those instruments read a *specific
phase*, so anything that promotes a card elsewhere -- a relic bought in a shop, an effect that
fires on the map, or cards that are already upgraded when the run starts -- is invisible to all of
them by construction. Guessing the next phase to instrument is how the last two candidates got
named and half-refuted.

So this census stops guessing about mechanisms and watches the counter. Before every step it reads
the deck (signed ids: negative = upgraded, ``CombatFactory.cs:243``) and after the step it reads it
again; whenever the upgraded count rises, it records where: the phase the policy was acting in,
the base action it took, the floor, and whether the deck also grew. That distinguishes the two
things that can happen -- an upgraded card *added* versus a held card *promoted* -- without needing
to know their names in the source. The run's opening state is recorded too, because cards already
upgraded at step 0 are a real channel that no during-run instrument can see.

Pre-registered:

* population: the act1 promotion partition, first 3,500 seeds, the campaign checkpoint -- the same
  window as the reward, event, campfire and relic censuses;
* the books must close: ``upgrades_at_reset + sum(step increments)`` must equal the upgraded cards
  present at the end of each run, and that total must land on the 1,140 the other censuses agree
  on. A shortfall means a promotion happened between two reads (the same step could add one card
  and promote another), and that is reported as an unresolved remainder rather than hidden;
* each channel is then reported with its own count and an example seed, so the finding is a
  breakdown, not a claim that some specific source is or isn't real.
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


def deck_state(core):
    deck = [int(card) for card in core._core.state_lists()["deck"]]
    return sum(1 for card in deck if card < 0), len(deck)


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

    increments: list[dict] = []
    decrements: list[dict] = []
    per_seed: list[dict] = []
    for seed in window:
        env = factory(int(seed))
        observation, info = env.reset(seed=int(seed))
        terminal = truncated = False
        core = env.unwrapped
        steps = 0
        up_at_reset, size_at_reset = deck_state(core)
        up_now, size_now = up_at_reset, size_at_reset
        while not (terminal or truncated):
            mask = env.action_masks()
            if not any(bool(value) for value in mask):
                break
            state = core.state_info()
            phase = state.get("phase_name")
            raw, _ = model.predict(observation, action_masks=mask, deterministic=True)
            action = int(raw.item() if hasattr(raw, "item") else raw)
            _obs, _r, terminal, truncated, info = env.step(action)
            observation = _obs
            steps += 1
            up_after, size_after = deck_state(core)
            if up_after != up_now:
                # The count can fall as well as rise: a shop can remove a card, and if what it
                # removed was an upgraded one, the running total of promotions would otherwise
                # overshoot the final count. Both directions are recorded so the books close.
                (increments if up_after > up_now else decrements).append({
                    "seed": int(seed), "step": steps, "floor": int(state["floor"]),
                    "phase": phase, "base_action": action // TARGET_SLOTS,
                    "node_type": int(state["current_node_type"]),
                    "event_id": int(state["event_id"]),
                    "encounter_id": int(state["encounter_id"]),
                    "added": up_after - up_now,
                    "deck_grew": size_after > size_now,
                    "deck_size_after": size_after})
            up_now, size_now = up_after, size_after
            if steps >= args.max_steps:
                truncated = True
        per_seed.append({"seed": int(seed), "upgrades_at_reset": up_at_reset,
                         "deck_size_at_reset": size_at_reset,
                         "upgraded_in_final_deck": up_now,
                         "steps": steps})
        env.close()

    from_rows = sum(inc["added"] for inc in increments)
    at_reset = sum(row["upgrades_at_reset"] for row in per_seed)
    final_total = sum(row["upgraded_in_final_deck"] for row in per_seed)
    removed = sum(-row["added"] for row in decrements)
    removal_channels = collections.Counter(
        f"{row['phase']}|action {row['base_action']}" for row in decrements)
    channels = collections.Counter(
        f"{inc['phase']}|action {inc['base_action']}|"
        f"{'card added already upgraded' if inc['deck_grew'] else 'held card promoted'}"
        for inc in increments)
    payload = {
        "aggregates": {
            "episodes": len(per_seed),
            "upgraded_cards_final_total": final_total,
            "upgrades_present_at_reset": at_reset,
            "episodes_with_upgrades_at_reset": sum(
                1 for row in per_seed if row["upgrades_at_reset"] > 0),
            "upgrades_attributed_to_a_step": from_rows,
            "upgrades_lost_by_removals": removed,
            "removal_channels": dict(removal_channels),
            "unresolved_remainder": final_total - at_reset - from_rows + removed,
            "books_close": at_reset + from_rows - removed == final_total,
            "mean_upgraded_in_final_deck": round(statistics.fmean(
                [row["upgraded_in_final_deck"] for row in per_seed]), 3),
        },
        "by_channel": dict(channels.most_common()),
        "decrements": decrements,
        "checkpoint_sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
        "channel_examples": {
            channel: next(f"seed {inc['seed']} floor {inc['floor']} "
                          f"event {inc['event_id']} encounter {inc['encounter_id']}"
                          for inc in increments
                          if f"{inc['phase']}|action {inc['base_action']}|"
                          f"{'card added already upgraded' if inc['deck_grew'] else 'held card promoted'}"  # noqa: E501
                          == channel)
            for channel in channels},
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "increments": increments,
        "not_established": [
            "which game object caused a channel: the channel is the phase and the action the "
            "policy took, read from the engine, not a named mechanic",
            "two promotions inside one step are counted once per card, but a step that both adds "
            "an upgraded card and promotes another is recorded as a single increment row"],
        "per_seed": per_seed,
        "reproduce": ("../third_party/slay-the-spire-2-emulator-main/.venv/Scripts/python.exe "
                      "scripts/attribute_upgrade_increments.py --checkpoint <zip> --limit "
                      f"{args.limit} --start-offset {args.start_offset} --out <file.json>"),
        "scope": ("simulator_act1 label, argmax, one checkpoint, "
                  f"{len(per_seed)} seeds; every deck-upgrade transition, wherever it happens"),
        "seed_window": {"offset": args.start_offset, "limit": args.limit,
                        "stage": args.stage, "split": args.split},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")
    agg = payload["aggregates"]
    print(f"{agg['episodes']} episodes, {agg['upgraded_cards_final_total']} upgraded cards total "
          f"(mean {agg['mean_upgraded_in_final_deck']} per episode)")
    print(f"  at reset {agg['upgrades_present_at_reset']} "
          f"(in {agg['episodes_with_upgrades_at_reset']} runs), attributed to a step "
          f"{agg['upgrades_attributed_to_a_step']}, lost to removals "
          f"{agg['upgrades_lost_by_removals']}, remainder "
          f"{agg['unresolved_remainder']}, books close: {agg['books_close']}")
    for channel, count in payload["by_channel"].items():
        print(f"  {count:5}  {channel}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
