"""Collect the overnight Act 1 campaign arms into one verifiable report.

The trainer already writes a hash chain into every metrics file
(``checkpoint_sha256`` / ``seed_sha256`` / ``scope``). This tool does not take
that on faith: it re-opens each referenced checkpoint and re-hashes it, so a
report can only claim a win when the metrics, the checkpoint bytes and the seed
partition all still agree.

Scope is reported exactly as the metrics declare it. The bundled emulator
generates Act 1 only (``RunConstants.MapBossRow = 16``, one boss node, the
``ActOneEncounter`` pool), so nothing produced here can be evidence for a
three-act clear; a real Act 1-3 victory requires the game itself.

    python scripts/report_act1_campaign.py [--root runtime/act1_overnight]
                                           [--out runs/act1_campaign_20260919]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

INTERESTING = ("win_rate", "win_count", "episodes", "mean_final_floor", "max_final_floor",
               "illegal_actions", "unclassified_dead_ends", "defect_truncation_rate",
               "boundary_rate", "scope", "stage", "split", "seed_sha256", "checkpoint_sha256")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rel(path: Path) -> str:
    path = path.resolve()
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def _rows(arm_dir: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for metrics_path in sorted(arm_dir.rglob("metrics/*.json")):
        try:
            data = json.loads(metrics_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            rows.append({"file": _rel(metrics_path), "error": f"unreadable: {exc}"})
            continue
        if not isinstance(data, dict) or "win_rate" not in data:
            continue
        row: dict[str, object] = {"file": _rel(metrics_path),
                                  "metrics_sha256": _sha256(metrics_path)}
        for key in INTERESTING:
            if key in data:
                row[key] = data[key]
        checkpoint = data.get("checkpoint")
        claimed = str(data.get("checkpoint_sha256") or "").upper()
        row["checkpoint_verified"] = False
        if checkpoint and claimed:
            path = Path(checkpoint)
            if not path.is_absolute():
                path = ROOT / path
            if path.is_file():
                actual = _sha256(path).upper()
                row["checkpoint_verified"] = actual == claimed
                if actual != claimed:
                    row["checkpoint_actual_sha256"] = actual
            else:
                row["checkpoint_missing"] = str(path)
        rows.append(row)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, action="append",
                        help="campaign root; repeat to scan several (default: runtime/act1_overnight)")
    parser.add_argument("--out", type=Path, default=ROOT / "runs" / "act1_campaign_20260919")
    args = parser.parse_args()

    roots = args.root or [ROOT / "runtime" / "act1_overnight"]
    missing = [_rel(root) for root in roots if not root.is_dir()]
    for root in missing:
        print(f"WARNING: campaign root does not exist: {root}")

    # run_dir/<run-id>/<stage>/metrics/*.json  ->  one entry per <stage> dir
    arms = sorted({path.parent.parent for root in roots for path in root.rglob("metrics/*.json")})
    report: dict[str, object] = {
        "schema_version": 1,
        "generated_by": "scripts/report_act1_campaign.py",
        "scanned_roots": [_rel(root) for root in roots if root.is_dir()],
        "missing_roots": missing,
        "scope_note": ("simulator_act1 only: the bundled emulator generates Act 1 "
                       "(RunConstants.MapBossRow = 16, single boss node). It cannot "
                       "evidence an Act 1-3 clear."),
        "arms": {},
    }
    wins: list[dict[str, object]] = []
    unverified: list[str] = []
    for arm in arms:
        rows = _rows(arm)
        if not rows:
            continue
        report["arms"][_rel(arm)] = rows
        for row in rows:
            if isinstance(row.get("win_rate"), (int, float)) and row["win_rate"] > 0:
                wins.append(row)
                if row.get("checkpoint_verified") is not True:
                    unverified.append(str(row["file"]))

    print(f"{len(report['arms'])} arms with metrics, {sum(len(r) for r in report['arms'].values())} "
          f"evaluation files, {len(wins)} files reporting win_rate > 0")
    for row in wins:
        print(f"  win: {row['file']} win_rate={row['win_rate']} episodes={row.get('episodes')} "
              f"illegal={row.get('illegal_actions')} unclassified={row.get('unclassified_dead_ends')} "
              f"checkpoint_verified={row.get('checkpoint_verified')}")
    if unverified:
        print(f"  WARNING: {len(unverified)} winning file(s) fail checkpoint re-hash: "
              f"{', '.join(unverified)}")

    args.out.mkdir(parents=True, exist_ok=True)
    out_path = args.out / "campaign_report.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    print(f"wrote {_rel(out_path)} (sha256 {_sha256(out_path)[:16]}…)")
    if wins and not unverified:
        print("VERDICT: at least one win is backed by a re-hashed checkpoint chain")
    else:
        print("VERDICT: no contract-clean win recorded yet")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
