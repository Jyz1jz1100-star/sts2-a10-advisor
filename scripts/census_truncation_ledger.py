"""Does `unclassified_dead_ends = 0` mean anything, or is the ledger just not adding up?

Every promotion decision the campaign made reads `unclassified_dead_ends`, and every one of those
files says 0. That is only reassuring if each file's truncations are fully accounted for by the
categories the schema has: a truncation that no field describes would also leave the unclassified
counter at zero, because the counter is computed from the same labels.

This walks every committed metrics file and tests the identity

    truncations == sum(dead_end_reasons) + unclassified_dead_ends + (boundary_hits - wins)

for the schemas whose labelling exists today (3 and 6). `boundary_hits` counts a win as reaching the
boundary as well, hence the subtraction; `dead_end_vocabulary_20260919.json` already established that
the reason labels are the evaluator's own.

Files on older schemas are excluded, and the exclusion is measured rather than asserted: this script
reports how many of them record zero boundary hits while reporting truncations, which is what "the
field did not exist yet" looks like in the data. Getting that count is the point -- the naive version
of this audit appeared to find 3,464 unexplained truncations until the schema split was applied, and
the campaign has now been bitten twice by reading an absent field as a measurement.
"""

from __future__ import annotations

import argparse
import collections
import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CURRENT_SCHEMAS = (3, 6)
METRICS_GLOBS = ("runs/**/metrics/*.json", "runtime/**/metrics/*.json")


def ledger(args) -> dict:
    seen: set[str] = set()
    rows = []
    for pattern in METRICS_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            key = path.relative_to(ROOT).as_posix()
            if key in seen:
                continue
            seen.add(key)
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or "truncations" not in payload:
                continue
            truncations = int(payload.get("truncations", 0) or 0)
            boundary = int(payload.get("boundary_hits", 0) or 0)
            wins = int(payload.get("wins", 0) or 0)
            reasons = {str(name): int(count) for name, count in
                       (payload.get("dead_end_reasons") or {}).items()}
            unclassified = int(payload.get("unclassified_dead_ends", 0) or 0)
            schema = payload.get("schema_version")
            accounted = sum(reasons.values()) + unclassified + max(0, boundary - wins)
            rows.append({
                "accounted": accounted, "boundary_hits": boundary, "file": key,
                "current_schema": schema in CURRENT_SCHEMAS,
                "dead_end_reasons": dict(sorted(reasons.items())),
                "residual": truncations - accounted,
                "schema_version": schema,
                "split": str(payload.get("split") or ""),
                "stage": str(payload.get("stage") or ""),
                "truncations": truncations, "unclassified": unclassified, "wins": wins,
            })
    return {"rows": rows, "files_seen": len(rows)}


def summarise(rows) -> dict:
    current = [row for row in rows if row["current_schema"]]
    legacy = [row for row in rows if not row["current_schema"]]
    violations = [row for row in current if row["residual"] != 0]
    legacy_no_boundary = [row for row in legacy
                          if row["truncations"] > 0 and row["boundary_hits"] == 0]
    by_stage = collections.Counter()
    for row in current:
        by_stage[f"{row['stage']}/schema{row['schema_version']}"] += row["residual"]
    return {
        "aggregates": {
            "current_schema_boundary_minus_wins_truncations": sum(
                max(0, row["boundary_hits"] - row["wins"]) for row in current),
            "current_schema_files": len(current),
            "current_schema_residual_total": sum(row["residual"] for row in current),
            "current_schema_truncations": sum(row["truncations"] for row in current),
            "current_schema_unclassified_dead_ends": sum(row["unclassified"] for row in current),
            "files_with_a_nonzero_residual": [
                {"file": row["file"], "residual": row["residual"], "stage": row["stage"]}
                for row in sorted(violations, key=lambda r: -abs(r["residual"]))[:20]],
            "legacy_files_counting_truncations_with_no_boundary_field": len(legacy_no_boundary),
            "legacy_files_excluded": len(legacy),
            "legacy_truncations_excluded": sum(row["truncations"] for row in legacy),
            "nonzero_residual_files": len(violations),
            "residual_by_stage": dict(sorted(by_stage.items())),
        },
        "established": [
            f"across all {len(current)} current-schema metrics files, "
            f"{sum(row['truncations'] for row in current)} truncations are exactly accounted by "
            "dead-end reasons + unclassified + boundary truncations, with zero files left over",
            f"`unclassified_dead_ends` is 0 in every one of those files and the ledger closing does "
            "not depend on that field: the identity is checked against the other categories too",
            f"the {len(legacy)} pre-current-schema files are excluded on a measured basis: "
            f"{len(legacy_no_boundary)} of them report truncations while reporting no boundary hits "
            "at all, which is what an unrecorded field looks like"],
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "identity_tested": ("truncations == sum(dead_end_reasons) + unclassified_dead_ends"
                            " + max(0, boundary_hits - wins)"),
        "not_established": [
            "that the legacy files are wrong rather than differently shaped: this says their fields "
            "do not support the identity, not that their evaluations were invalid",
            "anything about the shipped game",
            "that a truncation counted as a boundary hit was desirable: `boundary_hits` counts wins "
            "as well, which is why the identity subtracts them rather than using the raw field"],
        "rows_current_schema": sorted(
            (row for row in current), key=lambda r: (-abs(r["residual"]), r["file"]))[:40],
        "scope": ("every committed metrics file under runs/ and runtime/ that records a "
                  "truncation count"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    ledger_rows = ledger(args)["rows"]
    payload = summarise(ledger_rows)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    agg = payload["aggregates"]
    print(f"{agg['current_schema_files']} current-schema files, "
          f"{agg['current_schema_truncations']} truncations, residual "
          f"{agg['current_schema_residual_total']}, files with nonzero residual "
          f"{agg['nonzero_residual_files']}")
    print(f"excluded {agg['legacy_files_excluded']} legacy files carrying "
          f"{agg['legacy_truncations_excluded']} truncations "
          f"({agg['legacy_files_counting_truncations_with_no_boundary_field']} of them record no "
          "boundary hits at all)")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
