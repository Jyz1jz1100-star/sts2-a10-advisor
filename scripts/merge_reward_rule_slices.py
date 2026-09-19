"""Join the reward-rule slices for the whole promotion partition, with its closure checks.

`probe_boss_reward_rule.py` rolls one contiguous slice per invocation (8 slices x 1,250 seeds), and
each slice already carries the exact 2x2 join of winning seeds between the plain and rule passes.
This adds them up and, more importantly, runs the two checks that make the total trustworthy rather
than just larger: the plain pass must reproduce the judged-win count the committed Act-2 mis-exit
measurement recorded, and the converted seeds must be the same seed set that measurement listed as
cleared-but-not-judged. Two independent instruments agreeing on the same seeds is the claim; the win
count on its own would only say a rule helps.

Reads the scratch slices this script names, so re-running it after a fresh set is one command.
"""

from __future__ import annotations

import argparse
import glob
import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKSLASH = chr(92)


def recorded_for(misexit: dict, group: str) -> tuple[list[int], int]:
    """The seed list and judged-win baseline a committed measurement recorded for one group.

    Every group is a pre-registration: it names the seeds it lost and the wins it counted before
    this script was written, so comparing against it is a test rather than a fit.
    """

    holder = misexit if group == "whole_partition_promotion" else misexit[group]
    partition = holder["whole_partition"] if group == "whole_partition_promotion" else holder
    return (sorted(int(seed) for seed in holder["truncation_seeds"]),
            int(partition["act1"]["boss_win"]) + int(partition["act2"]["boss_win"]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slices", default="runtime/census_scratch/pop/slice_*.json")
    parser.add_argument("--misexit", type=Path,
                        default=ROOT / "docs/evidence/act2_boss_misexit_rate_20260919.json")
    parser.add_argument("--compare-group", default="whole_partition_promotion",
                        choices=("none", "whole_partition_promotion",
                                 "independent_window_checkpoint_split",
                                 "second_checkpoint_generality"),
                        help="'none' for a window no earlier measurement pre-registered, such as "
                             "the held-out final partition: the closure checks still run, the "
                             "reconciliation ones are reported as not applicable rather than "
                             "quietly passing")
    parser.add_argument("--label", default=None, help="free-text note carried into the artefact")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    paths = sorted(ROOT.glob(args.slices))
    if not paths:
        raise SystemExit(f"no slices match {args.slices}")
    slices = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    seeds_covered = [int(seed) for data in slices for seed in data["seeds"]]
    if len(seeds_covered) != len(set(seeds_covered)):
        raise SystemExit("two slices rolled the same seed; the partition would be double counted")

    if args.compare_group == "none":
        recorded_losses, baseline = [], None
    else:
        misexit = json.loads(args.misexit.read_text(encoding="utf-8"))
        recorded_losses, baseline = recorded_for(misexit, args.compare_group)
    converted = sorted({seed for data in slices
                        for seed in data["win_seed_join"]["converted_seeds"]})
    lost = sorted({seed for data in slices for seed in data["win_seed_join"]["lost_seeds"]})
    kept = sorted({seed for data in slices for seed in data["win_seed_join"]["kept_seeds"]})

    def summed(pick):
        return sum(pick(data) for data in slices)

    agg = {
        "converted_seed_count": len(converted),
        # None, not False, when no earlier measurement pre-registered this window: a holdout has
        # nothing to reconcile against, and reporting that as a mismatch would misdescribe it.
        "converted_seeds_equal_the_recorded_losses": (
            converted == recorded_losses if baseline is not None else None),
        "episodes": summed(lambda d: d["aggregates"]["episodes"]),
        "illegal_actions_after_rule": summed(lambda d: d["after_rule"]["illegal_actions"]),
        "illegal_actions_plain": summed(lambda d: d["before_rule"]["illegal_actions"]),
        "kept_win_count": len(kept),
        "lost_win_count": len(lost),
        "matched_boss_relic_screens": summed(lambda d: d["aggregates"]["screen_states_matched"]),
        "plain_win_count": summed(lambda d: d["aggregates"]["plain_wins"]),
        "plain_win_matches_the_recorded_baseline": (
            summed(lambda d: d["aggregates"]["plain_wins"]) == baseline
            if baseline is not None else None),
        "recorded_baseline_win_count": baseline,
        "rule_win_count": summed(lambda d: d["aggregates"]["ruled_wins"]),
        "slices": len(slices),
        "truncations_after_rule": summed(lambda d: d["aggregates"]["truncations_after_rule"]),
        "truncations_plain": summed(lambda d: d["aggregates"]["truncations_plain"]),
        "unclassified_dead_ends_after_rule": summed(
            lambda d: d["aggregates"]["unclassified_dead_ends_after_rule"]),
        "unclassified_dead_ends_plain": summed(
            lambda d: d["aggregates"]["unclassified_dead_ends_plain"]),
    }
    payload = {
        "compare_group": args.compare_group,
        "label": args.label,
        "_assembled_by": [
            f"scripts/probe_boss_reward_rule.py --checkpoint <checkpoint below> --start-offset "
            f"<{'|'.join(str(index * slices[0]['aggregates']['episodes'])
                         for index in range(len(slices)))}> "
            f"--limit {slices[0]['aggregates']['episodes']} --rule highest_legal --out <slice> "
            f"(x{len(slices)})",
            "scripts/merge_reward_rule_slices.py joins them; every seed of the declared partition "
            "appears in exactly one slice, which this script checks before summing"],
        "aggregates": agg,
        "checkpoint": str(slices[0]["after_rule"]["checkpoint"]).replace(BACKSLASH, "/"),
        "converted_seeds": converted,
        "established": (
            [f"the plain pass over {agg['episodes']} promotion seeds reproduces the "
             f"{baseline} judged wins the committed mis-exit measurement recorded, so the two "
             "instruments are looking at the same population"]
            if baseline is not None else
            [f"{agg['episodes']} seeds were rolled with no pre-registered baseline to reconcile "
             "against -- a holdout has nothing to match, so this artifact claims only its own "
             "internal closure, not agreement with an earlier measurement"]) + [
            f"the rule adds {len(converted)} judged wins and removes {len(lost)}"
            + (", and the added seeds are exactly the runs recorded as "
               "cleared-the-boss-but-not-judged" if baseline is not None else
               " (no pre-registered seed list to compare the added set against)"),
            f"truncations fall {agg['truncations_plain']} -> {agg['truncations_after_rule']} while "
            "illegal actions and unclassified dead ends stay 0 in both passes"],
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "kept_win_seeds": kept,
        "lost_win_seeds": lost,
        "not_established": [
            "that a trained policy would find this rule: it is hand-written and applied at one "
            "screen, so the number bounds what that one decision is worth, not what training gets",
            "anything about Act 3 or the shipped game -- an Act-2 judged win is still two acts, not "
            "the three-act flow the objective names",
            f"that the +{len(converted)} transfers to other checkpoints or policies: this is one "
            "checkpoint's promotion partition, and the rule only pays where a run reaches the screen"],
        "recorded_losses_for_comparison": recorded_losses,
        "rule": slices[0]["rule"],
        "scope": ("every seed of the act1 promotion partition at one checkpoint, argmax everywhere "
                  "except a stated action at the boss relic_reward screen, judged by "
                  "training/evaluation.py"),
        "slice_detail": [{"converted": len(d["win_seed_join"]["converted_seeds"]),
                          "episodes": d["aggregates"]["episodes"],
                          "lost": len(d["win_seed_join"]["lost_seeds"]),
                          "plain_wins": d["aggregates"]["plain_wins"],
                          "ruled_wins": d["aggregates"]["ruled_wins"],
                          "seed_source": d["seed_source"]} for d in slices],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    print(f"{agg['episodes']} episodes over {agg['slices']} slices; judged wins "
          f"{agg['plain_win_count']} -> {agg['rule_win_count']}; converted "
          f"{agg['converted_seed_count']}, lost {agg['lost_win_count']}")
    print(f"baseline reproduced: {agg['plain_win_matches_the_recorded_baseline']}; converted == "
          f"recorded losses: {agg['converted_seeds_equal_the_recorded_losses']}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
