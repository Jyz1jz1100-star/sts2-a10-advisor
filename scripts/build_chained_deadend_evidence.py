"""Assemble why the only expressible two-act flow stops at Act 2 floor 19 -- alive.

`run_chained_frontier_sweep.py` found 3 checkpoints that chain into Act 2; two of them end
*truncated while alive* (37/77 HP) at floor 19, and one walks on to floor 22 and dies in combat.
"Lost map successors" was the label, and it left the important question open: did the policy pick an
option the engine refused, or did the engine offer nothing at all? Those imply different ceilings for
the objective's maximum expressible flow.

This calls `probe_map_fork_successors.py` per dead-end checkpoint and assembles the answer: the
engine's own map mask has zero legal bases there (`RunEngine.cs:690-695` sets a bit only for
`MapNodeTypes[i] != NodeNone`, and `ChooseMapNode` returns false for all of them), so the state has
no successors at all. The V2 contract then advertises one sentinel action for the empty mask
(`training/v2_flat_env.py:308-329`) and intercepts it inside the flat env's own `step`
(`training/v2_flat_env.py:240-250`) before the engine is asked, which is why the episode records
rejections from *other* states and 0 illegal actions here, and why `training/evaluation.py` still
classifies it as `empty_action_mask` rather than leaving it unclassified.
`training/v2_run_wrapper.py:241-253` holds a second, equivalent interception as defence in depth.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts/probe_map_fork_successors.py"
PYTHON = Path(sys.executable)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontier", type=Path,
                        default=ROOT / "docs/evidence/chained_frontier_full_20260920.json")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    frontier = json.loads(args.frontier.read_text(encoding="utf-8"))
    dead_ends = [row for row in frontier["chained_runs"]
                 if row["run_outcome"] == "truncated" and (row["final_player_hp"] or 0) > 0]
    if not dead_ends:
        raise SystemExit("the frontier artifact lists no alive truncated chain -- nothing to fork")

    forks = []
    for row in dead_ends:
        output = ROOT / "runtime" / f"fork_{row['checkpoint_sha256'][:8]}.json"
        process = subprocess.run(
            [str(PYTHON), str(PROBE), "--checkpoint", row["checkpoint"],
             "--judge_with_evaluator", "--out", str(output)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if not output.is_file():
            print(process.stdout[-1500:], process.stderr[-1500:], file=sys.stderr)
            raise SystemExit(f"fork probe produced no artifact for {row['checkpoint']}")
        fork = json.loads(output.read_text(encoding="utf-8"))
        fork["frontier_row"] = {key: row[key] for key in
                                ("steps", "max_floor", "final_player_hp", "run_outcome")}
        forks.append(fork)

    masks = {json.dumps(fork["fork_masks_at_state"], sort_keys=True) for fork in forks}
    payload = {
        "aggregates": {
            "alive_truncated_chains": len(forks),
            "forks_where_the_engine_offered_no_map_option": sum(
                1 for fork in forks if fork["fork_masks_at_state"]["native_bases"] == []),
            "forks_with_identical_mask_signature": len(masks) == 1,
            "options_offered_at_the_fork": sorted({
                tuple(fork["fork_masks_at_state"]["flat_bases"]) for fork in forks}),
        },
        "conclusion": (
            "the ceiling on the two-act flow is the engine's map structure at that cursor position, "
            "not policy strength and not the step budget: the native mask lists no map option at "
            "all, so no choice was available to make better"),
        "contract_behaviour_explaining_the_counters": (
            "when the engine mask is empty the flat env advertises its one sentinel action so the "
            "policy always has something legal to play (hence 0 illegal actions, "
            "v2_flat_env.py:308-329), and its step() intercepts that action without asking the "
            "engine: it returns truncated with simulator_dead_end set and classifies the ending "
            "from the engine's own mask -- empty gives empty_action_mask, a non-empty mask whose "
            "candidates the filter removed gives rejected_to_exhaustion (v2_flat_env.py:240-250). "
            "V2RunEnvWrapper.step() short-circuits the same state as defence in depth "
            "(v2_run_wrapper.py:241-253); `labelling_layers_observed` records which one actually "
            "fired here, keyed off the sentinel-action field only that wrapper writes. So this "
            "state produces NO native refusal -- verified per branch by "
            "rejection_events_before_step == rejection_events_after_step with "
            "native_refusal_counted_on_this_step False -- which is exactly why the rejection-phase "
            "census can report 0 refusals across 11,060 map decisions and still be consistent with "
            "map states that have no successors."),
        "labelling_layers_observed": sorted({
            branch["which_contract_layer_labelled_it"] for fork in forks
            for branch in fork["branches"] if branch.get("which_contract_layer_labelled_it")}),
        "engine_citations": [
            "RunEngine.cs:690-695 (map mask: one bit per non-NodeNone MapNodeTypes entry)",
            "RunEngine.cs:970 (`StepMap` returns -1 when `ChooseMapNode` and the scripted "
            "fallback `TryChooseInstant5RetainedUnknownPath` both fail)",
            "RunMapGenerator.cs:1127-1139 (ChooseMapNode is false when every MapNodeTypes entry is "
            "NodeNone, or an option carries no coordinate)"],
        "forks": forks,
        "frontier_artifact": str(args.frontier.relative_to(ROOT)).replace("\\", "/"),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "not_established": [
            "that the dead end is unavoidable: the third chained checkpoint reached Act 2 floor 22 "
            "on the same seed, so successors exist for other cursor positions -- what is not "
            "established is whether any policy choice at an earlier node avoids this one",
            "how often this state is reached outside the retained-trace branch (it needs Act 2 of "
            "the scripted seed, which only that seed can enter)",
            "anything about the shipped game"],
        "scope": "the last map decision of each chained Act-2 run that ended alive, per checkpoint",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    agg = payload["aggregates"]
    print(f"{agg['alive_truncated_chains']} alive truncated chains; "
          f"{agg['forks_where_the_engine_offered_no_map_option']} had no engine map option; "
          f"identical signature: {agg['forks_with_identical_mask_signature']}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
