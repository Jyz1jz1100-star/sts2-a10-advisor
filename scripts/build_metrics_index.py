"""Build docs/evidence/metrics_index_20260919.json -- a committed index of the gitignored inputs.

The campaign's claims recompute from metrics files under `runs/` and `runtime/`, and both are
gitignored. That means "reviewable" currently means "reviewable on the machine that ran the
campaign", which is not what the objective asks for. This closes most of the gap without
committing the raw evaluation dumps: one committed row per metrics file, holding its sha256, byte
size, and the fields every aggregate is built from (episodes, wins, truncations, illegal_actions,
rejection_events, dead_end_reasons, unclassified_dead_ends, scope, stage, split, checkpoint digest).

Two things follow from that. The headline numbers become recomputable from committed content alone,
which `claim_metrics_index_aggregates` checks. And anyone who does obtain the original files can
verify this index describes them, which `claim_metrics_index_matches_files` checks on this machine
and a reviewer can run later.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

FIELDS = ("episodes", "wins", "win_rate", "truncations", "illegal_actions",
          "rejection_events", "unclassified_dead_ends", "defect_truncation_rate",
          "mean_final_floor", "max_final_floor")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path,
                        default=ROOT / "docs/evidence/metrics_index_20260919.json")
    args = parser.parse_args()

    import verify_report_claims as V

    rows = []
    for path, payload in V._metrics_payloads():
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            digest = None
        rows.append({
            "path": path.relative_to(ROOT).as_posix(),
            "sha256": digest,
            "bytes": path.stat().st_size,
            "scope": payload.get("scope"),
            "stage": payload.get("stage"),
            "split": payload.get("split"),
            "checkpoint_sha256": payload.get("checkpoint_sha256"),
            "has_rejection_events_field": "rejection_events" in payload,
            "dead_end_reasons": dict(sorted((payload.get("dead_end_reasons") or {}).items())),
            **{field: payload.get(field) for field in FIELDS},
        })
    rows.sort(key=lambda row: row["path"])

    current = [r for r in rows if r["has_rejection_events_field"]]
    vocabulary = Counter()
    for row in rows:
        for name, count in row["dead_end_reasons"].items():
            vocabulary[name] += int(count)
    payload = {
        "_comment": [
            "Committed index of the gitignored metrics files the campaign claims recompute from.",
            "One row per file: its sha256 and byte size plus every field the aggregates are built",
            "from. `runs/` and `runtime/` are gitignored, so the raw dumps are not in the repo;",
            "this makes the aggregate numbers reviewable from committed content, and makes the raw",
            "files verifiable-by-hash for anyone who does obtain them.",
        ],
        "aggregates": {
            "dead_end_vocabulary": dict(sorted(vocabulary.items())),
            "episodes_current_schema": sum(int(r["episodes"] or 0) for r in current),
            "illegal_actions_current_schema": sum(int(r["illegal_actions"] or 0) for r in current),
            "metrics_files": len(rows),
            "metrics_files_current_schema": len(current),
            "metrics_files_legacy_schema": len(rows) - len(current),
            "rejection_events_current_schema": sum(
                int(r["rejection_events"] or 0) for r in current),
            "truncations_total": sum(int(r["truncations"] or 0) for r in rows),
            "unclassified_dead_ends_total": sum(
                int(r["unclassified_dead_ends"] or 0) for r in rows),
        },
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "rows": rows,
        "scope": "every metrics JSON file present on this machine under runs/ and runtime/",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    print(f"{len(rows)} metrics files indexed "
          f"({payload['aggregates']['metrics_files_current_schema']} current schema)")
    print(json.dumps(payload["aggregates"], sort_keys=True))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
