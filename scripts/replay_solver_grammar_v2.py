"""Offline replay of the grammar v2 codec over real CombatSolver journals.

Read-only: it never starts the game, never talks to the bridge, and never
writes into the log directory.  It answers one question - does the grammar v2
reading path in ``combat_solver/logformat.py`` reproduce the solver's own
answers end to end on the sessions this machine actually has - and prints the
counts in the same shape the grammar v1 replay was documented in
(docs/COMBAT_SOLVER.md, docs/COMBAT_SOLVER_0_41_DRIFT_2026-09-19.md).

For every session it also replays the *same decoded lines* through the v1
reader.  That control is the point of the exercise: v1 can be fed
line-per-line and still bind every answer of a battle to turn 1, because the
v2 block no longer tells it which turn it answers.

Usage::

    python scripts/replay_solver_grammar_v2.py \
        --out runs/grammar_v2_replay_20260919/replay.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from combat_solver.logformat import LogTailSource  # noqa: E402
from combat_solver.loggrammar import (  # noqa: E402
    GRAMMAR_V1,
    GRAMMAR_V2,
    decode_v2_record,
    discover_v2_sources,
)

DEFAULT_LOGS_DIR = Path(os.path.expandvars(r"%APPDATA%")) / "SlayTheSpire2" / "logs"
JOURNAL_DIR_NAME = "CombatSolver"
INIT_VERSION_RE = re.compile(r"\bINIT mod=(\d+\.\d+\.\d+)")


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest().upper()


def _inputs(session: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source in discover_v2_sources(session):
        try:
            size = source.path.stat().st_size
            rows.append(
                {
                    "file": source.path.name,
                    "kind": source.kind,
                    "bytes": size,
                    "sha256": _sha256(source.path),
                    "battle_id_claim": source.claimed_battle_id,
                }
            )
        except OSError:
            continue
    return rows


def _mod_version(session: Path) -> str | None:
    process = session / "process.jsonl"
    try:
        blob = process.read_text(encoding="utf-8", errors="replace")[: 256 * 1024]
    except OSError:
        return None
    for line in blob.splitlines():
        try:
            message = json.loads(line)["Message"]
        except (ValueError, KeyError, TypeError):
            continue
        match = INIT_VERSION_RE.search(message)
        if match:
            return match.group(1)
    return None


def _sum_int(stats: dict[str, dict[str, Any]], key: str) -> int:
    return sum(int(file_stats.get(key, 0) or 0) for file_stats in stats.values())


def _sum_counter(stats: dict[str, dict[str, Any]], key: str) -> dict[str, int]:
    merged: Counter[str] = Counter()
    for file_stats in stats.values():
        value = file_stats.get(key)
        if isinstance(value, dict):
            merged.update({str(name): int(count) for name, count in value.items()})
    return dict(sorted(merged.items()))


def _summarize(events: list[Any]) -> dict[str, Any]:
    failures: Counter[str] = Counter()
    snapshots = 0
    deploys = 0
    empty_routes = 0
    turns: list[int] = []
    ranges = 0
    for event in events:
        if event.snapshot is not None:
            snapshots += 1
            turns.append(event.snapshot.battle_turn)
            if not event.snapshot.route:
                empty_routes += 1
        elif event.failure is not None:
            failures[event.failure.reason] += 1
        elif event.deploy is not None:
            deploys += 1
            if event.deploy.log_range is not None:
                ranges += 1
    return {
        "events": len(events),
        "snapshots": snapshots,
        "deploys": deploys,
        "deploys_with_byte_range": ranges,
        "empty_routes": empty_routes,
        "failures": sum(failures.values()),
        "failures_by_reason": dict(sorted(failures.items())),
        "snapshot_turns": sorted(turn for turn in turns if turn is not None),
    }


def _v1_control(session: Path) -> dict[str, Any]:
    """Feed the same decoded logical lines to the v1 reader, one per line."""

    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp)
        out = log_dir / "decoded.log"
        lines = 0
        with out.open("w", encoding="utf-8", newline="\n") as handle:
            for source in discover_v2_sources(session):
                if source.kind != "combat":
                    continue
                with source.path.open("r", encoding="utf-8", errors="replace") as fh:
                    for raw in fh:
                        try:
                            record = decode_v2_record(raw)
                        except Exception:
                            continue
                        for text in record.lines:
                            handle.write(text + "\n")
                            lines += 1
        events = LogTailSource(log_dir, replay=True, grammar=GRAMMAR_V1).poll()
        summary = _summarize(events)
        summary["decoded_lines"] = lines
        return summary


def replay_session(session: Path) -> dict[str, Any]:
    version = _mod_version(session)
    inputs = _inputs(session)
    source = LogTailSource(
        session, mod_version=version, replay=True, grammar=GRAMMAR_V2
    )
    events = source.poll()
    stats = source.v2_stats()
    v2 = _summarize(events)
    v1 = _v1_control(session)
    return {
        "session": session.name,
        "mod_version": version,
        "grammar": GRAMMAR_V2,
        "inputs": inputs,
        "bytes": sum(int(row["bytes"]) for row in inputs),
        "confirmed_battle_ids": source.battle_ids(),
        "records": _sum_int(stats, "records"),
        "logical_lines": _sum_int(stats, "logical_lines"),
        "solver_lines": _sum_int(stats, "solver_lines"),
        "envelope_errors": _sum_int(stats, "envelope_errors"),
        "combat_log_begin": _sum_int(stats, "combat_log_begin"),
        "combat_log_end": _sum_int(stats, "combat_log_end"),
        "combat_log_end_reasons": _sum_counter(stats, "combat_log_end_reasons"),
        "evidence": {
            "route_actions": _sum_int(stats, "route_actions"),
            "route_health": _sum_int(stats, "route_health"),
            "route_replays": _sum_int(stats, "route_replays"),
            "traces": _sum_int(stats, "evidence_traces"),
            "routes_taken_from_evidence": _sum_int(stats, "evidence_adopted"),
            "routes_where_evidence_ambiguous": _sum_int(stats, "evidence_ambiguous"),
            "routes_where_evidence_did_not_match": _sum_int(stats, "evidence_no_match"),
            "answers_without_replay_validation": _sum_int(
                stats, "answers_without_replay_validation"
            ),
            "replay_divergences": _sum_int(stats, "replay_divergences"),
            "unusable_route_actions": _sum_int(stats, "unusable_route_actions"),
            "unmapped_evidence_tags": _sum_int(stats, "unmapped_evidence_tags"),
        },
        "parser": {
            "death_route_answers": _sum_int(stats, "death_route_answers"),
            "suppressed_echoes": _sum_int(stats, "suppressed_echoes"),
            "budget_markers": _sum_int(stats, "budget_markers"),
            "resets": _sum_int(stats, "resets"),
            "unknown_action_kinds": _sum_int(stats, "unknown_action_kinds"),
            "unmapped_v1_failure_markers": _sum_int(
                stats, "unmapped_v1_failure_markers"
            ),
            "error_level_records": _sum_int(stats, "error_level_records"),
            "deploys_without_byte_range": _sum_int(stats, "deploys_without_range"),
        },
        "grammar_v2": v2,
        "grammar_v1_control_on_same_lines": v1,
    }


def _merge_reason_counts(reports: list[dict[str, Any]]) -> dict[str, int]:
    merged: Counter[str] = Counter()
    for row in reports:
        merged.update(row["grammar_v2"]["failures_by_reason"])
    return dict(sorted(merged.items()))


def _turn_collapse(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """How many answers each reader bound to turn 1 of its battle."""

    def bound(rows: list[dict[str, Any]], key: str) -> tuple[int, int]:
        turns = [turn for row in rows for turn in row[key]["snapshot_turns"]]
        return len(turns), sum(1 for turn in turns if turn == 1)

    v2_total, v2_first = bound(reports, "grammar_v2")
    v1_total, v1_first = bound(reports, "grammar_v1_control_on_same_lines")
    return {
        "grammar_v2_answers": v2_total,
        "grammar_v2_answers_bound_to_turn_1": v2_first,
        "grammar_v1_control_answers": v1_total,
        "grammar_v1_control_answers_bound_to_turn_1": v1_first,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs-dir", type=Path, default=DEFAULT_LOGS_DIR)
    parser.add_argument(
        "--out",
        type=Path,
        default=PROJECT_ROOT / "runs" / "grammar_v2_replay_20260919" / "replay.json",
    )
    args = parser.parse_args(argv)

    journal = args.logs_dir / JOURNAL_DIR_NAME
    sessions = sorted(item for item in journal.iterdir() if item.is_dir())
    reports = [replay_session(session) for session in sessions]

    aggregate = {
        "sessions": len(reports),
        "sessions_with_combat_file": sum(
            1
            for row in reports
            if any(item["kind"] == "combat" for item in row["inputs"])
        ),
        "bytes": sum(row["bytes"] for row in reports),
        "records": sum(row["records"] for row in reports),
        "logical_lines": sum(row["logical_lines"] for row in reports),
        "solver_lines": sum(row["solver_lines"] for row in reports),
        "envelope_errors": sum(row["envelope_errors"] for row in reports),
        "events": sum(row["grammar_v2"]["events"] for row in reports),
        "snapshots": sum(row["grammar_v2"]["snapshots"] for row in reports),
        "deploys": sum(row["grammar_v2"]["deploys"] for row in reports),
        "deploys_with_byte_range": sum(
            row["grammar_v2"]["deploys_with_byte_range"] for row in reports
        ),
        "empty_routes": sum(row["grammar_v2"]["empty_routes"] for row in reports),
        "failures": sum(row["grammar_v2"]["failures"] for row in reports),
        "failures_by_reason": _merge_reason_counts(reports),
        "deploys_without_byte_range": sum(
            row["parser"]["deploys_without_byte_range"] for row in reports
        ),
        "evidence_route_adoption": sum(
            row["evidence"]["routes_taken_from_evidence"] for row in reports
        ),
        "evidence_route_ambiguous": sum(
            row["evidence"]["routes_where_evidence_ambiguous"] for row in reports
        ),
        "evidence_route_no_match": sum(
            row["evidence"]["routes_where_evidence_did_not_match"] for row in reports
        ),
        "death_route_answers": sum(row["parser"]["death_route_answers"] for row in reports),
        "suppressed_answer_echoes": sum(
            row["parser"]["suppressed_echoes"] for row in reports
        ),
        "v1_control_turn_collapse": _turn_collapse(reports),
        "v1_control_events": sum(
            row["grammar_v1_control_on_same_lines"]["events"] for row in reports
        ),
        "v1_control_snapshots": sum(
            row["grammar_v1_control_on_same_lines"]["snapshots"] for row in reports
        ),
        "v1_control_deploys": sum(
            row["grammar_v1_control_on_same_lines"]["deploys"] for row in reports
        ),
        "v1_control_failures": sum(
            row["grammar_v1_control_on_same_lines"]["failures"] for row in reports
        ),
        "v1_control_empty_routes": sum(
            row["grammar_v1_control_on_same_lines"]["empty_routes"] for row in reports
        ),
    }
    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "logs_dir": str(args.logs_dir),
        "journal_dir": str(journal),
        "grammar": GRAMMAR_V2,
        "reader": "combat_solver.logformat.LogTailSource(grammar='v2')",
        "control_reader": "combat_solver.logformat.LogTailSource(grammar='v1')",
        "read_only": True,
        "sessions": reports,
        "aggregate": aggregate,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    print(f"grammar v2 replay over {journal}")
    print(
        f"{'session':<14} {'ver':<7} {'bytes':>9} {'recs':>6} {'msgs':>6} "
        f"{'snaps':>5} {'deps':>5} {'empty':>5} {'fails':>5}  reasons"
    )
    for row in reports:
        g2 = row["grammar_v2"]
        print(
            f"{row['session'].split('-')[0]:<14} {row['mod_version'] or '-':<7} "
            f"{row['bytes']:>9} {row['records']:>6} {row['logical_lines']:>6} "
            f"{g2['snapshots']:>5} {g2['deploys']:>5} {g2['empty_routes']:>5} "
            f"{g2['failures']:>5}  {json.dumps(g2['failures_by_reason'])}"
    )
    print(
        "sessions: {sessions}  bytes: {bytes}  records: {records}  "
        "messages: {logical_lines}  events: {events}  snapshots: {snapshots}  "
        "deploys: {deploys} ({deploys_with_byte_range} byte-ranged)  "
        "empty routes: {empty_routes}  typed failures: {failures}  "
        "envelope errors: {envelope_errors}".format(**aggregate)
    )
    print("  failures by reason: " + json.dumps(aggregate["failures_by_reason"]))
    collapse = aggregate["v1_control_turn_collapse"]
    print(
        "turn binding - v2 binds {grammar_v2_answers} answers, "
        "{grammar_v2_answers_bound_to_turn_1} of them to turn 1; "
        "v1 on the same lines binds {grammar_v1_control_answers} answers, "
        "{grammar_v1_control_answers_bound_to_turn_1} to turn 1".format(**collapse)
    )
    print(
        "grammar v1 control on the same decoded lines: "
        "{v1_control_events} events, {v1_control_snapshots} snapshots, "
        "{v1_control_deploys} deploys, {v1_control_failures} failures, "
        "{v1_control_empty_routes} empty routes".format(**aggregate)
    )
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
