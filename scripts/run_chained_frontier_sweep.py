"""Re-run the two-act frontier over *every* Act-1 checkpoint that currently exists on disk.

The 2026-09-19 sweep is the evidence for "0 two-act clears over an exhaustive population", but it was
driven ad hoc: no committed script discovered the population, so the completeness statement could not
be re-checked -- and by now it had quietly gone stale (84 checkpoints then, 102 zips now under an
``act1/checkpoints`` directory). This resolves the population by pattern, deduplicates it by content
digest, calls `probe_chained_act_flow.py` for the rollout rather than re-implementing it, and writes
the frontier artifact.

Exit code is non-zero if any run hits the step cap, because then the deepest floor is a property of
the budget and not a frontier.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts/probe_chained_act_flow.py"
POPULATION_GLOBS = ("runs/*/**/act1/checkpoints/*.zip", "runtime/*/**/act1/checkpoints/*.zip")
BULKY_FIELDS = ("trace", "final_state_info", "action_window")


def discover() -> list[Path]:
    """Every act1 checkpoint zip, deduplicated by content so a copied file counts once."""
    seen: dict[str, Path] = {}
    for pattern in POPULATION_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            seen.setdefault(hashlib.sha256(path.read_bytes()).hexdigest(), path)
    return [seen[d] for d in sorted(seen)]


def run_probe(checkpoints: list[Path], cap: int, out: Path) -> dict:
    command = [sys.executable, str(PROBE), "--max-steps", str(cap), "--out", str(out)]
    for checkpoint in checkpoints:
        command += ["--checkpoint", str(checkpoint)]
    process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                             errors="replace")
    if not out.is_file():
        raise SystemExit(f"probe produced no artifact (exit {process.returncode}):\n"
                         f"{process.stdout[-3000:]}\n{process.stderr[-3000:]}")
    return json.loads(out.read_text(encoding="utf-8"))


def slim(row: dict) -> dict:
    return {key: value for key, value in row.items() if key not in BULKY_FIELDS}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cap", type=int, default=4_000,
                        help="step budget per run; a run that hits it invalidates the frontier claim")
    parser.add_argument("--against", type=Path, default=None,
                        help="previous frontier artifact, to report population growth")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, default=ROOT / "runtime/chained_frontier_probe.json")
    parser.add_argument("--budget_probe", type=Path, default=None,
                        help="probe artifact for the step-cap-limited checkpoint at a larger budget, "
                             "recorded alongside the sweep so the frontier is not budget-limited")
    parser.add_argument("--reuse_scratch", action="store_true",
                        help="aggregate the probe output already in --scratch instead of re-rolling; "
                             "refuses if its checkpoint set does not match the discovered population")
    args = parser.parse_args()

    checkpoints = discover()
    print(f"population: {len(checkpoints)} distinct act1 checkpoints "
          f"({' + '.join(POPULATION_GLOBS)}), rolling each once on the retained-trace seed")
    digests_now = {hashlib.sha256(p.read_bytes()).hexdigest() for p in checkpoints}
    if args.reuse_scratch:
        if not args.scratch.is_file():
            raise SystemExit(f"--reuse_scratch asked for {args.scratch}, which does not exist")
        probed = json.loads(args.scratch.read_text(encoding="utf-8"))
        # Skipped rows carry no digest (the probe never loaded them), so coverage is counted,
        # and the rolled digests must be a subset of what the population holds now.
        rolled_digests = {row["checkpoint_sha256"] for row in probed.get("results", [])}
        seen = len(probed.get("results", [])) + len(probed.get("skipped", []))
        if seen != len(digests_now) or not rolled_digests <= digests_now:
            raise SystemExit(
                f"--reuse_scratch is stale: the scratch covers {seen} checkpoints "
                f"({len(rolled_digests)} of them loaded) against a population of "
                f"{len(digests_now)}; re-run without the flag")
        print("reusing the probe output already on disk (checkpoint set verified identical)")
    else:
        probed = run_probe(checkpoints, args.cap, args.scratch)
    results, skipped = probed.get("results", []), probed.get("skipped", [])
    cap_hits = [r["checkpoint"] for r in results if r["steps"] >= args.cap]
    chained = [r for r in results if r.get("chained_into_act_two")]
    deepest = max((r["max_floor"] for r in results), default=0)
    # The population, not the subset the probe could load: an artifact that lists only what it
    # rolled cannot be used to check that nothing was missed.
    digests = sorted(digests_now)
    rolled_digests = sorted(r["checkpoint_sha256"] for r in results)

    budget_probe = None
    if args.budget_probe and args.budget_probe.is_file():
        probed_rows = json.loads(args.budget_probe.read_text(encoding="utf-8")).get("results", [])
        budget_probe = [
            {key: row[key] for key in ("checkpoint", "checkpoint_sha256", "max_act", "max_floor",
                                       "steps", "run_outcome", "run_won", "illegal_actions",
                                       "chained_into_act_two", "final_player_hp")
             for row in probed_rows}]

    previous_digests: set[str] = set()
    if args.against and args.against.is_file():
        previous = json.loads(args.against.read_text(encoding="utf-8"))
        previous_digests = {row.get("checkpoint_sha256") for row in previous.get("chained_runs", [])}
        previous_digests |= set(previous.get("rolled_digests", []))

    payload = {
        "_comment": [
            "Complete enumeration by pattern, not by hand: the population is whatever matches "
            "POPULATION_GLOBS at run time, deduplicated by content digest.",
            "A checkpoint that cannot be loaded against the V2 observation space is recorded in "
            "'skipped_contract_mismatch', never dropped -- an earlier version of this sweep crashed "
            "on the first of them and produced no artifact at all.",
        ],
        "chained_into_act_two": len(chained),
        "chained_runs": [slim(row) for row in chained],
        "checkpoint_digests": digests,
        "rolled_checkpoint_digests": rolled_digests,
        "checkpoints_found_on_disk": len(results) + len(skipped),
        "frontier": (f"Act 2 floor {deepest}" if any(r["max_act"] > 1 for r in results)
                     else f"Act 1 floor {deepest}") + " (deepest of the population); "
        f"{sum(1 for r in results if r.get('run_won'))} two-act clears",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "illegal_actions_total": sum(r["illegal_actions"] for r in results),
        "max_floor_histogram": dict(sorted(collections.Counter(
            r["max_floor"] for r in results).items())),
        "max_steps_per_run": args.cap,
        "not_established": [
            "a three-act clear: the emulator defines two acts (RunConstants.cs:35-36)",
            "policy transfer: this seed's Act 1 is a scripted trace, so a clear here shows the "
            "harness can walk a two-act flow, not that the policy generalises",
            "anything about the shipped game"],
        "population_discovery_pattern": list(POPULATION_GLOBS),
        "previous_sweep": {
            "file": str(args.against) if args.against else None,
            # The 2026-09-19 artifact recorded digests only for its chained rows, so growth in the
            # population can be counted here but not attributed digest-by-digest to it.
            "caveat": ("the earlier artifact carried no per-checkpoint digest for its rolled-but-not-"
                       "chained rows, so 'digests_absent_from_the_previous_artifact' is a floor on "
                       "the growth, not a census of it"),
            "digests_absent_from_the_previous_artifact":
                sum(1 for d in digests if d and d not in previous_digests),
            "then_chained_rows_carried_digests": len(previous_digests)},
        "results_step_cap_hits": cap_hits,
        "step_cap_budget_probe": budget_probe,
        "rolled": len(results),
        "scope": "simulator chained demo-seed branch; not a three-act clear; not real-game A10 acceptance",
        "seed": "7MS1YN8NWB",
        "skipped_contract_mismatch": len(skipped),
        "skipped_contract_mismatch_rows": [slim(row) for row in skipped],
        "wins_anywhere": sum(1 for r in results if r.get("run_won")),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    print(f"rolled {payload['rolled']}, skipped {len(skipped)}, chained {payload['chained_into_act_two']}, "
          f"wins {payload['wins_anywhere']}, illegal {payload['illegal_actions_total']}")
    print(f"frontier: {payload['frontier']}")
    print(f"wrote {args.out}")
    if cap_hits:
        print(f"STEP CAP HIT by {len(cap_hits)} run(s) -- the deepest floor is budget-limited, "
              "not a frontier", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
