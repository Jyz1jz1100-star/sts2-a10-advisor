"""Live adapter: Combat Solver answers from the game's diagnostic logs.

Grammar **v1**, calibrated against a real installed-mod session log
(CombatSolver 0.25.3, 2026-09-02, ``%APPDATA%\\SlayTheSpire2\\logs\\godot.log``):

- every solver line carries the ``[CombatSolver/Test]`` marker (after an
  ``[INFO]``/``[ERROR]`` level tag and the ``[CombatSolver]`` logger name);
- ``SEARCH_REQUEST generation=N reason=... turn=T`` opens a search block;
- ``ACTION turn=K kind=PlayCard card_id=X occurrence=0 target_index=I ...`` /
  ``kind=UsePotion potion_id=P slot=S ...`` / ``kind=EndTurn`` materialize the
  route (``-1``/``-``/``None`` mean "no value");
- ``RESULT ... reused=True|False ... searched_turns=K ...
  battle_hp_lost_so_far=F projected_battle_hp_lost=P elapsed_ms=E
  total_elapsed_ms=E total_worker_allocated_bytes=B ... final_hp=H
  combat_ended_turn=C death_turn=- only_death_routes=False ...`` carries the
  prediction and cost stats (``reused=True`` lines are route re-emissions
  with zero cost and are excluded from solve-time/node/memory stats);
- ``TURN_OUTCOME turn=K hp_lost=L sold_hp=... max_block=... energy_left=...``
  completes the per-turn forecast of the block;
- ``SEARCH_FAILURE generation=N exception=<Exception ...>`` (``[ERROR]``
  level) and ``SEARCH_STALE generation=N`` are explicit failures;
- ``SEARCH_INTERIM_RESULT``/``FORECAST``/``COVERAGE``/``START`` and the rest
  are ignored for snapshot purposes.

One snapshot is emitted per completed block: when the RESULT arrives and the
TURN_OUTCOME count reaches ``searched_turns`` (or immediately when no
outcomes are expected), or at the next block boundary as a safety net. A
block whose REQUEST never got a RESULT flushes as a ``NO_ROUTE`` failure.

Records carry ``state_hash=None`` (the mod cannot compute the bridge's
canonical state hash) and bind by ``battle_turn`` with the freshness guard.
"""
from __future__ import annotations

import json
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from combat_solver.logranges import LogRangeError, capture_log_range
from combat_solver.reader import DeployRecord, SourceEvent
from combat_solver.snapshot import (
    FAILURE_REASONS,
    RouteAction,
    RouteStep,
    SCHEMA_VERSION,
    SolverFailure,
    snapshot_from_json,
)

SOLVER_LINE_MARKERS = ("[CombatSolver/Test]", "[CombatSolver/Debug]")

_KIND_TOKENS = {
    "PlayCard": "play",
    "UsePotion": "potion",
    "EndTurn": "end_turn",
}

_TURN_KEYS = ("turn", "battle_turn", "turn_index")
_CARD_KEYS = ("card_id", "card", "cardid")
_POTION_KEYS = ("potion_id", "potion")

_TOKEN = re.compile(r"([A-Za-z_][A-Za-z0-9_\[\]]*)=([^\s,;]+)")
_INT_NULLS = {"-", "None", "null", "n/a"}


def is_solver_line(line: str) -> bool:
    return any(marker in line for marker in SOLVER_LINE_MARKERS)


def parse_kv_tokens(line: str) -> dict[str, str]:
    return {key: value for key, value in _TOKEN.findall(line)}


def _kv_int(kv: dict[str, str], key: str) -> int | None:
    raw = kv.get(key)
    if raw is None or raw in _INT_NULLS:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _kv_bool(kv: dict[str, str], key: str) -> bool | None:
    raw = kv.get(key)
    if raw is None:
        return None
    if raw.lower() == "true":
        return True
    if raw.lower() == "false":
        return False
    return None


def _kv_index(kv: dict[str, str], key: str) -> int | None:
    value = _kv_int(kv, key)
    return None if value is None or value < 0 else value


def _first_int(kv: dict[str, str], keys: Iterable[str]) -> int | None:
    for key in keys:
        value = _kv_int(kv, key)
        if value is not None:
            return value
    return None


def _first_str(kv: dict[str, str], keys: Iterable[str]) -> str | None:
    for key in keys:
        raw = kv.get(key)
        if raw and raw not in _INT_NULLS:
            return raw
    return None


def read_solver_settings(settings_path: str | Path) -> dict[str, int | str | None]:
    """Extract the budget-relevant fields from combat_solver_settings.json."""
    try:
        with Path(settings_path).open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {"tier": None, "time_budget_ms": None, "memory_limit_mb": None}
    deep_seconds = data.get("deepTimeLimitSeconds")
    no_gc_gb = data.get("noGcRegionBudgetGigabytes")
    return {
        "tier": data.get("performancePreset"),
        "time_budget_ms": (
            int(deep_seconds * 1000) if isinstance(deep_seconds, (int, float)) else None
        ),
        "memory_limit_mb": (
            int(no_gc_gb * 1024) if isinstance(no_gc_gb, (int, float)) else None
        ),
    }


@dataclass
class _PendingAction:
    kind: str
    card_id: str | None
    target_index: int | None
    turn: int | None


@dataclass
class _SearchBlock:
    generation: int | None = None
    request_turn: int | None = None
    actions: list[_PendingAction] = field(default_factory=list)
    result: dict[str, str] | None = None
    turn_losses: dict[int, int] = field(default_factory=dict)

    @property
    def has_result(self) -> bool:
        return self.result is not None

    def searched_turns(self) -> int | None:
        if self.result is None:
            return None
        return _kv_int(self.result, "searched_turns")

    def route_signature(self) -> tuple | None:
        """Comparable identity of the emitted route (turn, kind, card, target)."""
        if not self.actions:
            return None
        return tuple(
            (
                action.turn if action.turn is not None else -1,
                action.kind,
                action.card_id,
                action.target_index,
            )
            for action in self.actions
        )


_BYTES_PER_MB = 1024 * 1024


@dataclass(frozen=True)
class _RawLine:
    path: Path
    byte_start: int
    byte_end: int
    text: str


_HASH_CHUNK_BYTES = 1 << 20
_EMPTY_DIGEST = hashlib.sha256().digest()


def _prefix_digest(path: Path, end: int) -> bytes:
    """Hash a consumed prefix in bounded chunks to detect in-place rewrites."""

    hasher = hashlib.sha256()
    remaining = end
    with path.open("rb") as handle:
        while remaining > 0:
            chunk = handle.read(min(_HASH_CHUNK_BYTES, remaining))
            if not chunk:
                break
            hasher.update(chunk)
            remaining -= len(chunk)
    return hasher.digest()


class LogTailSource:
    """Tails ``*.log`` files and emits solver snapshots/failures.

    ``settings_path`` (optional) points at ``combat_solver_settings.json`` so
    budget tier/limits come from the mod's own persistence instead of the
    log lines. Historical log content is never replayed: files are first
    seen at EOF (live tailing only).
    """

    name = "logtail"

    def __init__(
        self,
        log_dir: Path | str,
        settings_path: Path | str | None = None,
        mod_version: str | None = None,
        replay: bool = False,
    ):
        self._log_dir = Path(log_dir)
        self._mod_version = mod_version
        self._replay = replay
        settings = (
            read_solver_settings(settings_path)
            if settings_path is not None
            else {"tier": None, "time_budget_ms": None, "memory_limit_mb": None}
        )
        self._tier = settings.get("tier")
        self._time_budget_ms = settings.get("time_budget_ms")
        self._memory_limit_mb = settings.get("memory_limit_mb")
        self._offsets: dict[Path, int] = {}
        self._file_identities: dict[Path, tuple[int, int]] = {}
        self._prefix_digests: dict[Path, bytes] = {}
        self._started: set[Path] = set()
        self._active_path: Path | None = None
        self._block = _SearchBlock()
        self._emitted_signature: tuple | None = None
        self._emitted_reuse_turn: int | None = None
        # deployment bookkeeping (the mod's own execution records, only
        # present when the mod itself deployed the route)
        self._deploy_actions: dict[int, list[RouteAction]] = {}
        self._deploy_end_turn: dict[int, bool] = {}
        self._deploy_range_starts: dict[int, tuple[Path, int]] = {}
        self._last_search: tuple[Path, int, int] | None = None

    @property
    def log_dir(self) -> Path:
        return self._log_dir

    def _reset_parser_state(self) -> None:
        """Discard incomplete state when the producer file identity changes."""

        self._block = _SearchBlock()
        self._emitted_signature = None
        self._emitted_reuse_turn = None
        self._deploy_actions.clear()
        self._deploy_end_turn.clear()
        self._deploy_range_starts.clear()
        self._last_search = None

    def poll(self) -> list[SourceEvent]:
        events: list[SourceEvent] = []
        if not self._log_dir.is_dir():
            return events
        for path in sorted(self._log_dir.glob("*.log")):
            fresh, reset = self._read_new(path)
            if (reset and self._active_path in (None, path)) or (
                fresh and self._active_path not in (None, path)
            ):
                self._reset_parser_state()
            if fresh:
                self._active_path = path
            for line in fresh:
                self._feed_line(line.text, events, line)
        # End-of-stream flush: the last block of a battle may never see a
        # following SEARCH_REQUEST (combat ends first). Emitting here keeps
        # single-block sessions (and tests) correct; live battles always end
        # with a RESET/GC_COMBAT_LIFECYCLE_DETACHED line that arrives later,
        # so this cannot double-emit.
        self._flush_block(events)
        return events

    def _read_new(self, path: Path) -> tuple[list[_RawLine], bool]:
        """Read complete appended lines with real byte offsets.

        The cursor stops before an unterminated tail.  That tail is retried on
        the next poll, so a writer killed mid-UTF-8 sequence cannot create a
        synthetic marker or shift later offsets.  A digest of the entire
        consumed prefix detects rewrites anywhere before the cursor.
        """

        reset = False
        try:
            file_stat = path.stat()
            size = file_stat.st_size
            identity = (file_stat.st_dev, file_stat.st_ino)
            if path not in self._started:
                self._started.add(path)
                self._offsets[path] = 0 if self._replay else size
                self._file_identities[path] = identity
                self._prefix_digests[path] = _prefix_digest(
                    path, self._offsets[path]
                )
                if not self._replay:
                    return [], False
            offset = self._offsets.get(path, 0)
            if self._file_identities.get(path) != identity or size < offset:
                reset = True
            elif offset and (
                _prefix_digest(path, offset) != self._prefix_digests.get(path)
            ):
                reset = True
            if reset:
                self._file_identities[path] = identity
                # Persist this before early returns: an empty or unterminated
                # replacement must not retain the old cursor or parser state.
                offset = 0
                self._offsets[path] = 0
                self._prefix_digests[path] = _EMPTY_DIGEST
            if size == offset:
                return [], reset
            with path.open("rb") as handle:
                handle.seek(offset)
                payload = handle.read(size - offset)
            final_newline = payload.rfind(b"\n")
            if final_newline < 0:
                return [], reset
            complete = payload[: final_newline + 1]
            lines: list[_RawLine] = []
            relative = 0
            # Use LF-only boundaries, matching logranges._complete_lines.  A
            # lone CR remains content and cannot create a parser-only event.
            for body in complete.split(b"\n")[:-1]:
                end = relative + len(body) + 1
                lines.append(
                    _RawLine(
                        path=path,
                        byte_start=offset + relative,
                        byte_end=offset + end,
                        text=body.decode("utf-8", errors="replace"),
                    )
                )
                relative = end
            self._offsets[path] = offset + len(complete)
            self._prefix_digests[path] = _prefix_digest(path, self._offsets[path])
            return lines, reset
        except OSError:
            return [], reset

    # ------------------------------------------------------------------ feed
    def _feed_line(
        self,
        line: str,
        events: list[SourceEvent],
        source_line: _RawLine | None = None,
    ) -> None:
        if not is_solver_line(line):
            return
        kv = parse_kv_tokens(line)
        marker = _line_event(line)
        if marker == "SEARCH_REQUEST":
            # observed block order: REQUEST -> RESULT -> TURN_OUTCOME* ->
            # ACTION* -> next REQUEST; the route is complete only at the
            # next block boundary.
            self._flush_block(events)
            self._block = _SearchBlock(
                generation=_kv_int(kv, "generation"),
                request_turn=_kv_int(kv, "turn"),
            )
            if source_line is not None:
                self._last_search = (
                    source_line.path,
                    source_line.byte_start,
                    self._block.request_turn or -1,
                )
            return
        if marker == "RESET":
            self._flush_block(events)
            self._block = _SearchBlock()
            return
        if marker == "ACTION":
            self._feed_action(kv)
            return
        if marker == "DEPLOY_START":
            turn = _kv_int(kv, "turn")
            if turn is not None:
                self._deploy_actions[turn] = []
                self._deploy_end_turn[turn] = False
                if source_line is not None:
                    start = source_line.byte_start
                    if (
                        self._last_search is not None
                        and self._last_search[0] == source_line.path
                        and self._last_search[2] == turn
                    ):
                        start = self._last_search[1]
                    self._deploy_range_starts[turn] = (source_line.path, start)
            return
        if marker == "DEPLOY_ACTION":
            self._feed_deploy_action(kv, events)
            return
        if marker == "DEPLOY_END":
            turn = _kv_int(kv, "turn")
            if turn is None:
                return
            actions = tuple(self._deploy_actions.pop(turn, []))
            end_turn = bool(self._deploy_end_turn.pop(turn, False)) or (
                _kv_bool(kv, "end_turn") or False
            )
            log_range = None
            range_start = self._deploy_range_starts.pop(turn, None)
            if (
                source_line is not None
                and range_start is not None
                and range_start[0] == source_line.path
            ):
                try:
                    log_range = capture_log_range(
                        source_line.path,
                        range_start[1],
                        source_line.byte_end,
                        allowed_root=self._log_dir,
                    )
                except LogRangeError:
                    # Keep the deploy as a diagnostic event, but the tracker
                    # marks it unverified and the assessor will not accept it.
                    log_range = None
            events.append(
                SourceEvent.deployed(
                    DeployRecord(
                        turn=turn,
                        actions=actions,
                        end_turn=end_turn,
                        captured_at_utc=_utc_now(),
                        log_range=log_range,
                    )
                )
            )
            return
        if marker == "SEARCH_INTERIM_RESULT":
            return
        if marker == "RESULT":
            # Observed stream shape: within one battle the mod re-emits the
            # full route package (RESULT + TURN_OUTCOME* + ACTION*) after
            # each played card WITHOUT a new SEARCH_REQUEST, and `reused=True`
            # RESULTs repeat the identical package per turn. A second RESULT
            # closes the current block so the trailing ACTION lines rebind to
            # their own RESULT.
            if self._block.has_result:
                self._flush_block(events)
            self._block.result = kv
            return
        if marker == "TURN_OUTCOME":
            turn = _kv_int(kv, "turn")
            hp_lost = _kv_int(kv, "hp_lost")
            if turn is not None and hp_lost is not None:
                self._block.turn_losses[turn] = hp_lost
            return
        if marker == "SEARCH_FAILURE":
            turn = self._block.request_turn
            self._block = _SearchBlock()
            events.append(
                SourceEvent.failed(
                    SolverFailure(
                        reason="SEARCH_ERROR",
                        captured_at_utc=_utc_now(),
                        battle_turn=turn,
                        detail=line.strip(),
                    )
                )
            )
            return
        if marker == "SEARCH_STALE":
            events.append(
                SourceEvent.failed(
                    SolverFailure(
                        reason="STALE_STATE",
                        captured_at_utc=_utc_now(),
                        battle_turn=self._block.request_turn,
                        detail=line.strip(),
                    )
                )
            )
            return

    def _feed_action(self, kv: dict[str, str]) -> None:
        kind_raw = kv.get("kind")
        if kind_raw not in _KIND_TOKENS:
            return
        kind = _KIND_TOKENS[kind_raw]
        if kind == "potion":
            card_id = _first_str(kv, _POTION_KEYS)
        elif kind == "play":
            card_id = _first_str(kv, _CARD_KEYS)
        else:
            card_id = None
        self._block.actions.append(
            _PendingAction(
                kind=kind,
                card_id=card_id,
                target_index=_kv_index(kv, "target_index"),
                turn=_first_int(kv, _TURN_KEYS),
            )
        )

    def _feed_deploy_action(self, kv: dict[str, str], events: list[SourceEvent]) -> None:
        turn = _kv_int(kv, "turn")
        if turn is None:
            return
        potion = _first_str(kv, ("potion", "potion_id"))
        if potion is not None:
            action = RouteAction(kind="potion", card_id=potion,
                                 target_index=_kv_index(kv, "target_index"))
        else:
            card = _first_str(kv, ("card", "card_id"))
            if card is None:
                return
            action = RouteAction(kind="play", card_id=card,
                                 target_index=_kv_index(kv, "target_index"))
        self._deploy_actions.setdefault(turn, []).append(action)

    def _maybe_emit(self, events: list[SourceEvent]) -> None:  # noqa: ARG002
        # Emission happens exclusively at block boundaries (_flush_block):
        # ACTION lines trail RESULT/TURN_OUTCOME, so any earlier emission
        # would produce snapshots without routes.
        return

    def _flush_block(self, events: list[SourceEvent]) -> None:
        block = self._block
        if block.has_result:
            events.extend(self._emit_snapshot())
        elif block.request_turn is not None:
            events.append(
                SourceEvent.failed(
                    SolverFailure(
                        reason="NO_ROUTE",
                        captured_at_utc=_utc_now(),
                        battle_turn=block.request_turn,
                        detail=f"SEARCH_REQUEST generation={block.generation} got no RESULT",
                    )
                )
            )
        self._block = _SearchBlock()

    # ------------------------------------------------------------------ emit
    def _emit_snapshot(self) -> list[SourceEvent]:
        block = self._block
        result = block.result or {}
        reused = _kv_bool(result, "reused")
        turn = block.request_turn
        if reused and block.actions:
            # A reused package replays the surviving suffix of the original
            # route; its first action is the instruction for the CURRENT turn,
            # which is the turn this snapshot must bind to.
            first = block.actions[0]
            if first.turn is not None:
                turn = first.turn
        if turn is None and block.actions:
            turns = [a.turn for a in block.actions if a.turn is not None]
            turn = min(turns) if turns else None
        if turn is None:
            return [
                SourceEvent.failed(
                    SolverFailure(
                        reason="PARSE_ERROR",
                        captured_at_utc=_utc_now(),
                        detail="solver block without a turn label",
                    )
                )
            ]
        signature = block.route_signature()
        reused = _kv_bool(result, "reused")
        # Duplicate suppression: a `reused=True` block for the same turn whose
        # route is byte-identical to the last emission is a log echo of the
        # same answer, not a new solver result. Fresh searches and blocks with
        # changed routes always pass.
        if (
            signature is not None
            and reused
            and signature == self._emitted_signature
            and turn == self._emitted_reuse_turn
        ):
            return []
        self._emitted_signature = signature
        self._emitted_reuse_turn = turn if reused else None
        steps = _group_route(block.actions, block.turn_losses)
        reused = _kv_bool(result, "reused")
        worker_bytes = _kv_int(result, "total_worker_allocated_bytes")
        ws_bytes = _kv_int(result, "process_working_set_bytes")
        predicted: dict[str, int | bool | None] = {}
        final_hp = _kv_int(result, "final_hp")
        projected = _kv_int(result, "projected_battle_hp_lost")
        if final_hp is not None:
            predicted["hp_end"] = final_hp
        if projected is not None:
            predicted["hp_loss"] = projected
        if not predicted:
            return [
                SourceEvent.failed(
                    SolverFailure(
                        reason="PARSE_ERROR",
                        captured_at_utc=_utc_now(),
                        battle_turn=turn,
                        detail="RESULT without any prediction field",
                    )
                )
            ]
        payload = {
            "schema_version": SCHEMA_VERSION,
            "state_hash": None,
            "battle_turn": turn,
            "route": [step.to_json() for step in steps],
            "predicted": predicted,
            "budget": {
                "tier": self._tier,
                "time_limit_ms": self._time_budget_ms,
                "node_limit": None,
                "memory_limit_mb": self._memory_limit_mb,
                "elapsed_ms": None if reused else _kv_int(result, "total_elapsed_ms"),
                "nodes_expanded": None if reused else _kv_int(result, "expanded"),
                "peak_memory_mb": (
                    worker_bytes // _BYTES_PER_MB if worker_bytes is not None else None
                ),
                "short_elapsed_ms": (
                    None if reused else _kv_int(result, "short_elapsed_ms")
                ),
                "deep_elapsed_ms": (
                    None if reused else _kv_int(result, "deep_elapsed_ms")
                ),
                # process working set is an instantaneous process-level
                # observation, meaningful on every RESULT (fresh or reused)
                "process_working_set_mb": (
                    ws_bytes // _BYTES_PER_MB if ws_bytes is not None else None
                ),
            },
            "provenance": {
                "reader": self.name,
                "captured_at_utc": _utc_now(),
                "mod_version": self._mod_version,
                "source_file": None,
            },
            "candidates": [],
        }
        try:
            snapshot = snapshot_from_json(payload)
        except Exception as exc:
            return [
                SourceEvent.failed(
                    SolverFailure(
                        reason="PARSE_ERROR",
                        captured_at_utc=_utc_now(),
                        battle_turn=turn,
                        detail=f"block did not satisfy the snapshot contract: {exc}",
                    )
                )
            ]
        return [SourceEvent.of(snapshot)]


def _group_route(
    actions: list[_PendingAction], turn_losses: dict[int, int]
) -> list[RouteStep]:
    grouped: dict[int, list[RouteAction]] = {}
    for action in actions:
        grouped.setdefault(action.turn if action.turn is not None else 1, []).append(
            RouteAction(
                kind=action.kind,
                card_id=action.card_id,
                target_index=action.target_index,
            )
        )
    steps: list[RouteStep] = []
    for step_turn in sorted(grouped):
        loss = turn_losses.get(step_turn)
        steps.append(
            RouteStep(
                turn=step_turn,
                actions=tuple(grouped[step_turn]),
                predicted_hp_lost=loss,
            )
        )
    return steps


def _line_event(line: str) -> str | None:
    """Extract the event word after the exact solver marker that matched."""

    for marker in SOLVER_LINE_MARKERS:
        idx = line.find(marker)
        if idx < 0:
            continue
        rest = line[idx + len(marker) :].strip()
        if not rest:
            return None
        token = rest.split()[0]
        if token.startswith("["):  # extra logger scopes between marker and event
            return None
        return token
    return None


def failure_from_line(line: str) -> SolverFailure | None:
    kv = parse_kv_tokens(line)
    for reason in FAILURE_REASONS:
        if kv.get("failure") == reason or kv.get("reason") == reason:
            return SolverFailure(
                reason=reason, captured_at_utc=_utc_now(), detail=line.strip()
            )
    return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


__all__ = [
    "LogTailSource",
    "read_solver_settings",
    "is_solver_line",
    "parse_kv_tokens",
    "failure_from_line",
]
