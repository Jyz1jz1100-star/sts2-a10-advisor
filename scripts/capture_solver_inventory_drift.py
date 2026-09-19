"""docs/evidence/solver_inventory_drift_20260919.json -- read-only capture of what blocks the live path.

The live three-act attempt is commonly assumed to be waiting on the game being launched. It is
not, or not only: `scripts/run_solver_comparison.py --dry-run` with the game closed reaches
`verify_solver_inventory` (run_solver_comparison.py:146-175) and stops there, because that gate
re-hashes each required mod DLL against `evaluation_environment.mod_dll_inventory` in
config/combat_solver.lock.json. Two of the three required entries no longer match the files on
disk. This file records the delta; the lock itself is the operator's to reconcile.
"""

import datetime
import hashlib
import json
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]
LOCK = REPO / "config/combat_solver.lock.json"
MANIFESTS = {
    "CombatSolver": (r"3790899961", "CombatSolver.json"),
    "STS2-RitsuLib": (r"3747602295", "mod_manifest.json"),
}
WORKSHOP = pathlib.Path(
    r"G:\SteamLibrary\steamapps\workshop\content\2868840")


def sha256_upper(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


lock = json.loads(LOCK.read_text(encoding="utf-8"))
inventory = (lock.get("evaluation_environment") or {}).get("mod_dll_inventory") or []
history = {(entry.get("version") or entry.get("installed_mod_version")): entry.get("sha256")
           for entry in ((lock.get("solver") or {}).get("version_history") or [])}

entries, failing = [], []
for entry in inventory:
    path = pathlib.Path(entry.get("path") or ".")
    observed = sha256_upper(path) if path.is_file() else None
    expected = str(entry.get("sha256") or "").upper()
    ok = path.is_file() and observed == expected
    record = {
        "mod_id": entry.get("mod_id"),
        "required": bool(entry.get("required")),
        "path": str(path),
        "locked_sha256": expected,
        "observed_sha256": observed,
        "matches": ok,
        "file_mtime": (datetime.datetime.fromtimestamp(path.stat().st_mtime)
                       .isoformat(timespec="seconds") if path.exists() else None),
    }
    entries.append(record)
    if not ok and record["required"]:
        failing.append(record["mod_id"])

observed_versions = {}
for mod_id, (item, manifest_name) in MANIFESTS.items():
    manifest = WORKSHOP / item / manifest_name
    dll = WORKSHOP / item / f"{mod_id}.dll"
    payload = {}
    if manifest.is_file():
        try:
            raw = json.loads(manifest.read_text(encoding="utf-8-sig"))
            payload = {"manifest_version": raw.get("version"),
                       "min_game_version": raw.get("min_game_version")}
        except Exception as error:  # a manifest that will not parse is itself a finding
            payload = {"manifest_error": f"{type(error).__name__}: {error}"}
    if dll.is_file():
        payload["dll_sha256"] = sha256_upper(dll)
        payload["dll_mtime"] = (datetime.datetime.fromtimestamp(dll.stat().st_mtime)
                                .isoformat(timespec="seconds"))
    observed_versions[mod_id] = payload

payload = {
    "_comment": [
        "Read-only. config/combat_solver.lock.json was not modified, and nothing under the Steam",
        "library was written. The dry-run was invoked with --solver-lock against a temporary copy",
        "of git HEAD's lock to test whether restoring the committed version would clear the gate.",
    ],
    "blocking_gate": "verify_solver_inventory (run_solver_comparison.py:146-175): every required "
                     "entry in evaluation_environment.mod_dll_inventory must exist and hash-match",
    "entries": entries,
    "established": [
        f"the live path stops before the bridge is ever contacted: {failing} fail the DLL inventory "
        "gate, so 'launch the game and run the batch' is not yet sufficient",
        "restoring the committed lock does not clear it -- the same dry-run against git HEAD's "
        "version fails identically, so this is not the uncommitted capture's fault",
        f"the install has moved twice since the newest entry the working copy records "
        f"(0.31.0): observed {json.dumps(observed_versions, sort_keys=True)}",
        "STS2_MCP and the optional RegentFX still hash-match, so the inventory is not wholesale "
        "wrong -- exactly the two Workshop-auto-updated mods drifted",
    ],
    "generated_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
    "lock_newest_history_versions": sorted(k for k in history if k),
    "not_established": [
        "which of the drifted versions the current grammar v2 marker vocabulary was last validated "
        "against -- the attestation path (per-batch mod version/hash self-attestation) should be run "
        "against a 0.41.0 session log before any acceptance claim is made on this install",
        "whether the operator wants to re-pin to 0.41.0/0.6.2, downgrade, or run with the gate "
        "widened; that file is operator-owned and was left untouched here",
    ],
    "preconditions_now_missing_for_task_9": [
        "reconcile config/combat_solver.lock.json (operator decision)",
        "re-validate grammar v2 against a log from the 0.41.0 install",
        "launch the game (its process is not running; this agent will not cold-start it)",
        "only then: fixed-seed availability judgement, then the observational batch",
    ],
    "observed_install": observed_versions,
    "scope": "this machine, this install, read-only capture",
}
out = REPO / "docs/evidence/solver_inventory_drift_20260919.json"
out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
               encoding="utf-8")
print("failing required entries:", failing)
print("observed:", json.dumps(observed_versions, sort_keys=True)[:400])
print("lock history versions:", payload["lock_newest_history_versions"])
