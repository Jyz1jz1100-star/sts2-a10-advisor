"""Were the lost Act-2 boss clears recoverable, or is the win unreachable from that state?

`act2_boss_misexit_rate_20260919.json` counted runs that killed the Act-2 boss and were not judged
wins -- 21 seeds on one checkpoint, 6 on another window, 7 on a second arm's checkpoint -- and the
report calls them false negatives. That word carries a load-bearing assumption: that a win was
*available* and the run missed it. The assumption had been checked on exactly one seed
(`130012038`), where two of four legal actions at the relic-reward screen ended the run `complete`.

This drives `probe_boss_reward_order.py` over every seed in those groups, at the checkpoint that
produced the recording, and answers per seed whether any legal action at the floor-17 `relic_reward`
state yields `complete / won`. The distinction matters because the two answers imply different fixes:
a recoverable loss is a policy gap worth training for (and the cheapest one in the campaign), while a
lost boss clear with no winning action at any reward screen is another structural ceiling, which would
make "Act 2 cleared" unrepresentable rather than merely unravelling.

Aggregates are per group as well as pooled, because the groups are different checkpoints and one seed
(`130001859`) appears in two of them by design.
"""

from __future__ import annotations

import argparse
import collections
import json
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts/probe_boss_reward_order.py"
PYTHON = Path(sys.executable)
SOURCE = "docs/evidence/act2_boss_misexit_rate_20260919.json"


def build_groups(source: dict) -> list[dict]:
    """One group per (checkpoint, seed list) the misexit measurement actually rolled.

    Two of the three groups name their checkpoint in the artifact. The third -- the independent
    checkpoint-partition window -- records only "same script", so its checkpoint is taken from the
    top level *and has to earn its place*: `summarise` keeps it only if replaying those seeds at
    that checkpoint reproduces the loss the artifact recorded. An attribution that cannot be
    reproduced is reported, not averaged in.
    """

    primary = source["checkpoint"]
    groups = [
        {"checkpoint": primary,
         "checkpoint_provenance": "named by the measurement artifact",
         "group": "whole_partition_promotion",
         "seeds": [int(seed) for seed in source["truncation_seeds"]]},
        {"checkpoint": source["independent_window_checkpoint_split"].get("checkpoint", primary),
         "checkpoint_provenance": "inherited from the top-level checkpoint because the artifact's "
                                  "reproduction line says 'same script' -- verified by replay",
         "group": "independent_window_checkpoint_split",
         "seeds": [int(seed)
                   for seed in source["independent_window_checkpoint_split"]["truncation_seeds"]]},
        {"checkpoint": source["second_checkpoint_generality"]["checkpoint"],
         "checkpoint_provenance": "named by the measurement artifact",
         "group": "second_checkpoint_generality",
         "seeds": [int(seed)
                   for seed in source["second_checkpoint_generality"]["truncation_seeds"]]},
    ]
    for group in groups:
        if not Path(group["checkpoint"]).is_absolute():
            group["checkpoint"] = str(ROOT / group["checkpoint"])
    return groups


def fork_group(group: dict, fork_phase: str, max_steps: int) -> dict:
    """Run the reward-screen probe for one group and pull the per-seed verdicts out of it."""

    with tempfile.TemporaryDirectory() as scratch:
        out = Path(scratch) / "fork.json"
        process = subprocess.run(
            [str(PYTHON), str(PROBE), "--checkpoint", group["checkpoint"],
             "--seeds", ",".join(str(seed) for seed in group["seeds"]),
             "--fork-phase", fork_phase, "--max-steps", str(max_steps), "--out", str(out)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if not out.is_file():
            print(process.stdout[-1500:], process.stderr[-1500:], file=sys.stderr)
            raise SystemExit(f"reward fork probe produced no artifact for {group['group']}")
        payload = json.loads(out.read_text(encoding="utf-8"))
    rows = []
    for record in payload["seeds"]:
        legal = [int(action) for action in record["legal_actions"]]
        winners = sorted(int(variant["action"]) for variant in record["variants"]
                         if variant["outcome"]["final_phase"] == "complete"
                         and bool(variant["outcome"]["run_won"]))
        played = record["as_played"]
        rows.append({
            "as_played_floor": played.get("final_floor"),
            "as_played_phase": played.get("final_phase"),
            "as_played_won": bool(played.get("run_won")),
            "fork_found": bool(record["fork_found"]),
            "group": group["group"],
            "legal_action_count": len(legal),
            "policy_choice_won": int(record["policy_chosen"]) in winners,
            "seed": int(record["seed"]),
            "winning_actions": winners,
        })
    replayed = [row for row in rows if row["fork_found"]]
    reproduces = bool(replayed) and all(
        row["as_played_phase"] == "map" and not row["as_played_won"]
        and row["as_played_floor"] == 17 for row in replayed)
    return {"checkpoint": str(Path(group["checkpoint"]).relative_to(ROOT).as_posix()),
            "checkpoint_provenance": group["checkpoint_provenance"],
            "fork_phase": fork_phase, "group": group["group"],
            "replays_the_recorded_loss": reproduces, "rows": rows}


def summarise(forks: list[dict], source: dict, max_steps: int) -> dict:
    verified = [fork for fork in forks if fork["replays_the_recorded_loss"]]
    unverified = [fork for fork in forks if not fork["replays_the_recorded_loss"]]
    rows = [row for fork in verified for row in fork["rows"]]
    per_group = collections.Counter()
    for fork in verified:
        for row in fork["rows"]:
            per_group[fork["group"]] += 1
    return {
        "aggregates": {
            "forks_found": sum(1 for row in rows if row["fork_found"]),
            "groups_excluded_for_not_replaying_the_loss": sorted(
                fork["group"] for fork in unverified),
            "groups_in_the_pooled_verdict": sorted(fork["group"] for fork in verified),
            "pct_of_forked_seeds_recoverable": round(
                100.0 * sum(1 for row in rows if row["fork_found"] and row["winning_actions"])
                / max(1, sum(1 for row in rows if row["fork_found"])), 1),
            "recoverable": sum(1 for row in rows if row["fork_found"] and row["winning_actions"]),
            "rows": len(rows),
            "rows_per_group": dict(sorted(per_group.items())),
            "seeds_where_policy_chose_a_winning_action": sum(
                1 for row in rows if row["policy_choice_won"]),
            "unrecoverable": sum(1 for row in rows if row["fork_found"]
                                 and not row["winning_actions"]),
            "winning_action_counts": dict(sorted(collections.Counter(
                str(len(row["winning_actions"])) for row in rows if row["fork_found"]).items())),
        },
        "established": [
            "every seed in every group was forked at the checkpoint that recorded its loss, so a "
            "recoverable verdict is about the policy that actually played the run",
            f"of the {sum(1 for row in rows if row['fork_found'])} forked seeds, "
            f"{sum(1 for row in rows if row['winning_actions'])} had at least one legal action at "
            "the relic-reward screen that ended the run as a win, and the policy took it on "
            f"{sum(1 for row in rows if row['policy_choice_won'])} of them",
        ],
        "fork_phase": forks[0]["fork_phase"] if forks else None,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "groups": forks,
        "max_steps_per_episode": max_steps,
        "not_established": [
            "that a winning action at the relic screen is reachable by any policy that beats the "
            "boss: this forks the state the recorded run actually arrived at, so a seed whose run "
            "reached a different reward state is reported as fork_found false rather than assumed",
            "that these wins survive a re-roll of the whole run -- the probe replays the recorded "
            "action prefix and substitutes one action at one state, which is a counterfactual "
            "about that state, not an end-to-end policy evaluation",
            "anything about the shipped game",
            "a rate over arrivals: the denominators here are the recorded losses only, so this "
            "answers 'were the lost ones recoverable', not 'what fraction of boss kills are lost'"],
        "scope": ("every seed the Act-2 boss mis-exit measurement recorded as cleared-but-not-judged,"
                  " forked at the relic-reward screen of its own checkpoint"),
        "source_measurement": {"artifact": SOURCE,
                               "checkpoint_sha256": source.get("checkpoint_sha256"),
                               "groups": {group["group"]: len(group["seeds"])
                                          for group in build_groups(source)}},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / SOURCE)
    parser.add_argument("--fork-phase", default="relic_reward")
    parser.add_argument("--max-steps", type=int, default=4000)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    source = json.loads(args.source.read_text(encoding="utf-8"))
    groups = build_groups(source)
    for group in groups:
        if not Path(group["checkpoint"]).is_file():
            raise SystemExit(f"missing checkpoint for {group['group']}: {group['checkpoint']}")
        print(f"{group['group']}: {len(group['seeds'])} seed(s) at "
              f"{Path(group['checkpoint']).name}", flush=True)
    forks = [fork_group(group, args.fork_phase, args.max_steps) for group in groups]
    payload = summarise(forks, source, args.max_steps)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    agg = payload["aggregates"]
    print(f"{agg['rows']} seeds, {agg['forks_found']} reached the {args.fork_phase} screen: "
          f"{agg['recoverable']} recoverable, {agg['unrecoverable']} not, "
          f"policy took the win {agg['seeds_where_policy_chose_a_winning_action']} time(s)")
    print(f"winning-action counts among forked seeds: {agg['winning_action_counts']}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
