"""Capture the solver's own pause evidence before the mod deletes the file it lives in.

Measured on 2026-10-07: the battle log that carried the 13:08:14Z `DEPLOY_CHOICE_PAUSED` line was
gone from the log directory ~11 minutes later, so a bug report that cites a *path* becomes
unverifiable within the same session. The line itself survives in two places -- the batch manifest
that detected it, and (for the earlier instance) a battle log file that has not been rotated yet.
This script copies both verbatim into a committed artifact, and records the rotation it observed.

    .tools/python/.../python.exe scripts/capture_solver_pause_evidence.py \
        --out docs/evidence/solver_pause_20261007.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

#: Where the mod writes its own per-process log directories.
LOG_ROOT = pathlib.Path(os.path.expanduser("~")) / "AppData" / "Roaming" / \
    "SlayTheSpire2" / "logs" / "CombatSolver"
PAUSE_MARKERS = ("DEPLOY_CHOICE_PAUSED", "NativeChoiceSurfaceMismatch")


def _utc(ms: float | None) -> str | None:
    if not isinstance(ms, (int, float)):
        return None
    return dt.datetime.fromtimestamp(ms / 1000.0, dt.timezone.utc).isoformat()


def scan_live_logs() -> tuple[list[dict], list[dict]]:
    """Every pause line still on disk, plus the files that no longer exist.

    Returns (events, referenced_but_gone). A pause whose file has already been rotated away is
    still an event -- it just carries `file_exists: false`, which is the finding about the mod's
    retention, not a reason to drop the record.
    """
    events: list[dict] = []
    if not LOG_ROOT.is_dir():
        return events, []
    for path in sorted(LOG_ROOT.glob("*/*.jsonl")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip() or not any(marker in line for marker in PAUSE_MARKERS):
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            message = str(record.get("Message") or "")
            request = _preceding_request(path, record.get("Time"))
            events.append({
                "time_ms": record.get("Time"),
                "time_utc": _utc(record.get("Time")),
                "level": record.get("Level"),
                "message": message,
                "exception_class": (re.search(r"exception=([\w\.]+)", message) or
                                    [None, None])[1],
                "turn": (re.search(r"\bturn=(\d+)", message) or [None, None])[1],
                "log_file": str(path.relative_to(LOG_ROOT)),
                "file_exists": path.exists(),
                "native_choice_request": request,
            })
    return events, []


def _preceding_request(path: pathlib.Path, time_ms: object) -> dict | None:
    """The mod's own request record just before the pause -- the line that carries the arity.

    The pause exception is a claim about a surface; the `NATIVE_CHOICE_REQUEST` line is the mod's
    own description of that surface (`surface=`, `options=`, `select=`), and the two disagreeing is
    the most useful thing in this file. It is deliberately a *separate* scan: the battle may be
    logged in another file than the pause, so callers can pass a directory instead.
    """
    return _latest_matching(path, "NATIVE_CHOICE_REQUEST", time_ms)


def _latest_matching(path: pathlib.Path, marker: str, time_ms: object) -> dict | None:
    best: tuple[float, str] | None = None
    try:
        rows = [json.loads(l) for l in
                path.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
    except ValueError:
        return None
    for record in rows:
        message = str(record.get("Message") or "")
        if marker not in message:
            continue
        stamp = record.get("Time")
        if not isinstance(stamp, (int, float)) or not isinstance(time_ms, (int, float)):
            continue
        if stamp <= time_ms and (best is None or stamp > best[0]):
            best = (stamp, message)
    if best is None:
        return None
    return {"time_utc": _utc(best[0]), "message": best[1]}


def from_batch_manifests() -> list[dict]:
    """Pause records the supervisor already stored, verbatim, in its own manifest.

    These survive log rotation because they are ours, and `runs/` is gitignored -- which is exactly
    why they are copied into a committed artifact here.
    """
    found: list[dict] = []
    base = ROOT / "runs" / "solver_supervisor"
    if not base.is_dir():
        return found
    for manifest in sorted(base.glob("*/manifest.json")):
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        for event in payload.get("solver_pause") or []:
            found.append({**event, "batch_id": manifest.parent.name})
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    args = parser.parse_args()

    live, _ = scan_live_logs()
    mine = from_batch_manifests()
    referenced = {e.get("file") for e in mine}
    live_files = {e["log_file"].split("\\")[-1] for e in live}
    gone = sorted(str(name) for name in referenced if name and name not in live_files)

    payload = {
        "schema_version": 1,
        "generated_by": "scripts/capture_solver_pause_evidence.py",
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "how_to_recheck": ("python scripts/capture_solver_pause_evidence.py "
                           "--out docs/evidence/solver_pause_20261007.json"),
        "log_root": str(LOG_ROOT),
        "pause_events_still_on_disk": live,
        "pause_events_recorded_by_our_batches": mine,
        "mod_log_files_already_rotated_away": gone,
        "not_established": [
            "that the mod deletes battle logs on a schedule: the observation is that a named "
            "battle log present minutes after the pause was absent ~11 minutes later, in the same "
            "client process directory",
            "that a full-auto re-toggle clears every pause class -- the one recovered instance was "
            "cleared by an operator click, not by an automated one",
        ],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    print(json.dumps({
        "pause_events_on_disk": len(live),
        "pause_events_in_our_manifests": len(mine),
        "rotated_away": gone,
        "out": str(args.out),
    }, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
