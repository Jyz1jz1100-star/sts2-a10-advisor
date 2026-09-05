"""Pluggable on-disk sources for Combat Solver outputs.

The advisor never scrapes the game UI and never reads mod internals. A
source turns files written by (or for) the Combat Solver mod into validated
`SolverSnapshot` / `SolverFailure` records. The concrete mapping from the
mod's own payloads to this contract lives in the adapter classes; until the
mod's diagnostic format is pinned, payloads may also be pre-normalized
offline (JsonlSource) so the comparison pipeline is testable end to end.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from combat_solver.snapshot import (
    RouteAction,
    SnapshotError,
    SolverFailure,
    SolverSnapshot,
    failure_from_json,
    snapshot_from_line,
    snapshot_from_json,
)


@dataclass(frozen=True)
class DeployRecord:
    """The mod's own execution record for one turn (DEPLOY_ACTION/DEPLOY_END).

    Only produced when the mod itself deployed (full-auto or execute-turn
    mode). It is the exact ground truth of what was played and supersedes
    state-delta inference for automated batches.
    """

    turn: int
    actions: tuple[RouteAction, ...]
    end_turn: bool
    captured_at_utc: str


@dataclass(frozen=True)
class SourceEvent:
    """One validated item read from a source."""

    snapshot: SolverSnapshot | None = None
    failure: SolverFailure | None = None
    deploy: DeployRecord | None = None

    @classmethod
    def of(cls, snapshot: SolverSnapshot) -> "SourceEvent":
        return cls(snapshot=snapshot)

    @classmethod
    def failed(cls, failure: SolverFailure) -> "SourceEvent":
        return cls(failure=failure)

    @classmethod
    def deployed(cls, deploy: DeployRecord) -> "SourceEvent":
        return cls(deploy=deploy)


class SolverSource(Protocol):
    name: str

    def poll(self) -> list[SourceEvent]:
        """Return new events since the previous poll (never blocks)."""
        ...


class JsonlSource:
    """Reads pre-normalized snapshot/failure JSONL lines appended over time.

    Line forms:
      {"kind": "snapshot", ...snapshot fields...}
      {"kind": "failure", "reason": "...", ...}
    A missing "kind" is accepted as a snapshot (the common case).
    """

    def __init__(self, path: Path | str):
        self.name = f"jsonl:{path}"
        self._path = Path(path)
        self._offset = 0
        self._seen_bad_bytes = False

    def poll(self) -> list[SourceEvent]:
        try:
            with self._path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self._offset)
                fresh = handle.read()
                self._offset = handle.tell()
        except FileNotFoundError:
            if not self._seen_bad_bytes:
                self._seen_bad_bytes = True
            return []
        except OSError:
            return []
        events: list[SourceEvent] = []
        for line in fresh.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except ValueError:
                events.append(
                    SourceEvent.failed(
                        SolverFailure(reason="PARSE_ERROR", captured_at_utc="", detail="bad json line")
                    )
                )
                continue
            events.append(self._parse_item(data))
        return events

    @staticmethod
    def _parse_item(data: dict) -> SourceEvent:
        kind = data.get("kind")
        if kind == "failure":
            failure_data = {k: v for k, v in data.items() if k != "kind"}
            try:
                return SourceEvent.failed(failure_from_json(failure_data))
            except SnapshotError as exc:
                return SourceEvent.failed(
                    SolverFailure(
                        reason="PARSE_ERROR", captured_at_utc="", detail=str(exc)
                    )
                )
        if kind == "snapshot":
            data = {k: v for k, v in data.items() if k != "kind"}
        try:
            return SourceEvent.of(snapshot_from_json(data))
        except SnapshotError as exc:
            return SourceEvent.failed(
                SolverFailure(reason="PARSE_ERROR", captured_at_utc="", detail=str(exc))
            )


class DirectorySource:
    """Hands every new file in a directory to an injected parser.

    This is the seam where the mod's own export/log payloads get adapted:
    ``parse_fn`` receives the raw file bytes plus the file path and returns
    SourceEvents. Files are consumed in sorted name order, once.
    """

    def __init__(
        self,
        directory: Path | str,
        parse_fn: Callable[[bytes, Path], list[SourceEvent]],
        pattern: str = "*",
    ):
        self.name = f"directory:{directory}"
        self._directory = Path(directory)
        self._parse_fn = parse_fn
        self._pattern = pattern
        self._seen: set[str] = set()

    def poll(self) -> list[SourceEvent]:
        if not self._directory.is_dir():
            return []
        events: list[SourceEvent] = []
        for path in sorted(self._directory.glob(self._pattern)):
            if not path.is_file() or path.name in self._seen:
                continue
            self._seen.add(path.name)
            try:
                payload = path.read_bytes()
            except OSError:
                continue
            try:
                events.extend(self._parse_fn(payload, path))
            except Exception as exc:  # parser bugs must not kill the harness
                events.append(
                    SourceEvent.failed(
                        SolverFailure(
                            reason="PARSE_ERROR",
                            captured_at_utc="",
                            detail=f"{path.name}: {exc}",
                        )
                    )
                )
        return events


def parse_normalized_json_bytes(payload: bytes, source: str | Path) -> list[SourceEvent]:
    """Default parser: JSON array or single object in the normalized contract."""
    source_name = Path(source).name if source else None
    try:
        data = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return [
            SourceEvent.failed(
                SolverFailure(reason="PARSE_ERROR", captured_at_utc="", detail=str(exc))
            )
        ]
    items = data if isinstance(data, list) else [data]
    events: list[SourceEvent] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        event = JsonlSource._parse_item(item)
        if event.snapshot is not None:
            snapshot = event.snapshot
            event = SourceEvent.of(
                SolverSnapshot(
                    schema_version=snapshot.schema_version,
                    state_hash=snapshot.state_hash,
                    battle_turn=snapshot.battle_turn,
                    route=snapshot.route,
                    predicted=snapshot.predicted,
                    budget=snapshot.budget,
                    provenance=type(snapshot.provenance)(
                        reader=snapshot.provenance.reader,
                        captured_at_utc=snapshot.provenance.captured_at_utc,
                        mod_version=snapshot.provenance.mod_version,
                        source_file=source_name,
                    ),
                    candidates=snapshot.candidates,
                    raw=snapshot.raw,
                )
            )
        events.append(event)
    return events


__all__ = [
    "SourceEvent",
    "SolverSource",
    "JsonlSource",
    "DirectorySource",
    "parse_normalized_json_bytes",
    "snapshot_from_line",
]
