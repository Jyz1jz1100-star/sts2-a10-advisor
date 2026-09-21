"""Does a stated rule at the boss relic screen turn lost Act-2 clears into judged wins end to end?

`act2_boss_clear_recoverability_20260920.json` is a single-state counterfactual: replay the recorded
prefix, substitute one action at the floor-17 `relic_reward` state, and the run ends `complete / won`.
That says the exit was in the mask at that moment. It does not say a policy that *always* plays by a
simple rule gets there, because the substitute also changes what the player takes with them.

So this runs the whole episode under a rule and lets the campaign's own classifier judge it:
`training/evaluation.py::evaluate_policy` is called with a policy adapter that is the checkpoint's
argmax everywhere except a state whose phase is `relic_reward` and whose current node is the boss
(`current_node_type=6`), where it takes the highest-numbered legal action -- the proceed-style option
the recoverability fork pointed at. Two passes over the same seeds (plain argmax, then rule) make the
delta self-contained, and both passes report `illegal_actions` and `unclassified_dead_ends` under the
same gate code, so a "win" bought with a contract violation would show up as one.

The rule is not learned and the seeds are the ones already known to have lost; the population-level
question (how many judged Act-2 wins a partition gains) is the same command over a wider window.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "third_party" / "slay-the-spire-2-emulator-main" / "src"))
sys.path.insert(0, str(ROOT))

from training.campaign_content import CAMPAIGN_ENVIRONMENT_VERSION

REWARD_PHASE = "relic_reward"
BOSS_NODE_TYPE = 6  # third_party RunConstants.cs:32

# The screen this rule plays at is a relic of approx-v1: from
# sts2sim-campaign-fidelity-v2 a boss clear grants no relic (RewardsSet.cs:245-261), so
# `phase == relic_reward at a boss node` no longer occurs and this probe measures nothing
# there. Its +/- win deltas stay approx-v1 results and are stamped as such.
APPROX_V1_RELIC_SCREEN = "sts2sim-campaign-approx-v1"


class RewardScreenRule:
    """The checkpoint's argmax, except at a boss relic screen where a stated rule decides."""

    def __init__(self, base, offsets, mode: str):
        from training.v2_observation import PHASE_NAMES

        self._base = base
        self._mode = mode
        self._phase_names = PHASE_NAMES
        self.phase_index = offsets["phase_onehot"]
        self.node_index = offsets["current_node_type_onehot"]
        self.matched = 0
        self.overrides: list[dict] = []

    def _at_boss_relic_screen(self, observation) -> bool:
        vector = observation
        phase = int(max(range(len(self._phase_names)),
                         key=lambda index: vector[self.phase_index + index]))
        node = int(max(range(8), key=lambda index: vector[self.node_index + index]))
        return self._phase_names[phase] == REWARD_PHASE and node == BOSS_NODE_TYPE

    def predict(self, observation, action_masks=None, deterministic=True):
        raw, value = self._base.predict(observation, action_masks=action_masks,
                                        deterministic=deterministic)
        action = int(raw.item() if hasattr(raw, "item") else raw)
        if self._mode != "argmax" and self._at_boss_relic_screen(observation):
            self.matched += 1
            legal = [index for index, on in enumerate(action_masks) if bool(on)]
            chosen = max(legal) if self._mode == "highest_legal" else min(legal)
            if chosen != action:
                self.overrides.append({"from": action, "to": chosen})
            action = chosen
        return np.asarray([action], dtype=np.int64), value


def run_pass(args, label, factory, model, seeds, max_steps):
    from training.evaluation import evaluate_policy

    metrics = evaluate_policy(model, env_factory=factory, seeds=seeds, stage=args.stage,
                              split=f"rule-{label}", scope="simulator_act1",
                              checkpoint=args.checkpoint, max_steps_per_episode=max_steps)
    payload = metrics.to_dict()
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "config/training_v2.toml")
    parser.add_argument("--stage", default="act1")
    parser.add_argument("--seeds", default="", help="comma list; default reads --recoverability")
    parser.add_argument("--recoverability", type=Path,
                        default=ROOT / "docs/evidence/act2_boss_clear_recoverability_20260920.json")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap the seed list (used with --split-window to widen the question)")
    parser.add_argument("--split", default="promotion")
    parser.add_argument("--start-offset", type=int, default=None,
                        help="first index of the partition slice to roll")
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--rule", default="highest_legal",
                        choices=("argmax", "highest_legal", "lowest_legal"))
    parser.add_argument("--per-seed", action="store_true",
                        help="also run each seed through both passes, to separate conversions "
                             "from wins the rule would cost")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    import sts2_gym
    from sb3_contrib import MaskablePPO
    from stable_baselines3.common.vec_env import DummyVecEnv

    from training.v2_config import load_v2_training_config
    from training.v2_curriculum import _environment_factory
    from training.v2_observation import BLOCK_OFFSETS

    if args.seeds:
        seeds = [int(value) for value in args.seeds.split(",") if value.strip()]
        seed_source = "--seeds"
    elif args.start_offset is not None or args.limit:
        # A contiguous slice of the declared partition, so the population rate can be measured
        # without hand-writing 10,000 integers (and so a shard boundary is auditable).
        from training.v2_config import load_v2_training_config as _load

        partition = _load(args.config.resolve()).partition(args.stage, args.split).seeds()
        start = args.start_offset or 0
        seeds = partition[start:start + (args.limit or len(partition))]
        seed_source = (f"{args.stage}/{args.split} partition slice "
                       f"[{start}:{start + len(seeds)}] of {len(partition)}")
    else:
        recovered = json.loads(args.recoverability.read_text(encoding="utf-8"))
        seeds = sorted({int(row["seed"]) for group in recovered["groups"] for row in group["rows"]})
        seed_source = str(args.recoverability.relative_to(ROOT).as_posix())
    if args.limit and args.seeds:
        seeds = seeds[:args.limit]

    config = load_v2_training_config(args.config.resolve())
    stage = next(s for s in config.stages if s.name == args.stage)
    max_steps = args.max_steps or stage.max_episode_steps
    open_stage = dataclasses.replace(stage, max_floor=None, max_episode_steps=max_steps)
    factory = _environment_factory(config, open_stage, sts2_gym)
    checkpoint = args.checkpoint.resolve()

    probe = DummyVecEnv([lambda: factory(seeds[0])])
    try:
        base_model = MaskablePPO.load(str(checkpoint), env=probe, device="cpu")
    finally:
        probe.close()
    plain = run_pass(args, "argmax", factory, base_model, seeds, max_steps)
    adapter = RewardScreenRule(base_model, BLOCK_OFFSETS, args.rule)
    ruled = run_pass(args, args.rule, factory, adapter, seeds, max_steps)

    per_seed: list[dict] = []
    if args.per_seed:
        # One episode per call so the two passes can be joined per seed: an aggregate delta hides
        # the case where the rule converts one run and breaks another, and only the join shows it.
        for seed in seeds:
            before = run_pass(args, "argmax", factory, base_model, [seed], max_steps)
            matched_before = adapter.matched
            after = run_pass(args, args.rule, factory, adapter, [seed], max_steps)
            per_seed.append({
                "matched_screen": adapter.matched > matched_before,
                "plain_won": int(before["wins"]),
                "ruled_won": int(after["wins"]),
                "seed": seed,
            })

    payload = {
        "schema_version": 1,
        "environment_version": CAMPAIGN_ENVIRONMENT_VERSION,
        # A screen this rule can act on no longer exists in the live environment, so the
        # number below belongs to the version it was measured on and to no other.
        "result_applies_to_environment": APPROX_V1_RELIC_SCREEN,
        "aggregates": {
            "delta_judged_wins": ruled["wins"] - plain["wins"],
            "episodes": len(seeds),
            "plain_wins": plain["wins"],
            "ruled_wins": ruled["wins"],
            "rule_state_overrides": len(adapter.overrides),
            "screen_states_matched": adapter.matched,
            "truncations_after_rule": ruled["truncations"],
            "truncations_plain": plain["truncations"],
            "unclassified_dead_ends_after_rule": ruled["unclassified_dead_ends"],
            "unclassified_dead_ends_plain": plain["unclassified_dead_ends"],
        },
        "after_rule": ruled,
        "before_rule": plain,
        # The exact 2x2, joined on the seeds each pass reports as terminal wins. An aggregate delta
        # cannot tell "converted three and broke three" from "did nothing", and this can.
        "win_seed_join": {
            "converted_seeds": sorted(set(ruled["winning_seeds"])
                                      - set(plain["winning_seeds"])),
            "kept_seeds": sorted(set(ruled["winning_seeds"]) & set(plain["winning_seeds"])),
            "lost_seeds": sorted(set(plain["winning_seeds"]) - set(ruled["winning_seeds"])),
        },
        "per_seed": per_seed,
        "per_seed_tally": {
            "converted_to_win": sum(1 for row in per_seed
                                    if not row["plain_won"] and row["ruled_won"]),
            "cost_a_win": sum(1 for row in per_seed if row["plain_won"] and not row["ruled_won"]),
            "matched_screens": sum(1 for row in per_seed if row["matched_screen"]),
            "unchanged": sum(1 for row in per_seed
                             if row["plain_won"] == row["ruled_won"]),
        } if per_seed else None,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "not_established": [
            "that the rule is learnable by a policy -- it is a hand-written tiebreak applied at one "
            "screen, so a positive delta bounds what training could recover, it is not a trained "
            "result",
            "that the matched screen is the boss relic screen specifically: the adapter keys on "
            "phase=relic_reward and current_node_type=boss, which any boss relic award satisfies",
            "anything about the shipped game",
            "anything about an environment later than approx-v1: a boss grants no relic there, "
            "so this rule has no state to act on and a re-run measures zero, not a lost gain"],
        "rule": {"argmax_elsewhere": True, "chosen": args.rule,
                 "screen": f"{REWARD_PHASE} at current_node_type={BOSS_NODE_TYPE}"},
        "scope": ("the seeds whose Act-2 boss clear went unjudged, replayed end to end under a "
                  "stated reward-screen rule, classified by training/evaluation.py"),
        "seed_source": seed_source,
        "seeds": seeds,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    agg = payload["aggregates"]
    print(f"{agg['episodes']} seeds; judged wins {agg['plain_wins']} -> {agg['ruled_wins']} "
          f"(delta {agg['delta_judged_wins']:+d}); overrides {agg['rule_state_overrides']}")
    print(f"illegal {plain['illegal_actions']} -> {ruled['illegal_actions']}; unclassified "
          f"{agg['unclassified_dead_ends_plain']} -> {agg['unclassified_dead_ends_after_rule']}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
