"""The run starts with a relic choice, and only 8.7%-looking slices of runs pick the relic that upgrades cards.

``attribute_upgrade_increments.py`` closed the books and named the biggest missing channel: 463 of
1,140 upgraded cards appear on a step whose phase is ``ancient``. The source says why --
``ApplyAncientChoice(relicId)`` is not a flavour screen, it is ``ApplyRelicPickup`` for one of three
relics offered at run start, and for the upgrade relics that call returns
``RunFollowUp.TransformSelect``. So the opening decision is a strength decision, and it is exactly
the kind of lever the campfire turned out to be: fully available, fully policy-controlled, and
previously uncounted. (It also refines the relic census's negative: that instrument watched
``relic_reward`` screens, where these relics are never offered, while the opening screen hands them
out through a different phase.)

What this census measures, evaluation-only under the 2026-09-02 PPO freeze:

* every run's opening screen: the three relic ids actually offered
  (``state_lists()["neow_options"]``), which of them the native mask allowed, and which one the
  policy took (or that it skipped);
* the outcome of that choice, read once the run leaves ``ancient``/``transform_select``/
  ``card_reward``: upgraded cards gained, cards added, relics gained, HP and gold change;
* **primary**: the share of openings whose taken relic produced an upgrade, and which offered
  position the policy favours -- if the upgrade relic sits at an offered position the policy
  systematically does not pick, that is a second unexercised strength lever next to the campfire;
* cross-check: the upgrade totals here must agree with the 463 the step-attribution census
  attributes to the ``ancient`` phase and with the 0.326 mean every other census reproduces, and
  ``ApplyRelicPickup`` should name the relic that actually arrived;
* not measured: what each relic does later in the run. Only the immediate deck delta is attributed.
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

OPEN_PHASES = {"ancient", "transform_select", "card_reward"}
SKIP = 3  # RunConstants.RewardSkipAction


def deck_and_relics(core):
    lists = core._core.state_lists()
    deck = [int(card) for card in lists["deck"]]
    return (sum(1 for card in deck if card < 0), len(deck),
            {int(relic) for relic in lists["relics"]})


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

    openings: list[dict] = []
    for seed in window:
        env = factory(int(seed))
        observation, info = env.reset(seed=int(seed))
        terminal = truncated = False
        core = env.unwrapped
        state = core.state_info()
        offered = [int(option) for option in core._core.state_lists()["neow_options"]]
        up0, size0, relics0 = deck_and_relics(core)
        legal = {f"option {i}": bool(flag)
                 for i, flag in enumerate(core.base_action_mask()[:4])}
        pending = {"seed": int(seed), "offered": offered, "legal": legal,
                   "hp": int(state["player_hp"]), "max_hp": int(state["player_max_hp"]),
                   "gold": int(state["gold"]), "up0": up0, "size0": size0,
                   "relics0": sorted(relics0), "chosen": None, "phase": state.get("phase_name")}
        steps = 0
        while not (terminal or truncated) and pending.get("resolved") is None:
            mask = env.action_masks()
            if not any(bool(value) for value in mask):
                break
            state = core.state_info()
            phase = state.get("phase_name")
            if phase not in OPEN_PHASES:
                up1, size1, relics1 = deck_and_relics(core)
                pending.update(resolved=True, upgrade_delta=up1 - pending["up0"],
                               deck_delta=size1 - pending["size0"],
                               hp_delta=int(state["player_hp"]) - pending["hp"],
                               gold_delta=int(state["gold"]) - pending["gold"],
                               new_relics=sorted(relics1 - set(pending["relics0"])),
                               end_phase=phase)
                break
            raw, _ = model.predict(observation, action_masks=mask, deterministic=True)
            action = int(raw.item() if hasattr(raw, "item") else raw)
            if pending["chosen"] is None and phase == "ancient":
                pending["chosen"] = action // TARGET_SLOTS
                pending["raw_action"] = action
            _obs, _r, terminal, truncated, info = env.step(action)
            observation = _obs
            steps += 1
            if steps >= args.max_steps:
                truncated = True
        pending.setdefault("resolved", False)
        pending["steps_before_resolution"] = steps
        openings.append(pending)
        env.close()

    resolved = [row for row in openings if row["resolved"]]
    took_something = [row for row in resolved if row["chosen"] is not None
                      and row["chosen"] != SKIP]
    gained = [row for row in resolved if (row.get("upgrade_delta") or 0) > 0]
    by_position = collections.Counter(
        str(row["chosen"]) for row in resolved if row["chosen"] is not None)
    upgrade_by_position = collections.Counter(
        str(row["chosen"]) for row in gained)
    payload = {
        "aggregates": {
            "episodes": len(openings),
            "resolved_openings": len(resolved),
            "unresolved_openings": len(openings) - len(resolved),
            "chosen_position_counts": dict(by_position),
            "upgrade_granting_positions": dict(upgrade_by_position),
            "share_of_openings_taking_an_upgrade_relic": (
                round(len(gained) / len(resolved), 4) if resolved else None),
            "upgrades_total": sum(row.get("upgrade_delta") or 0 for row in resolved),
            "relic_gained_share": round(
                sum(1 for row in resolved if row.get("new_relics")) / len(resolved), 4)
            if resolved else None,
            "relics_seen_when_upgrade_happened": dict(collections.Counter(
                relic for row in gained for relic in row.get("new_relics") or [])),
            "offered_option_ids_top": dict(collections.Counter(
                relic for row in resolved for relic in row["offered"] if relic).most_common(8)),
            "mean_deck_delta": round(sum(row.get("deck_delta") or 0 for row in resolved)
                                     / len(resolved), 3) if resolved else None,
            "mean_hp_delta_at_resolution": (round(statistics.fmean(
                [row["hp_delta"] for row in resolved if row.get("hp_delta") is not None]), 2)
                if any(row.get("hp_delta") is not None for row in resolved) else None),
        },
        "checkpoint_sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "not_established": [
            "what the chosen relic does later in the run; only the immediate deck delta is read",
            "that a different opening relic would win more -- this measures the preference, not a "
            "counterfactual policy"],
        "openings": openings,
        "reproduce": ("../third_party/slay-the-spire-2-emulator-main/.venv/Scripts/python.exe "
                      "scripts/measure_opening_choice.py --checkpoint <zip> --limit "
                      f"{args.limit} --start-offset {args.start_offset} --out <file.json>"),
        "scope": ("simulator_act1 label, argmax, one checkpoint, "
                  f"{len(openings)} seeds; the run-start relic choice and its immediate deck effect"),
        "seed_window": {"offset": args.start_offset, "limit": args.limit,
                        "stage": args.stage, "split": args.split},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")
    agg = payload["aggregates"]
    print(f"{agg['episodes']} openings, {agg['resolved_openings']} resolved, "
          f"{agg['upgrades_total']} upgrades granted at the opening "
          f"(share taking an upgrade relic {agg['share_of_openings_taking_an_upgrade_relic']})")
    print(f"  chosen positions: {agg['chosen_position_counts']} | "
          f"upgrade-granting positions: {agg['upgrade_granting_positions']}")
    print(f"  relics present when an upgrade happened: {agg['relics_seen_when_upgrade_happened']}")
    print(f"  offered relic ids (top): {agg['offered_option_ids_top']}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
