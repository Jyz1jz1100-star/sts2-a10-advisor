"""Decide whether one live trace is a contract-complete real full run.

This is the gate the project has been missing: "we saw a victory" and "one
continuous, provenance-bound trace walked the whole build-defined progression"
are different claims, and only the second one justifies opening real full-run
training.  Every check is answered from the trace itself, and a check with no
evidence in the stream fails rather than being skipped.

    python scripts/verify_full_run_contract.py runs/.../autoplay_trace.jsonl
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bridge.run_progress import (  # noqa: E402
    REQUIRED_ACTS,
    REQUIRED_BOSSES_PER_ACT,
    RunCoverage,
)

# The Ancients each act can actually present, from the locked build's act models
# (Acts/Overgrowth.cs, Acts/Underdocks.cs, Acts/Hive.cs, Acts/Glory.cs).
VALID_ANCIENTS_BY_ACT = {
    1: {"NEOW", "DARV"},
    2: {"OROBAS", "PAEL", "TEZCATARA", "DARV"},
    3: {"NONUPEIPE", "TANX", "VAKUU", "DARV"},
}
# A run started from the beginning enters act 1 at floor 1; anything deeper means
# the stream was begun mid-run and cannot attest the acts it never carried.
FIRST_FLOOR = 1


def _records(path: Path) -> list[dict[str, Any]]:
    out = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                out.append({"event_type": "corrupt_line"})
    return out


def _actions(records: list[dict[str, Any]]):
    for record in records:
        if record.get("event_type") in ("action", "result") and isinstance(
            record.get("raw"), dict
        ):
            yield record["event_type"], record["raw"]


def _driver_ledgers(records: list[dict[str, Any]]) -> dict[Any, dict[str, Any]]:
    """Index the per-run ledgers the driver wrote when it stopped, by run identity.

    Replaying the state frames rebuilds what happened *to* the run, but not what the
    driver made of it: an unhandled screen posts nothing and refuses nothing, so it
    leaves no mark a replay can find, and a held frame is by definition an action
    that was not taken.  Both live only in the ``session_end`` summary the batch
    writes, which is the second source those two items need -- without it
    ``no_screen_skipped_without_a_rule`` cannot fail, and it duly passed on the
    trace whose last line read ``unhandled_screen ... screen 'event' at act 3 floor 49``.

    A run the driver never recorded is absent here, which is what the items report;
    two runs sharing one identity disqualify the key rather than guess between them.
    """
    ledgers: dict[Any, dict[str, Any]] = {}
    wrote_summary = False
    for record in records:
        if record.get("event_type") != "session_end":
            continue
        wrote_summary = True
        summary = (record.get("raw") or {}).get("summary") or {}
        for run in summary.get("runs") or []:
            if not isinstance(run, dict):
                continue
            key = (tuple(run.get("acts_seen") or ()), run.get("terminal_floor"))
            if key in ledgers:
                del ledgers[key]
            else:
                ledgers[key] = run
    # Two runs sharing one identity is ambiguous, so the key is dropped and both
    # runs are reported as unrecorded rather than matched to whichever came last.
    return ledgers, wrote_summary


def _ledger_key(cov: dict[str, Any]) -> tuple:
    return (tuple(cov.get("acts_seen") or ()), cov.get("terminal_floor"))


def check_run(
    records: list[dict[str, Any]],
    coverage: RunCoverage,
    session: dict[str, Any] | None = None,
    driver_ledger: dict[str, Any] | None = None,
    has_driver_ledgers: bool = False,
) -> dict[str, Any]:
    """Name every contract item for one run's own ledger, pass or fail.

    ``session`` is the stream's provenance record.  A trace file is one process,
    so it is hoisted by the caller and passed to every run in the file: read from
    a run's own segment, only the first run of a batch could ever satisfy it, and
    that is a property of where one record happens to sit rather than of the run.

    ``driver_ledger`` is this run's row of the ``session_end`` summary, matched by
    ``_driver_ledgers``; ``has_driver_ledgers`` says whether the stream carried a
    ``session_end`` at all, which is how a trace stopped mid-batch is told apart
    from a run the driver found nothing to remark on.
    """
    cov = coverage.coverage()
    if session is None:
        session = next(
            (r.get("raw") or {} for r in records if r.get("event_type") == "session"),
            {},
        )
    results = []

    def add(name: str, ok: bool, detail: str) -> None:
        results.append({"check": name, "passed": bool(ok), "detail": detail})

    first_entry = coverage.act_entries.get(1)
    add(
        "starts_at_floor_one",
        # Floor 0 is the run before its first node -- a fresh start observed at
        # Neow -- so it is the earliest evidence there is, not a mid-run join.  A
        # continued run reports the floor it was saved at, which is >= 1, and
        # anything above FIRST_FLOOR still refuses, as intended.
        coverage.started
        and cov["acts_seen"][:1] == [1]
        and isinstance(first_entry, int)
        and first_entry <= FIRST_FLOOR,
        f"first act entry floor={first_entry!r}, acts={cov['acts_seen']}",
    )
    add(
        "three_acts_in_order",
        cov["acts_in_required_progression"] == list(REQUIRED_ACTS)
        and coverage.act_entries.get(2, 0) > coverage.act_entries.get(1, 0)
        and coverage.act_entries.get(3, 0) > coverage.act_entries.get(2, 0),
        f"acts={cov['acts_seen']} entries={ {k: v for k, v in sorted(coverage.act_entries.items())} }",
    )
    ancients_ok = sorted(cov["ancients_covered_acts"]) == list(REQUIRED_ACTS) and all(
        # ``ancients_by_act`` is emitted with str keys because it is a JSON
        # artifact (run_progress.coverage), while REQUIRED_ACTS are ints.  Read
        # with `.get(act)` this lookup returned None for every run and the check
        # could not pass -- the first synthetic trace that walks all three acts
        # is the test that pins it.
        act in VALID_ANCIENTS_BY_ACT
        and str(cov["ancients_by_act"].get(str(act))) in VALID_ANCIENTS_BY_ACT[act]
        for act in REQUIRED_ACTS
    )
    add(
        "three_ancients_are_this_builds",
        ancients_ok,
        f"ancients={cov['ancients_by_act']} expected one per act from {sorted(VALID_ANCIENTS_BY_ACT)}",
    )
    cleared = cov["bosses_cleared"]
    per_act: dict[int, list[str]] = {act: [] for act in REQUIRED_ACTS}
    for node in cleared:
        act = int(node.split(":")[0])
        if act in per_act:
            per_act[act].append(node)
    add(
        "bosses_1_plus_1_plus_2_cleared",
        all(len(per_act[act]) >= REQUIRED_BOSSES_PER_ACT[act] for act in REQUIRED_ACTS),
        f"cleared={ {k: v for k, v in sorted(per_act.items())} } required={REQUIRED_BOSSES_PER_ACT}",
    )
    add(
        "terminal_is_the_games_victory_flag",
        cov["outcome"] is True and cov["outcome_source"] == "bridge_is_victory_flag",
        f"outcome={cov['outcome']!r} source={cov['outcome_source']!r} terminal_floor={cov['terminal_floor']!r}",
    )
    add("run_complete_by_its_own_definition", cov["run_complete"], f"run_complete={cov['run_complete']}")

    posted = [raw for kind, raw in _actions(records) if kind == "action"]
    refused = [raw for kind, raw in _actions(records) if kind == "result" and raw.get("status") != "ok"]
    empty_candidates: dict[str, int] = {}
    for row in cov.get("empty_candidate_refusals", []):
        key = f"{row.get('state_type')}@{row.get('act')}-{row.get('floor')}"
        empty_candidates[key] = empty_candidates.get(key, 0) + 1
    # The holds come off the driver's row, not off the replay: a frame the driver
    # deliberately left alone posted nothing, so the replayed ledger is empty here
    # for every trace on disk, and the counter that exists to keep a clean pass
    # from hiding a run that spent its time holding had nothing to show.
    waits = (driver_ledger or {}).get("waits_for_transition") or cov.get("waits_for_transition")
    add(
        "every_action_was_legal_and_acked",
        bool(posted) and len(refused) == 0,
        f"posted={len(posted)} refused={len(refused)}"
        # A clean pass must not be able to hide a run that spent its time
        # holding, so the holds are printed next to the count that passed.  A
        # screen the candidate contract found nothing to do on is reported the
        # same way: it was retried, and a retry that worked is not a gap that
        # never happened.
        f" waits={waits}"
        f" empty_candidates={empty_candidates}"
        + (f" first_refusals={[r.get('error') for r in refused[:3]]}" if refused else ""),
    )
    # An unhandled screen posts nothing and refuses nothing, so a replay of the
    # states cannot see one; it survives only in the row the driver wrote when the
    # batch stopped.  Union the two, and say so when the stream carried a summary
    # this run is absent from -- a run the driver never described is not a run the
    # driver found clean.
    unhandled: dict[str, list] = {k: list(v) for k, v in cov["unhandled_screens"].items()}
    for screen, frames in ((driver_ledger or {}).get("unhandled_screens") or {}).items():
        known = {json.dumps(f, sort_keys=True) for f in unhandled.get(screen, [])}
        unhandled.setdefault(screen, []).extend(
            f for f in frames if json.dumps(f, sort_keys=True) not in known
        )
    add(
        "no_screen_skipped_without_a_rule",
        not cov["unhandled_screen_bypasses"] and not unhandled
        and (driver_ledger is not None or not has_driver_ledgers),
        f"bypasses={cov['unhandled_screen_bypasses']} unhandled={unhandled}"
        + ("" if driver_ledger is not None or not has_driver_ledgers else " driver_ledger=missing"),
    )
    add(
        "provenance_bound_to_the_locked_build",
        bool(session.get("observed_game")) and bool(session.get("observed_mods"))
        and session.get("execution_owner") == "combat_solver_full_auto",
        f"game={ (session.get('observed_game') or {}).get('version') }"
        f" build={(session.get('observed_game') or {}).get('steam_build_id')}"
        f" mods={session.get('observed_mods')} owner={session.get('execution_owner')!r}"
        f" cohort={session.get('cohort')!r} seed_mode={session.get('seed_mode')!r}",
    )
    sequences = [r.get("sequence") for r in records if isinstance(r.get("sequence"), int)]
    add(
        "stream_is_continuous",
        bool(sequences) and sequences == sorted(sequences)
        and len(set(sequences)) == len(sequences),
        f"records={len(records)} sequenced={len(sequences)}"
        f" monotonic={sequences == sorted(sequences)} unique={len(set(sequences)) == len(sequences)}",
    )
    add(
        "no_corrupt_records",
        not any(r.get("event_type") == "corrupt_line" for r in records),
        f"corrupt={sum(1 for r in records if r.get('event_type') == 'corrupt_line')}",
    )
    return {"coverage": cov, "checks": results, "passed": all(c["passed"] for c in results)}


def audit(path: Path) -> dict[str, Any]:
    records = _records(path)
    stream_session = next(
        (r.get("raw") or {} for r in records if r.get("event_type") == "session"),
        {},
    )
    # Segment on the terminal and on menu visits, exactly as the coverage auditor
    # does, so a run can never borrow evidence from its neighbour.  Records are
    # segmented too: a refusal from one run must not be charged to another, and
    # a clean run must not inherit a neighbour's legality by sharing a file.
    ledgers_list: list[RunCoverage] = [RunCoverage()]
    segments: list[list[dict[str, Any]]] = [[]]
    for record in records:
        current = len(segments) - 1
        segments[current].append(record)
        state = record.get("raw") if record.get("event_type") == "state" else None
        if not isinstance(state, dict):
            continue
        state_type = str(state.get("state_type") or "unknown")
        if ledgers_list[current].closed and state_type in ("menu", "game_over"):
            continue
        if ledgers_list[current].closed:
            ledgers_list.append(RunCoverage())
            segments.append([record])
        ledgers_list[-1].observe(state)
    driver_rows, wrote_summary = _driver_ledgers(records)
    verdicts = [
        check_run(
            segment, ledger, session=stream_session,
            driver_ledger=driver_rows.get(_ledger_key(ledger.coverage())),
            has_driver_ledgers=wrote_summary,
        )
        for segment, ledger in zip(segments, ledgers_list)
        if ledger.acts_seen
    ]
    best = max(verdicts, key=lambda v: sum(c["passed"] for c in v["checks"]), default=None)
    return {
        "schema_version": 1,
        "trace": str(path),
        "trace_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "runs_evaluated": len(verdicts),
        "contract_satisfied": bool(best and best["passed"]),
        "best_run": best,
        "all_runs": [
            {"passed": v["passed"], "failed": [c["check"] for c in v["checks"] if not c["passed"]]}
            for v in verdicts
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("traces", type=Path, nargs="+")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    report = {"schema_version": 1, "traces": [audit(p) for p in args.traces]}
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    for trace in report["traces"]:
        print(f"{trace['trace']}: contract_satisfied={trace['contract_satisfied']}"
              f" runs={trace['runs_evaluated']}")
        best = trace.get("best_run")
        if best:
            for check in best["checks"]:
                print(("  PASS " if check["passed"] else "  FAIL ") + check["check"]
                      + " :: " + check["detail"])
    return 0 if any(t["contract_satisfied"] for t in report["traces"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
