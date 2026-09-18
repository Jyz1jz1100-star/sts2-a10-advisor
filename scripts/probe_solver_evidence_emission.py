"""Measure when CombatSolver 0.41.0 actually publishes route evidence.

Read-only: it never starts the game, never talks to the bridge, and never writes
into the log directory.  It answers one question, which is the question the
grammar v2 evidence contract turns on:

    does every solver answer carry its own ``ROUTE_ACTION`` records, or only the
    first answer of a combat?

For every combat journal it walks the records in order and, for each ``RESULT``
record, counts how many ``ROUTE_ACTION`` records appeared since the previous
``RESULT``.  It cross-tabs that against the record's own ``reused=`` flag, so the
answer is not "how many traces exist" but "which answers own a trace window".

The conclusion recorded in docs/GRAMMAR_V2_TODO_2026-09-19.md (10 of 10 combats
with RESULT records: one burst, inside the first non-reused answer's window, zero
for every reused answer) is reproducible with this command:

    python scripts/probe_solver_evidence_emission.py            # default log dir
    python scripts/probe_solver_evidence_emission.py --log-dir <path> --limit 20
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

DEFAULT_LOG_DIR = Path(os.path.expandvars(r"%APPDATA%")) / "SlayTheSpire2" / "logs" / "CombatSolver"

_PREFIX_WORD_RE = re.compile(r"^\[CombatSolver/\w+\]\s+(\w+)")
_TRACE_RE = re.compile(r'"traceId"\s*:\s*"([0-9a-fA-F]{6,})"')
_REUSED_RE = re.compile(r"\breused=(\w+)")


def summarise(path: Path) -> dict[str, object]:
    records_since_result = 0
    actions_per_result: list[int] = []
    reused_by_own_window: Counter[tuple[bool, bool]] = Counter()
    traces: set[str] = set()
    replays = 0
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                message = json.loads(line).get("Message")
            except json.JSONDecodeError:
                continue
            if not isinstance(message, str):
                continue
            match = _PREFIX_WORD_RE.match(message)
            if match is None:
                continue
            word = match.group(1)
            if word == "ROUTE_ACTION":
                records_since_result += 1
                trace = _TRACE_RE.search(message)
                if trace:
                    traces.add(trace.group(1).lower())
            elif word == "ROUTE_REPLAY":
                replays += 1
            elif word == "RESULT":
                reused = (_REUSED_RE.search(message) or [None, "?"])[1] == "True"
                actions_per_result.append(records_since_result)
                reused_by_own_window[(reused, records_since_result > 0)] += 1
                records_since_result = 0
    return {"actions_per_result": actions_per_result,
            "reused_by_own_window": dict(reused_by_own_window),
            "traces": len(traces), "replays": replays}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--limit", type=int, default=0, help="newest N journals; 0 means all")
    args = parser.parse_args()

    if not args.log_dir.is_dir():
        print(f"no journal tree at {args.log_dir}", file=sys.stderr)
        return 2
    journals = sorted(args.log_dir.rglob("combat-*.jsonl"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    if args.limit:
        journals = journals[: args.limit]

    verdict: Counter[str] = Counter()
    with_results = 0
    for journal in journals:
        try:
            summary = summarise(journal)
        except OSError as exc:
            print(f"{journal.parent.name[:6]}/{journal.name[7:15]}: unreadable ({exc})")
            continue
        per_result = summary["actions_per_result"]
        if not per_result:
            continue
        with_results += 1
        burst = [index for index, count in enumerate(per_result) if count > 0]
        reused_map = summary["reused_by_own_window"]
        every_reused_window_empty = not any(own and reused for reused, own in reused_map)
        verdict[f"{len(burst)} window(s) with records"] += 1
        print(f"{journal.parent.name[:6]}/{journal.name[7:15]} results={len(per_result)} "
              f"record_counts_per_result={per_result} traces={summary['traces']} "
              f"replays={summary['replays']} reusedxOwnWindow={reused_map} "
              f"all_reused_windows_empty={every_reused_window_empty}")

    print(f"\n{with_results} of {len(journals)} journals contain RESULT records; "
          f"windows carrying ROUTE_ACTION records per journal: {dict(verdict)}")
    if with_results == 0:
        print("VERDICT: no evidence -- no journal here has a RESULT record")
        return 1
    single = verdict.get("1 window(s) with records", 0)
    print(f"VERDICT: {single}/{with_results} journals publish evidence in exactly one window")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
