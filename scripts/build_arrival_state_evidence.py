"""Merge the arrival-state shards into one evidence artifact, with the reproduction check built in.

``measure_arrival_state_at_boss.py`` writes one file per 500-seed shard. Pooling has to happen on
the rows, not on the per-shard aggregates: a difference of means over 162 arrivals is not the
average of seven differences over 20-ish arrivals, and the permutation tests need the pooled
sample. This script therefore recomputes everything from concatenated rows and refuses to
merge shards that disagree about which checkpoint produced them.

The merge also carries the two checks the measurement promises:

* **reproduction** -- arrivals and clears must equal ``potion_slot_cost_20260919.json``, which
  measured the same partition and the same policy on an earlier question. A mismatch means this
  instrumentation is wrong, and the artifact says so in ``established`` rather than reporting a
  finding;
* **provenance** -- the older artifact is itself a merge of seven shards and kept only
  ``aggregates`` per shard, so it has no top-level ``checkpoint_sha256``: which policy produced
  its rows is inferable from sibling artifacts, not stated in the file. This one states it.
"""

from __future__ import annotations

import argparse
import collections
import json
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from measure_arrival_state_at_boss import (ACT_NAMES, RESOURCES,  # noqa: E402
                                          bootstrap_mean_ci, two_sample)

CENSUS = ROOT / "docs/evidence/potion_slot_cost_20260919.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shards", type=Path, nargs="+", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    parts = [json.loads(path.read_text(encoding="utf-8")) for path in args.shards]
    digests = {part["checkpoint_sha256"] for part in parts}
    if len(digests) != 1:
        raise SystemExit(f"refusing to merge shards from different checkpoints: {digests}")
    digest = digests.pop()
    rows = [row for part in parts for row in part["rows"]]
    windows = sorted((part["seed_window"]["offset"] for part in parts))
    expected = list(range(0, 500 * len(parts), 500))
    if windows != expected:
        raise SystemExit(f"shard offsets are {windows}, expected the contiguous {expected}")

    arrivals = [row for row in rows if row["arrived"]]
    by_act = {act: [row for row in arrivals if row["act"] == act] for act in (1, 2)}
    cleared = [row for row in arrivals if row["cleared"]]

    prior = json.loads(CENSUS.read_text(encoding="utf-8"))["aggregates"]
    mine = {"arrivals": len(arrivals),
            "arrivals_by_generated_act": {ACT_NAMES[a]: len(by_act[a]) for a in (1, 2)},
            "cleared": len(cleared),
            "cleared_by_generated_act": dict(collections.Counter(
                ACT_NAMES.get(row["act"], str(row["act"])) for row in cleared)),
            # Which boss each act's arrivals actually fought: both acts offer three, so a
            # cross-act difference cannot be blamed on "one hard scripted boss".
            "encounters_by_generated_act": {
                ACT_NAMES[act]: dict(collections.Counter(
                    row["encounter_id"] for row in by_act[act])) for act in (1, 2)},
            "boss_floors_by_generated_act": {
                ACT_NAMES[act]: dict(collections.Counter(
                    row["floor"] for row in by_act[act])) for act in (1, 2)},
            "episodes": len(rows)}
    # The committed artifact names its clear count ``cleared_arrivals``, so the key mapping is
    # spelled out rather than looked up by name.
    reproduction = {
        "committed_artifact": CENSUS.name,
        "expected": {"arrivals": prior.get("arrivals"),
                     "arrivals_by_generated_act": prior.get("arrivals_by_generated_act"),
                     "cleared": prior.get("cleared_arrivals"),
                     "cleared_by_generated_act": prior.get("cleared_by_generated_act"),
                     "episodes": prior.get("episodes")},
        "measured": {key: mine.get(key) for key in
                     ("arrivals", "arrivals_by_generated_act", "cleared",
                      "cleared_by_generated_act", "episodes")},
    }
    reproduction["matches"] = (reproduction["expected"] == reproduction["measured"])

    def group_stats(sample):
        if not sample:
            return {"n": 0}
        return {"n": len(sample),
                **{resource: round(statistics.fmean([row[resource] for row in sample]), 3)
                   for resource in RESOURCES},
                "boss_decisions_median": statistics.median(
                    [row["boss_decisions"] for row in sample]),
                "relic_count_bootstrap_ci_95": bootstrap_mean_ci(
                    [row["relic_count"] for row in sample]),
                "deck_size_bootstrap_ci_95": bootstrap_mean_ci(
                    [row["deck_size"] for row in sample])}

    payload = {
        "aggregates": mine,
        "checkpoint_sha256": digest,
        "cross_act_arrival_state": {
            resource: two_sample([row[resource] for row in by_act[1]],
                                 [row[resource] for row in by_act[2]])
            for resource in RESOURCES},
        "established": ([
            "arrivals and clears reproduce the committed census, so the two act groups being "
            "compared were produced by the same policy over the same seeds as every other "
            "campaign figure"] if reproduction["matches"] else [
            "NOT ESTABLISHED: arrivals/clears do not reproduce the committed census, so the "
            "instrumentation is wrong and no comparison below should be read as a finding"]),
        "act2_win_vs_loss": {
            resource: two_sample([row[resource] for row in by_act[2] if row["cleared"]],
                                 [row[resource] for row in by_act[2] if not row["cleared"]])
            for resource in RESOURCES},
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "groups": {
            f"{ACT_NAMES[act]}|{'win' if won else 'loss'}": group_stats(
                [row for row in by_act[act] if row["cleared"] == won])
            for act in (1, 2) for won in (True, False)},
        "not_established": [
            "card quality (upgrades, which cards): state_info() reports counts, not strengths, "
            "so equal deck_size and relic_count is equal quantity, not equal power",
            "anything about Act 1 wins specifically: this window contains "
            f"{sum(1 for row in by_act[1] if row['cleared'])} of them, so the Act-1 side of "
            "every comparison here is a loss-vs-loss comparison",
            "that the acts differ for a single reason -- this retires one candidate confound "
            "and measures one predictor, it does not identify the mechanism"],
        "readable": {
            "act1_arrivals": len(by_act[1]), "act2_arrivals": len(by_act[2]),
            "act2_wins": sum(1 for row in by_act[2] if row["cleared"]),
            "threshold_met_20_arrivals_per_act_and_10_act2_wins": bool(
                len(by_act[1]) >= 20 and len(by_act[2]) >= 20
                and sum(1 for row in by_act[2] if row["cleared"]) >= 10)},
        "reproduction_check": reproduction,
        "rows": rows,
        "scope": ("simulator_act1 label, mixed generated acts, argmax, one checkpoint, "
                  f"{len(rows)} seeds, arrival quantities only"),
        "seed_windows": [part["seed_window"] for part in parts],
        "shard_files": [path.name for path in args.shards],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(f"{len(rows)} episodes, {len(arrivals)} arrivals {mine['arrivals_by_generated_act']}, "
          f"{len(cleared)} cleared {mine['cleared_by_generated_act']}")
    print("reproduction:", "MATCH" if reproduction["matches"] else "MISMATCH",
          json.dumps(reproduction, ensure_ascii=False)[:300])
    if not reproduction["matches"]:
        return 1
    for label, block in (("act1-act2", payload["cross_act_arrival_state"]),
                         ("act2 win-loss", payload["act2_win_vs_loss"])):
        for resource, stats in block.items():
            print(f"  {label:14} {resource:16} diff={stats['diff']} "
                  f"ci={stats['bootstrap_ci_95']} p={stats['permutation_p']}")
    for name, block in payload["groups"].items():
        print(f"  group {name}: n={block.get('n')} deck={block.get('deck_size')} "
              f"relic={block.get('relic_count')} maxhp={block.get('player_max_hp')} "
              f"hp={block.get('player_hp')} gold={block.get('gold')} "
              f"boss_decisions={block.get('boss_decisions_median')}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
