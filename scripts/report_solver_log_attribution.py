"""Offline Combat Solver log attribution report (read-only).

For every saved ``godot*.log`` file this script reports whether it can be
bound to a supervisor batch / run by *verifiable* linkage, and otherwise
prints an explicit ``无法归属`` (unattributable) verdict with the concrete
reasons.  Timestamp proximity is never used as attribution evidence.

What the supervisor inventories actually provide is *hash-observation*
semantics, and nothing more:

- ``hash_observed_at_start``: this exact content hash was in the batch's
  ``at_start`` inventory.
- ``hash_observed_at_end_only``: this exact content hash was missing from
  ``at_start`` and present in ``at_end``.  This is an observation of hash
  absence/presence, **not** a creation claim: an existing file whose content
  grew (appended ``godot.log``) changes its hash, and a start snapshot may
  be missing or have refused to hash a file.  Creation time is not
  observable through these inventories.

Neither observation attributes the log to a run: current Combat Solver log
formats carry no run identity, and a run-level binding would require
content-range and run-identity association that no format provides today.
Anything else — including "the log was modified around the same time as a
batch" — stays unattributable.  A missing deploy-log binding must keep the
whole-run assessment failed (``combat_deploy_log_missing``); this report
never upgrades evidence, it only explains why it cannot be attributed.

Example::

    python scripts/report_solver_log_attribution.py \
        --out runs/evidence_attribution_20260907/attribution.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.supervise_solver_batch import (  # noqa: E402
    SOLVER_LOG_GLOB,
    snapshot_solver_logs,
)

DEFAULT_GAME_LOG_DIR = (
    Path.home() / "AppData" / "Roaming" / "SlayTheSpire2" / "logs"
)
DEFAULT_SUPERVISOR_ROOT = PROJECT_ROOT / "runs" / "solver_supervisor"
DEFAULT_GAME_LOGS_SUBDIR = "logs"

# Combat Solver writes a recognisable version marker when it initializes
# (assembly load line ``CombatSolver, Version=0.31.0.0, ...``); the exact
# grammar is versioned in combat_solver/logformat.py, and this report only
# needs the coarse version for the inventory.
_VERSION_RE = re.compile(
    r"CombatSolver(?:, ?Version=|[/ ]v?)(\d+\.\d+\.\d+(?:\.\d+)?)", re.IGNORECASE
)


def _sha256_file(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest().upper()


def _load_manifests(supervisor_root: Path) -> list[dict[str, Any]]:
    manifests: list[dict[str, Any]] = []
    if not supervisor_root.is_dir():
        return manifests
    for batch_dir in sorted(supervisor_root.glob("ssb-*")):
        manifest_path = batch_dir / "manifest.json"
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            manifests.append(payload)
    return manifests


def _batch_inventory_observations(
    log_sha256: str, manifest: dict[str, Any]
) -> list[dict[str, Any]]:
    """Report how one batch's inventories observed a log file hash.

    Pure hash-observation semantics — never a creation or production claim:

    - ``hash_observed_at_start``: this exact content hash was present in the
      batch's ``at_start`` inventory.
    - ``hash_observed_at_end_only``: this exact content hash was absent from
      ``at_start`` and present in ``at_end``.  This must NOT be read as "the
      file was created during the window": an existing file whose content
      grew (e.g. an appended ``godot.log``) changes its hash, and a start
      snapshot may be missing entirely or have refused to hash a file, so
      the hash's absence from ``at_start`` is not evidence of creation.
      Creation time is simply not observable through these inventories.
    """

    logs = manifest.get("combat_solver_logs")
    if not isinstance(logs, dict):
        return []
    at_start = logs.get("at_start")
    at_end = logs.get("at_end")
    in_start = isinstance(at_start, list) and any(
        isinstance(row, dict) and row.get("sha256") == log_sha256
        for row in at_start
    )
    in_end = isinstance(at_end, list) and any(
        isinstance(row, dict) and row.get("sha256") == log_sha256
        for row in at_end
    )
    if in_start:
        return [
            {
                "batch_id": str(manifest.get("batch_id")),
                "observation": "hash_observed_at_start",
            }
        ]
    if in_end:
        return [
            {
                "batch_id": str(manifest.get("batch_id")),
                "observation": "hash_observed_at_end_only",
            }
        ]
    return []


def attribute_log(
    log_row: dict[str, Any],
    manifests: list[dict[str, Any]],
    log_has_run_id: bool,
) -> dict[str, Any]:
    """Return the attribution verdict for one inventoried log file."""

    sha256 = log_row.get("sha256")
    reasons: list[str] = []
    if not sha256:
        reasons.append("log_hash_unavailable")
    if not log_has_run_id:
        reasons.append("log_contains_no_run_identity_binding")
    observations: list[dict[str, Any]] = []
    for manifest in manifests:
        if sha256:
            observations.extend(_batch_inventory_observations(sha256, manifest))
    if observations:
        # Inventory presence is an observation, not an attribution: even a
        # created-during-window log names no run, so nothing can be bound.
        return {
            "attributable": False,
            "verdict": "无法归属到具体运行",
            "observed_in_batch_inventories": observations,
            "run_ids": [],
            "reasons": reasons
            + [
                "inventory observation only; binding to a run would require "
                "content-range and run-identity association inside the log, "
                "which no current format provides"
            ],
        }
    reasons.append(
        "no supervisor batch manifest inventory observed this file's hash"
    )
    return {
        "attributable": False,
        "verdict": "无法归属",
        "observed_in_batch_inventories": [],
        "run_ids": [],
        "reasons": reasons,
        "note": (
            "时间接近不作为归属依据：日志与某批次/运行的时间相邻不构成可验证关联"
        ),
    }


def build_report(
    log_dir: Path, supervisor_root: Path
) -> dict[str, Any]:
    manifests = _load_manifests(supervisor_root)
    inventory = snapshot_solver_logs(log_dir)
    logs: list[dict[str, Any]] = []
    for row in inventory:
        path = Path(row["path"])
        has_run_id = False
        solver_versions: list[str] = []
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if text:
            has_run_id = "run_id" in text
            solver_versions = sorted(
                {match.group(1) for match in _VERSION_RE.finditer(text)}
            )
        entry: dict[str, Any] = {**row, "observed_solver_versions": solver_versions}
        entry.update(attribute_log(entry, manifests, has_run_id))
        logs.append(entry)
    bound_manifests = [
        manifest.get("batch_id")
        for manifest in manifests
        if isinstance(manifest.get("combat_solver_logs"), dict)
    ]
    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "game_log_dir": str(log_dir),
        "supervisor_root": str(supervisor_root),
        "manifests_with_solver_log_snapshots": bound_manifests,
        "policy": {
            "time_proximity_is_not_evidence": True,
            "missing_binding_requires_failed_acceptance": (
                "combat_deploy_log_missing stays a whole-run acceptance blocker"
            ),
        },
        "logs": logs,
        "summary": {
            "total_logs": len(logs),
            "attributable_to_run": sum(
                1 for entry in logs if entry.get("attributable")
            ),
            "unattributable": sum(
                1 for entry in logs if not entry.get("attributable")
            ),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-log-dir", type=Path, default=DEFAULT_GAME_LOG_DIR)
    parser.add_argument("--supervisor-root", type=Path, default=DEFAULT_SUPERVISOR_ROOT)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.out.exists():
        parser.error(
            f"refusing to overwrite existing report: {args.out}; "
            "historical reports are never rewritten"
        )
    report = build_report(args.game_log_dir, args.supervisor_root)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "out": str(args.out),
                "total_logs": report["summary"]["total_logs"],
                "unattributable": report["summary"]["unattributable"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
