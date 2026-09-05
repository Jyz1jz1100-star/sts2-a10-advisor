"""Auditable fixed-seed allocations and crash-safe consumption ledger.

The game seed is an opaque alphanumeric identifier.  This module therefore
keeps two values for every allocation entry: ``raw_seed`` is the exact value
registered by the caller and ``canonical_seed`` is only the v0.111.0 display
normalisation used for comparisons.  No seed is converted to an integer.

The ledger is intentionally small and append-like.  ``next_index`` advances
only when a started run has been verified by the bridge.  A run is first
written as ``active`` before the POST which starts it; if the process dies
after that reservation, a later process may reconcile the reservation only
when it is explicitly given this ledger and an authoritative current run with
the same seed.  A missing or conflicting run never causes a retry, skip,
abandon, or overwrite.  The supervisor itself does not resume comparison
records; its default ledger lives in the new batch directory.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from bridge.trace_controller import BridgeProtocolError, validate_requested_seed


class SeedAllocationError(BridgeProtocolError):
    """The allocation or its consumption ledger is unsafe to use."""


class SeedAllocationExhausted(SeedAllocationError):
    """All registered seeds have been consumed; random fallback is forbidden."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise SeedAllocationError(f"cannot read seed allocation {path}: {exc}") from exc
    return digest.hexdigest().upper()


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Persist a ledger without exposing a partially written JSON file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


@dataclass(frozen=True)
class SeedEntry:
    """One ordered, pre-registered game seed."""

    index: int
    raw_seed: str
    canonical_seed: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "raw_seed": self.raw_seed,
            "canonical_seed": self.canonical_seed,
        }


@dataclass(frozen=True)
class SeedAllocation:
    """Validated allocation file and its immutable content hash."""

    path: Path
    sha256: str
    entries: tuple[SeedEntry, ...]
    schema_version: int
    allocation_kind: str
    partition: dict[str, Any] | None = None

    @property
    def count(self) -> int:
        return len(self.entries)

    def describe(self) -> dict[str, Any]:
        """Return manifest-safe allocation provenance."""

        result: dict[str, Any] = {
            "path": str(self.path),
            "sha256": self.sha256,
            "schema_version": self.schema_version,
            "allocation_kind": self.allocation_kind,
            "count": self.count,
            "entries": [entry.as_dict() for entry in self.entries],
        }
        if self.partition is not None:
            result["partition"] = dict(self.partition)
        return result


def _raw_seed(value: Any, index: int) -> str:
    # Existing checked-in allocations use JSON integers.  Representing that
    # value as its decimal wire string is the only compatibility conversion;
    # alphanumeric strings are retained byte-for-byte and never normalised.
    if isinstance(value, bool):
        raise SeedAllocationError(f"seed[{index}] must not be boolean")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        if not value or value != value.strip():
            raise SeedAllocationError(
                f"seed[{index}] must be a non-empty string without surrounding whitespace"
            )
        return value
    raise SeedAllocationError(
        f"seed[{index}] must be an ASCII alphanumeric string or integer"
    )


def load_seed_allocation(path: Path | str) -> SeedAllocation:
    """Load and validate a pre-registered allocation in deterministic order."""

    allocation_path = Path(path).expanduser().resolve()
    try:
        payload = json.loads(allocation_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise SeedAllocationError(
            f"cannot parse seed allocation {allocation_path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise SeedAllocationError("seed allocation must be a JSON object")
    if payload.get("schema_version") != 1:
        raise SeedAllocationError("unsupported seed allocation schema_version")
    if payload.get("allocation_kind") != "run_seed":
        raise SeedAllocationError("seed allocation kind must be 'run_seed'")
    seeds = payload.get("seeds")
    if not isinstance(seeds, list) or not seeds:
        raise SeedAllocationError("seed allocation must contain a non-empty seeds list")

    entries: list[SeedEntry] = []
    seen: dict[str, int] = {}
    for index, value in enumerate(seeds):
        raw = _raw_seed(value, index)
        try:
            canonical = validate_requested_seed(raw)
        except (TypeError, ValueError) as exc:
            raise SeedAllocationError(f"invalid seed[{index}] {raw!r}: {exc}") from exc
        if canonical in seen:
            raise SeedAllocationError(
                f"duplicate canonical seed {canonical!r} at indices {seen[canonical]} and {index}"
            )
        seen[canonical] = index
        entries.append(SeedEntry(index, raw, canonical))

    partition_value = payload.get("partition")
    partition: dict[str, Any] | None = None
    if partition_value is not None:
        if not isinstance(partition_value, dict):
            raise SeedAllocationError("partition must be a JSON object")
        partition = dict(partition_value)
        count = partition.get("count")
        if count is not None:
            if isinstance(count, bool) or not isinstance(count, int) or count != len(entries):
                raise SeedAllocationError(
                    f"partition.count {count!r} does not match {len(entries)} seeds"
                )
        start = partition.get("start")
        if start is not None:
            if isinstance(start, bool) or not isinstance(start, int) or start < 0:
                raise SeedAllocationError("partition.start must be a non-negative integer")
            # Validate the existing numeric partition when it declares one;
            # do not invent a numeric interpretation for alphanumeric seeds.
            if all(entry.raw_seed.isdecimal() for entry in entries):
                expected = [str(start + index) for index in range(len(entries))]
                if [entry.raw_seed for entry in entries] != expected:
                    raise SeedAllocationError(
                        "numeric partition does not match the ordered seeds list"
                    )

    return SeedAllocation(
        path=allocation_path,
        sha256=_sha256_file(allocation_path),
        entries=tuple(entries),
        schema_version=1,
        allocation_kind="run_seed",
        partition=partition,
    )


def _allocation_fingerprint(allocation: SeedAllocation) -> dict[str, Any]:
    return {
        "path": str(allocation.path),
        "sha256": allocation.sha256,
        "schema_version": allocation.schema_version,
        "allocation_kind": allocation.allocation_kind,
        "count": allocation.count,
        "entries": [entry.as_dict() for entry in allocation.entries],
    }


class SeedLedger:
    """Crash-safe ordered consumption ledger for one allocation file."""

    SCHEMA_VERSION = 1

    def __init__(
        self,
        allocation: SeedAllocation,
        path: Path | str | None = None,
        *,
        batch_dir: Path | str | None = None,
        create: bool = True,
    ) -> None:
        self.allocation = allocation
        self.batch_dir = (
            Path(batch_dir).expanduser().resolve() if batch_dir is not None else None
        )
        if path is None and self.batch_dir is None:
            # A ledger is durable state, so silently putting it beside the
            # checked-in allocation would make two otherwise independent
            # batches consume one another's seeds.  Callers must either give
            # the exact ledger path or identify the owning batch directory.
            raise SeedAllocationError(
                "seed ledger path is required; pass a batch-local path or batch_dir"
            )
        expected_path = (
            self.batch_dir / "seed_allocation.ledger.json"
            if self.batch_dir is not None
            else None
        )
        resolved_path = (
            Path(path).expanduser().resolve() if path is not None else expected_path
        )
        assert resolved_path is not None
        if expected_path is not None and resolved_path != expected_path:
            raise SeedAllocationError(
                "seed ledger path must be the batch-local seed_allocation.ledger.json"
            )
        self.path = resolved_path
        if self.path.exists():
            self._read()
        elif create:
            _atomic_write_json(self.path, self._initial_state())
        else:
            raise SeedAllocationError(f"seed ledger does not exist: {self.path}")

    def _initial_state(self) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "allocation": _allocation_fingerprint(self.allocation),
            "next_index": 0,
            "active": None,
            "consumed": [],
        }

    def _read_raw(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise SeedAllocationError(f"cannot read seed ledger {self.path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise SeedAllocationError("seed ledger must be a JSON object")
        return payload

    def _read(self) -> dict[str, Any]:
        payload = self._read_raw()
        if payload.get("schema_version") != self.SCHEMA_VERSION:
            raise SeedAllocationError("unsupported seed ledger schema_version")
        if payload.get("allocation") != _allocation_fingerprint(self.allocation):
            raise SeedAllocationError(
                "seed ledger allocation fingerprint differs from --seed-file"
            )
        next_index = payload.get("next_index")
        if isinstance(next_index, bool) or not isinstance(next_index, int):
            raise SeedAllocationError("seed ledger next_index must be an integer")
        if next_index < 0 or next_index > self.allocation.count:
            raise SeedAllocationError("seed ledger next_index is outside the allocation")
        consumed = payload.get("consumed")
        if not isinstance(consumed, list) or len(consumed) != next_index:
            raise SeedAllocationError(
                "seed ledger consumed entries must exactly cover next_index"
            )
        for index, record in enumerate(consumed):
            self._validate_record(record, index, require_run_id=True)
        active = payload.get("active")
        if active is not None:
            self._validate_record(active, next_index, require_run_id=False)
            if next_index >= self.allocation.count:
                raise SeedAllocationError("seed ledger cannot reserve past allocation end")
        return payload

    def _entry(self, index: int) -> SeedEntry:
        if not 0 <= index < self.allocation.count:
            raise SeedAllocationError(f"seed index {index} is outside allocation")
        return self.allocation.entries[index]

    def _validate_record(
        self, record: Any, expected_index: int, *, require_run_id: bool
    ) -> None:
        if not isinstance(record, dict):
            raise SeedAllocationError("seed ledger record must be an object")
        entry = self._entry(expected_index)
        if record.get("index") != expected_index:
            raise SeedAllocationError("seed ledger records are not contiguous")
        if record.get("raw_seed") != entry.raw_seed:
            raise SeedAllocationError("seed ledger raw seed differs from allocation")
        if record.get("canonical_seed") != entry.canonical_seed:
            raise SeedAllocationError("seed ledger canonical seed differs from allocation")
        if require_run_id:
            run_id = record.get("run_id")
            if not isinstance(run_id, str) or not run_id.strip():
                raise SeedAllocationError("consumed seed record has no run_id")
        elif "run_id" in record and record["run_id"] is not None:
            if not isinstance(record["run_id"], str) or not record["run_id"].strip():
                raise SeedAllocationError("active seed record has an invalid run_id")

    def snapshot(self) -> dict[str, Any]:
        """Return validated ledger state plus current allocation provenance."""

        state = self._read()
        return {
            "path": str(self.path),
            "allocation": self.allocation.describe(),
            "next_index": state["next_index"],
            "active": dict(state["active"]) if isinstance(state.get("active"), dict) else None,
            "consumed": [dict(record) for record in state["consumed"]],
            "exhausted": state["next_index"] >= self.allocation.count
            and state.get("active") is None,
        }

    def reserve_next(self) -> SeedEntry:
        """Reserve exactly the next seed before attempting a start POST."""

        state = self._read()
        if state.get("active") is not None:
            raise SeedAllocationError(
                "seed ledger has an unresolved active reservation; reconcile it before starting"
            )
        index = state["next_index"]
        if index >= self.allocation.count:
            raise SeedAllocationExhausted(
                f"fixed seed allocation exhausted after {self.allocation.count} runs"
            )
        entry = self._entry(index)
        state["active"] = {
            **entry.as_dict(),
            "reserved_at_utc": _utc_now(),
        }
        _atomic_write_json(self.path, state)
        return entry

    @staticmethod
    def _identity_seed(identity: Mapping[str, Any]) -> tuple[str, str]:
        observed = identity.get("seed")
        if not isinstance(observed, str) or not observed.strip():
            raise SeedAllocationError("authoritative run identity has no string seed")
        try:
            canonical = validate_requested_seed(observed)
        except (TypeError, ValueError) as exc:
            raise SeedAllocationError(f"authoritative run seed is invalid: {observed!r}") from exc
        run_id = identity.get("run_id")
        if not isinstance(run_id, str) or not run_id.strip():
            raise SeedAllocationError("authoritative run identity has no run_id")
        return canonical, run_id

    def finalize_started(self, entry: SeedEntry, identity: Mapping[str, Any]) -> None:
        """Commit a verified run and advance the allocation exactly once."""

        state = self._read()
        active = state.get("active")
        if active is None:
            # A repeated reconciliation after a successful commit is safe only
            # when it is exactly the most recent committed run.
            if state["next_index"] == entry.index + 1:
                record = state["consumed"][entry.index]
                observed_canonical, run_id = self._identity_seed(identity)
                if (
                    record.get("canonical_seed") == entry.canonical_seed
                    and record.get("run_id") == run_id
                    and observed_canonical == entry.canonical_seed
                ):
                    return
            raise SeedAllocationError("seed reservation is missing; refusing to advance ledger")
        self._validate_record(active, entry.index, require_run_id=False)
        observed_canonical, run_id = self._identity_seed(identity)
        if observed_canonical != entry.canonical_seed:
            raise SeedAllocationError(
                "authoritative run seed does not match reserved allocation entry"
            )
        active_run_id = active.get("run_id")
        if active_run_id is not None and active_run_id != run_id:
            raise SeedAllocationError(
                "authoritative run_id conflicts with active seed reservation"
            )
        record = {
            **entry.as_dict(),
            "run_id": run_id,
            "observed_seed": identity["seed"],
            "character": identity.get("character"),
            "character_id": identity.get("character_id"),
            "ascension": identity.get("ascension"),
            "game_mode": identity.get("game_mode"),
            "started_at_utc": active.get("reserved_at_utc"),
            "consumed_at_utc": _utc_now(),
        }
        if state["next_index"] != entry.index:
            raise SeedAllocationError("seed ledger next_index conflicts with active reservation")
        state["consumed"].append(record)
        state["next_index"] = entry.index + 1
        state["active"] = None
        _atomic_write_json(self.path, state)

    def observe_current_run(self, identity: Mapping[str, Any]) -> SeedEntry:
        """Reconcile/validate a current run without treating it as a new seed."""

        state = self._read()
        canonical, run_id = self._identity_seed(identity)
        matching = [entry for entry in self.allocation.entries if entry.canonical_seed == canonical]
        if not matching:
            raise SeedAllocationError(
                f"current run seed {identity.get('seed')!r} is not in this allocation"
            )
        entry = matching[0]
        active = state.get("active")
        if active is not None:
            self._validate_record(active, state["next_index"], require_run_id=False)
            if active["index"] != entry.index:
                raise SeedAllocationError(
                    "current run does not match unresolved seed reservation"
                )
            self.finalize_started(entry, identity)
            return entry
        if entry.index >= state["next_index"]:
            raise SeedAllocationError(
                "current run belongs to an unconsumed allocation entry; refusing to skip seeds"
            )
        record = state["consumed"][entry.index]
        if record.get("run_id") != run_id:
            raise SeedAllocationError(
                "current run_id conflicts with the consumed allocation record"
            )
        if record.get("canonical_seed") != canonical:
            raise SeedAllocationError("current run seed conflicts with consumed allocation record")
        return entry

    def validate_saved_run(self, identity: Mapping[str, Any]) -> SeedEntry:
        """Validate a Continue save against consumed/reserved state."""

        # This intentionally delegates to observe_current_run: a saved run is
        # never permitted to consume a pending seed implicitly.
        return self.observe_current_run(identity)
