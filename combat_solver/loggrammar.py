"""Grammar selection, container codecs and the shared line grammar.

Two producer containers have to be readable side by side:

**Grammar v1** (CombatSolver <= 0.31.0, calibrated against the 2026-09-02
``godot.log`` session and replay-verified on 0.29.1/0.31.0): the mod wrote one
solver event per *physical* line of the Godot log, so
``%APPDATA%\\SlayTheSpire2\\logs\\godot*.log`` is the source of record.

**Grammar v2** (CombatSolver 0.35.5 - 0.41.0 observed on this machine): the mod
writes its own JSON Lines journal under
``%APPDATA%\\SlayTheSpire2\\logs\\CombatSolver\\<pid>-<guid>\\``, with

* ``process.jsonl``: process-scoped mirror of a fixed performance whitelist
  plus the combat session boundaries ``COMBAT_LOG_BEGIN id=<guid>`` /
  ``COMBAT_LOG_END reason=<reason>``.  No solver payload marker
  (``RESULT``/``ACTION``/``DEPLOY_*``/``SEARCH_*``) appears in it.
* ``combat-<guid>.jsonl``: one file per combat; all content markers live here.

Every line is the envelope ``{"Time":<unix ms>,"Level":"info","Message":...}``.
The ``Message`` keeps the v1 ``[CombatSolver/<channel>] TAG key=value`` shape,
but **one record can carry a whole search block** as embedded ``\\r\\n`` separated
lines (observed on all 13 real sessions: one record holds ``RESULT`` +
``FORECAST`` + ``TURN_OUTCOME`` + ``COVERAGE`` + ``ACTION``), so a record decodes
to one or more *logical* lines.  The v1 line grammar is unchanged inside the
payload; what moved is the container and the block boundaries (``SEARCH_REQUEST``
is now emitted once per combat and later turns announce ``SEARCH_REUSED``).

The byte offsets used for evidence are always offsets into the *physical* file
that was read; the envelope is never rewritten or re-encoded.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from combat_solver.snapshot import RouteAction, RouteStep, SCHEMA_VERSION

GRAMMAR_V1 = "v1"
GRAMMAR_V2 = "v2"
GRAMMARS = (GRAMMAR_V1, GRAMMAR_V2)

#: directory the v2 journal is written to, relative to the game log dir
JOURNAL_DIR_NAME = "CombatSolver"
PROCESS_FILE_NAME = "process.jsonl"
COMBAT_FILE_PREFIX = "combat-"
JOURNAL_FILE_SUFFIX = ".jsonl"

SOLVER_LINE_MARKERS = ("[CombatSolver/Test]", "[CombatSolver/Debug]")
EVIDENCE_MARKER = "[CombatSolver/Evidence]"

#: the v1 ``SEARCH_REQUEST`` line is only emitted for the first search of a
#: combat in v2; later turns announce the turn they answer with one of these
V2_REQUEST_MARKERS = ("SEARCH_REQUEST", "SEARCH_REUSED", "TURN_SETUP_SEARCH_START")
#: markers that record a search being cut off by its own budget
V2_BUDGET_MARKERS = ("SEARCH_TIME_BUDGET", "TURN_LAYER_BUDGET")
#: per-combat session boundary inside ``combat-*.jsonl``
V2_RESET_MARKER = "RESET"
#: process-scoped combat session boundaries inside ``process.jsonl``
V2_COMBAT_LOG_BEGIN_RE = re.compile(r"^COMBAT_LOG_BEGIN\s+id=([0-9a-fA-F]{32})\b")
V2_COMBAT_LOG_END_RE = re.compile(r"^COMBAT_LOG_END\s+reason=(\S+)")
#: ``<guid>.jsonl`` names are only *claims*; they become a battle identity once
#: the matching ``COMBAT_LOG_BEGIN id=`` record has been read from process.jsonl
_BATTLE_ID_RE = re.compile(r"^[0-9a-f]{32}$")

#: Grammar v1 failure classes that have **no** producer-side equivalent in v2.
#: Verified over all 13 real 0.35.5 - 0.41.0 sessions (10793 combat records plus
#: 15 ``process.jsonl`` files): zero occurrences of ``SEARCH_FAILURE``,
#: ``SEARCH_ERROR``, ``SEARCH_STALE``, ``exception=``, ``failure=`` and zero
#: case-insensitive occurrences of ``stale``.  The v2 codec therefore never
#: emits these reasons; there is no invented mapping for them.
V2_UNREACHABLE_REASONS = ("SEARCH_ERROR", "STALE_STATE")

#: ``kind=`` tokens of the ACTION line plus ``action.Kind`` of the evidence
#: channel: the producer uses one vocabulary for both
KIND_TOKENS = {
    "PlayCard": "play",
    "UsePotion": "potion",
    "EndTurn": "end_turn",
}
#: ``ROUTE_ACTION.action`` fields the reader looks at; ``EndTurn`` is reported
#: with ``IsExecutable=false`` by the producer (the mod ends the turn through its
#: own UI path), so executability is not used as a route filter.
EVIDENCE_ACTION_FIELDS = ("Kind", "Turn", "CardId", "PotionId", "TargetIndex")

TURN_KEYS = ("turn", "battle_turn", "turn_index")
CARD_KEYS = ("card_id", "card", "cardid")
POTION_KEYS = ("potion_id", "potion")

_TOKEN = re.compile(r"([A-Za-z_][A-Za-z0-9_\[\]]*)=([^\s,;]+)")
_INT_NULLS = {"-", "None", "null", "n/a"}

#: sniffing result for "this file has no classifiable content (yet)"
SNIFF_UNDECIDED = "undecided"
_SNIFF_WINDOW_BYTES = 1 << 20
_HASH_CHUNK_BYTES = 1 << 20
_EMPTY_DIGEST = hashlib.sha256().digest()


class EnvelopeError(ValueError):
    """A v2 journal record cannot be decoded (unknown shape, torn, not an object)."""


def is_solver_line(line: str) -> bool:
    return any(marker in line for marker in SOLVER_LINE_MARKERS)


def is_evidence_line(line: str) -> bool:
    return EVIDENCE_MARKER in line


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


def _line_event(line: str, markers: Iterable[str] = SOLVER_LINE_MARKERS) -> str | None:
    """Extract the event word after the exact solver marker that matched."""

    for marker in markers:
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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


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


@dataclass(frozen=True)
class _RawLine:
    path: Path
    byte_start: int
    byte_end: int
    text: str


# --------------------------------------------------------------- sniffing


def sniff_grammar(path: Path | str) -> str | None:
    """Classify a source file by content, never by name.

    Positive evidence is required for v2: the first non-empty physical line must
    start with ``{`` *and* decode as a journal envelope.  Anything else that is
    readable stays v1, which is the historical default for Godot logs whose
    first lines are ordinary engine output.  ``None`` means "a journal-looking
    record that does not decode": callers must fail closed, not fall back.
    """

    try:
        with Path(path).open("rb") as handle:
            blob = handle.read(_SNIFF_WINDOW_BYTES)
    except OSError:
        return SNIFF_UNDECIDED
    for raw in blob.split(b"\n"):
        if not raw.strip():
            continue
        if not raw.lstrip().startswith(b"{"):
            return GRAMMAR_V1
        try:
            decode_v2_record(raw.decode("utf-8"))
        except (EnvelopeError, UnicodeDecodeError):
            return None
        return GRAMMAR_V2
    return SNIFF_UNDECIDED


@dataclass(frozen=True)
class V2Record:
    level: str
    time_ms: int | None
    lines: tuple[str, ...]


def decode_v2_record(text: str) -> V2Record:
    """Unwrap one journal envelope into its logical payload lines.

    ``Time`` is only reported, never used to synthesise provenance.  A record
    without a string ``Level``/``Message`` is an error: the codec must not guess
    that a record it cannot classify is a solver event.
    """

    stripped = text.strip()
    if not stripped:
        raise EnvelopeError("empty journal record")
    try:
        data = json.loads(stripped)
    except ValueError as exc:
        raise EnvelopeError(f"record is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise EnvelopeError("record is not a JSON object")
    level = data.get("Level")
    if not isinstance(level, str) or not level:
        raise EnvelopeError("record has no string Level")
    message = data.get("Message")
    if not isinstance(message, str) or not message:
        raise EnvelopeError("record has no non-empty string Message")
    time_ms = data.get("Time")
    if isinstance(time_ms, bool) or not isinstance(time_ms, int):
        time_ms = None
    lines = tuple(
        body[:-1] if body.endswith("\r") else body
        for body in (raw.strip("\ufeff") for raw in message.split("\n"))
        if body
    )
    if not lines:
        raise EnvelopeError("record Message has no payload lines")
    return V2Record(level=level, time_ms=time_ms, lines=lines)


def parse_evidence_payload(line: str) -> dict[str, Any] | None:
    """Return the JSON object of an ``[CombatSolver/Evidence]`` line, if any."""

    start = line.find("{")
    if start < 0:
        return None
    try:
        data = json.loads(line[start:])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------- discovery


@dataclass(frozen=True)
class V2Source:
    """One tailable journal file plus the battle identity its name claims."""

    path: Path
    session_dir: Path
    kind: str  # "process" | "combat"
    claimed_battle_id: str | None

    @property
    def is_content_source(self) -> bool:
        return self.kind == "combat"


def journal_roots(log_dir: Path | str) -> list[Path]:
    """Session directories under a game log dir, or the dir itself if it is one.

    A directory that holds combat files without a ``process.jsonl`` is still a
    session: the combat stream is readable on its own, it just cannot confirm
    any battle identity.
    """

    root = Path(log_dir)
    roots: list[Path] = []
    nested = root / JOURNAL_DIR_NAME
    if nested.is_dir():
        roots.extend(sorted(item for item in nested.iterdir() if item.is_dir()))
    if (root / PROCESS_FILE_NAME).is_file() or any(
        root.glob(COMBAT_FILE_PREFIX + "*" + JOURNAL_FILE_SUFFIX)
    ):
        roots.append(root)
    return roots


def discover_v2_sources(log_dir: Path | str) -> tuple[V2Source, ...]:
    """Per-combat file discovery, ordered so a session's boundaries precede content.

    ``process.jsonl`` is listed before the combat files of the same session so
    the ``COMBAT_LOG_BEGIN id=`` records that confirm a battle identity are read
    first; sessions are ordered by their directory name, combat files by name.
    """

    sources: list[V2Source] = []
    for session in journal_roots(log_dir):
        process = session / PROCESS_FILE_NAME
        if process.is_file():
            sources.append(
                V2Source(
                    path=process,
                    session_dir=session,
                    kind="process",
                    claimed_battle_id=None,
                )
            )
        for path in sorted(session.glob(COMBAT_FILE_PREFIX + "*" + JOURNAL_FILE_SUFFIX)):
            if not path.is_file():
                continue
            stem = path.name[: -len(JOURNAL_FILE_SUFFIX)]
            claimed = stem[len(COMBAT_FILE_PREFIX) :]
            if not _BATTLE_ID_RE.fullmatch(claimed):
                # Not a battle journal: a name like combat-notes.jsonl must not
                # become a source with an unclaimed identity, or an unrelated
                # file could be parsed as if it were a combat.
                continue
            sources.append(
                V2Source(
                    path=path,
                    session_dir=session,
                    kind="combat",
                    claimed_battle_id=claimed,
                )
            )
    return tuple(sources)


# --------------------------------------------------------------- line state


@dataclass
class _PendingAction:
    kind: str
    card_id: str | None
    target_index: int | None
    turn: int | None
    note: str | None = None


@dataclass
class _TraceActions:
    """One ``[CombatSolver/Evidence]`` route trace, accumulated by ``traceId``."""

    trace_id: str
    actions: list[_PendingAction] = field(default_factory=list)
    action_count: int | None = None
    completed_steps: int | None = None
    first_difference: Any = None
    replay_seen: bool = False

    def complete(self) -> bool:
        return (
            self.replay_seen
            and self.action_count is not None
            and self.completed_steps == self.action_count
            and self.first_difference is None
        )

    def comparable(self) -> tuple | None:
        if not self.actions:
            return None
        return route_signature(self.actions)


def route_signature(actions: Iterable[_PendingAction]) -> tuple:
    """Comparable identity of a route (turn, kind, card, target)."""

    return tuple(
        (
            action.turn if action.turn is not None else -1,
            action.kind,
            action.card_id,
            action.target_index,
        )
        for action in actions
    )


@dataclass
class _SearchBlock:
    generation: int | None = None
    request_turn: int | None = None
    request_marker: str | None = None
    actions: list[_PendingAction] = field(default_factory=list)
    result: dict[str, str] | None = None
    turn_losses: dict[int, int] = field(default_factory=dict)
    #: grammar v2 only: a budget marker was seen while this request was open
    budget_exhausted: bool = False
    #: grammar v2 only: evidence traces announced while this request was open
    traces: dict[str, _TraceActions] = field(default_factory=dict)

    @property
    def has_result(self) -> bool:
        return self.result is not None

    def searched_turns(self) -> int | None:
        if self.result is None:
            return None
        return _kv_int(self.result, "searched_turns")

    def route_signature(self) -> tuple | None:
        if not self.actions:
            return None
        return route_signature(self.actions)


def group_route(
    actions: list[_PendingAction], turn_losses: dict[int, int]
) -> list[RouteStep]:
    """Materialize one snapshot route, grouped by the action's own turn label."""

    grouped: dict[int, list[RouteAction]] = {}
    for action in actions:
        grouped.setdefault(action.turn if action.turn is not None else 1, []).append(
            RouteAction(
                kind=action.kind,
                card_id=action.card_id,
                target_index=action.target_index,
                note=action.note,
            )
        )
    steps: list[RouteStep] = []
    for step_turn in sorted(grouped):
        steps.append(
            RouteStep(
                turn=step_turn,
                actions=tuple(grouped[step_turn]),
                predicted_hp_lost=turn_losses.get(step_turn),
            )
        )
    return steps


_BYTES_PER_MB = 1024 * 1024


def snapshot_payload(
    block: _SearchBlock,
    *,
    turn: int,
    steps: list[Any],
    tier: str | None,
    time_limit_ms: int | None,
    memory_limit_mb: int | None,
    reader: str,
    mod_version: str | None,
    source_file: str | None,
) -> dict[str, Any] | None:
    """Build the normalized snapshot payload shared by both grammars.

    Returns ``None`` when the RESULT carries no prediction at all: an answer
    without a prediction is not a solver answer, so the caller must fail closed.
    """

    result = block.result or {}
    reused = _kv_bool(result, "reused") or False
    predicted: dict[str, int | bool | None] = {}
    final_hp = _kv_int(result, "final_hp")
    projected = _kv_int(result, "projected_battle_hp_lost")
    if final_hp is not None:
        predicted["hp_end"] = final_hp
    if projected is not None:
        predicted["hp_loss"] = projected
    if not predicted:
        return None
    worker_bytes = _kv_int(result, "total_worker_allocated_bytes")
    ws_bytes = _kv_int(result, "process_working_set_bytes")
    return {
        "schema_version": SCHEMA_VERSION,
        "state_hash": None,
        "battle_turn": turn,
        "route": [step.to_json() for step in steps],
        "predicted": predicted,
        "budget": {
            "tier": tier,
            "time_limit_ms": time_limit_ms,
            "node_limit": None,
            "memory_limit_mb": memory_limit_mb,
            "elapsed_ms": None if reused else _kv_int(result, "total_elapsed_ms"),
            "nodes_expanded": None if reused else _kv_int(result, "expanded"),
            "peak_memory_mb": (
                worker_bytes // _BYTES_PER_MB if worker_bytes is not None else None
            ),
            "short_elapsed_ms": None if reused else _kv_int(result, "short_elapsed_ms"),
            "deep_elapsed_ms": None if reused else _kv_int(result, "deep_elapsed_ms"),
            # process working set is an instantaneous process-level observation,
            # meaningful on every RESULT (fresh or reused)
            "process_working_set_mb": (
                ws_bytes // _BYTES_PER_MB if ws_bytes is not None else None
            ),
        },
        "provenance": {
            "reader": reader,
            "captured_at_utc": _utc_now(),
            "mod_version": mod_version,
            "source_file": source_file,
        },
        "candidates": [],
    }
