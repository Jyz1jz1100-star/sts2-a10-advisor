"""Merge the Act-2 map-choice search over every checkpoint that chains into Act 2.

The frontier artifact names the checkpoints that reach Act 2 at all; this runs
`probe_chained_act2_reachability.py` on each of them and joins the results, so "is the floor-19
dead end avoidable by choosing differently" is answered over the whole chained population rather
than one run, and the answer can be rebuilt with one command.
"""

from __future__ import annotations

import argparse
import collections
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts/probe_chained_act2_reachability.py"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontier", type=Path,
                        default=ROOT / "docs/evidence/chained_frontier_full_20260920.json")
    parser.add_argument("--max-forks", type=int, default=120)
    parser.add_argument("--max-depth", type=int, default=8)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    frontier = json.loads(args.frontier.read_text(encoding="utf-8"))
    chains = [row for row in frontier["chained_runs"] if row.get("chained_into_act_two")]
    if not chains:
        raise SystemExit("the frontier artifact lists no chains into Act 2")
    searches = []
    for row in chains:
        scratch = ROOT / "runtime" / f"reachability_{row['checkpoint_sha256'][:8]}.json"
        process = subprocess.run(
            [sys.executable, str(PROBE), "--checkpoint", row["checkpoint"],
             "--max-forks", str(args.max_forks), "--max-depth", str(args.max_depth),
             "--out", str(scratch)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if not scratch.is_file():
            print(process.stdout[-1500:], process.stderr[-1500:], file=sys.stderr)
            raise SystemExit(f"reachability probe produced no artifact for {row['checkpoint']}")
        if process.returncode != 0:
            raise SystemExit(f"reachability probe reported replay mismatches for {row['checkpoint']}")
        search = json.loads(scratch.read_text(encoding="utf-8"))
        search["frontier_row"] = {key: row[key] for key in
                                  ("steps", "max_floor", "final_player_hp", "run_outcome")}
        searches.append(search)

    floors = sorted({floor for search in searches for floor in search["aggregates"]["act2_floors_reached"]})
    payload = {
        "aggregates": {
            "act2_floors_reached_by_any_route": floors,
            "boss_node_states_observed": sum(
                search["aggregates"]["boss_node_states_observed_in_act2"] for search in searches),
            "chains_searched": len(searches),
            "deepest_act_floor_over_all_routes": max(
                (search["aggregates"]["deepest_act_floor"] for search in searches),
                key=lambda pair: tuple(pair)),
            "engine_map_dead_ends": sum(
                search["aggregates"]["engine_map_dead_ends_found"] for search in searches),
            "map_decisions_expanded": sum(
                search["aggregates"]["map_decisions_expanded"] for search in searches),
            "replay_mismatches": sum(
                search["aggregates"]["replay_mismatches"] for search in searches),
            "routes_by_stop_state": dict(collections.Counter(
                leaf["stop_state"] for search in searches for leaf in search["leaves"])),
            "wins": sum(search["aggregates"]["wins"] for search in searches),
        },
        "frontier_artifact": str(args.frontier.relative_to(ROOT)).replace("\\", "/"),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "limits": {"max_depth": args.max_depth, "max_forks": args.max_forks},
        "not_established": [
            "that a two-act win exists: the trained policy plays every combat, so a reachable floor "
            "is not a survivable floor",
            "routes beyond the depth/fork bounds -- 'search_budget' leaves mark where the search was "
            "cut, not where the map ends",
            "anything about Act 3 (the emulator defines two acts, RunConstants.cs:35-36)"],
        "question": ("can a different Act-2 map choice avoid the floor-19 dead end, and is the Act-2 "
                     "boss node reachable at all?"),
        "searches": searches,
        "scope": "every checkpoint the frontier sweep chained into Act 2, map choices explored breadth-first",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    agg = payload["aggregates"]
    print(f"{agg['chains_searched']} chains, {agg['map_decisions_expanded']} map decisions expanded; "
          f"Act-2 floors reached {agg['act2_floors_reached_by_any_route']}; "
          f"boss states {agg['boss_node_states_observed']}; dead ends {agg['engine_map_dead_ends']}; "
          f"routes {agg['routes_by_stop_state']}; wins {agg['wins']}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
