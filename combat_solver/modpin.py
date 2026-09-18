"""Per-batch attestation of the mod binaries a live run actually used.

The version locks only prove what was on disk when somebody last looked. Steam
updates Workshop mods on its own - CombatSolver moved through eight versions in
seven days (0.35.5 -> 0.41.0) while ``combat_solver.lock.json`` still pinned
0.31.0 - so a batch has to record the mods it *really* ran against, at both ends
of its own window, and invalidate itself if they moved.

Nothing here mutates or disables anything: it reads files. In particular it does
not touch the solver's own settings; full_auto ownership is untouched.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MANIFEST_FIELDS = ("version", "id", "name")


@dataclass(frozen=True)
class ModRecord:
    mod_id: str
    path: str
    expected_sha256: str
    actual_sha256: str | None
    version: str | None
    error: str | None = None

    @property
    def matches_lock(self) -> bool:
        return (
            self.error is None
            and self.actual_sha256 is not None
            and self.actual_sha256 == self.expected_sha256.upper()
        )

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "mod_id": self.mod_id,
            "path": self.path,
            "expected_sha256": self.expected_sha256,
            "actual_sha256": self.actual_sha256,
            "mod_manifest_version": self.version,
            "matches_lock": self.matches_lock,
        }
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass
class ModAttestation:
    """A measurement of the locked mod set at one instant."""

    records: list[ModRecord] = field(default_factory=list)

    @property
    def drifted(self) -> list[str]:
        return [r.mod_id for r in self.records if not r.matches_lock]

    @property
    def unreadable(self) -> list[str]:
        return [r.mod_id for r in self.records if r.error]

    @property
    def all_match_lock(self) -> bool:
        return bool(self.records) and not self.drifted

    def by_id(self) -> dict[str, str | None]:
        return {r.mod_id: r.actual_sha256 for r in self.records}

    def moved_since(self, earlier: "ModAttestation") -> list[str]:
        """Mods whose bytes differ between two measurements (including vanishing)."""
        before = earlier.by_id()
        return sorted(
            r.mod_id for r in self.records if before.get(r.mod_id) != r.actual_sha256
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "records": [r.to_json() for r in self.records],
            "drifted_from_lock": self.drifted,
            "unreadable": self.unreadable,
            "all_match_lock": not self.drifted,
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _sibling_manifest(path: Path) -> str | None:
    """Read the mod's own manifest version, if a JSON sidecar exists."""
    for candidate in (path.with_suffix(".json"), path.parent / "mod_manifest.json"):
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return None
        if isinstance(data, dict) and data.get("version") is not None:
            return str(data["version"])
    return None


def attest_lock_mods(lock_path: Path | str) -> ModAttestation:
    """Measure every ``mod_dll_inventory`` entry the lock declares.

    Fails closed: a missing or unreadable DLL is reported as an error record, not
    as a match, and never silently dropped from the inventory.
    """
    path = Path(lock_path)
    attestation = ModAttestation()
    lock = json.loads(path.read_text(encoding="utf-8"))
    inventory = (lock.get("evaluation_environment") or {}).get("mod_dll_inventory") or []
    for entry in inventory:
        mod_id = str(entry.get("mod_id") or "?")
        declared = str(entry.get("path") or "")
        expected = str(entry.get("sha256") or "").upper()
        target = Path(declared)
        if not declared:
            attestation.records.append(
                ModRecord(mod_id, declared, expected, None, None, error="no_path_declared")
            )
            continue
        try:
            actual = _sha256(target) if target.is_file() else None
        except OSError as exc:
            attestation.records.append(
                ModRecord(mod_id, declared, expected, None, None, error=f"unreadable: {exc}")
            )
            continue
        if actual is None:
            attestation.records.append(
                ModRecord(mod_id, declared, expected, None, None, error="missing_file")
            )
            continue
        attestation.records.append(
            ModRecord(mod_id, declared, expected, actual, _sibling_manifest(target))
        )
    return attestation
