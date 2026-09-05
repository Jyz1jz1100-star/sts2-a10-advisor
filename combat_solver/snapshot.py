"""Normalized record contract for the Combat Solver read-only interface.

Everything the advisor consumes from the external Combat Solver mod passes
through this schema. Raw mod payloads are preserved verbatim in ``raw`` so a
reader bug can be audited without re-running the solver, while the normalized
fields stay strict: unknown keys or missing required fields are errors, and
format evolution bumps ``schema_version``.

The interface is intentionally file/payload based, not a UI screen: a record
must be traceable to one exact advisor-visible game state via ``state_hash``
(the same normalized SHA-256 the bridge uses as decision id).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = 1

STATE_HASH_PREFIX = "local-sha256:"

#: Turn-level reasons a solver route was unavailable. Battle-level failure
#: accounting in combat_solver.compare treats any of these as "solver absent".
FAILURE_REASONS = (
    "NO_ROUTE",            # solver produced no route for the state
    "TIMEOUT",             # solver exceeded its search budget without a route
    "SEARCH_ERROR",        # solver-internal search failure (SEARCH_FAILURE log)
    "CRASH",               # solver or game crashed mid-battle
    "PARSE_ERROR",         # payload existed but did not match expectations
    "STALE_STATE",         # payload's state no longer matches the live state
    "UNSUPPORTED_SCREEN",  # state is combat but solver does not handle it
    "READER_DOWN",         # the on-disk source was missing/unreadable
)

_ACTION_KINDS = ("play", "potion", "end_turn", "select", "target")


class SnapshotError(ValueError):
    """Raised when a payload does not satisfy the snapshot contract."""


@dataclass(frozen=True)
class RouteAction:
    kind: str
    card_id: str | None = None
    target_index: int | None = None
    note: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    def comparable(self) -> tuple[str, str | None, int | None]:
        """Projection used for route-deviation comparison."""
        return (self.kind, self.card_id, self.target_index)


@dataclass(frozen=True)
class RouteStep:
    turn: int
    actions: tuple[RouteAction, ...]
    predicted_hp_end: int | None = None
    predicted_hp_lost: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "turn": self.turn,
            "actions": [a.to_json() for a in self.actions],
            "predicted_hp_end": self.predicted_hp_end,
            "predicted_hp_lost": self.predicted_hp_lost,
        }


@dataclass(frozen=True)
class CandidateRoute:
    rank: int
    hp_loss: int | None = None
    hp_end: int | None = None
    summary: str | None = None
    actions: tuple[RouteAction, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "hp_loss": self.hp_loss,
            "hp_end": self.hp_end,
            "summary": self.summary,
            "actions": [a.to_json() for a in self.actions],
        }


@dataclass(frozen=True)
class SearchBudget:
    tier: str | None = None
    time_limit_ms: int | None = None
    node_limit: int | None = None
    memory_limit_mb: int | None = None
    elapsed_ms: int | None = None
    nodes_expanded: int | None = None
    peak_memory_mb: int | None = None
    short_elapsed_ms: int | None = None
    deep_elapsed_ms: int | None = None
    process_working_set_mb: int | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Prediction:
    hp_loss: int | None = None
    hp_end: int | None = None
    win: bool | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    def populated(self) -> bool:
        return self.hp_loss is not None or self.hp_end is not None or self.win is not None


@dataclass(frozen=True)
class Provenance:
    reader: str
    captured_at_utc: str
    mod_version: str | None = None
    source_file: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SolverSnapshot:
    """One solver answer bound to exactly one advisor-visible game state.

    ``state_hash`` is the bridge's canonical decision id when the record was
    normalized on this side; records derived from the mod's own logs carry
    ``state_hash=None`` and bind by ``battle_turn`` (freshness-guarded).
    """

    schema_version: int
    state_hash: str | None
    battle_turn: int | None
    route: tuple[RouteStep, ...]
    predicted: Prediction
    budget: SearchBudget
    provenance: Provenance
    candidates: tuple[CandidateRoute, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "state_hash": self.state_hash,
            "battle_turn": self.battle_turn,
            "route": [step.to_json() for step in self.route],
            "predicted": self.predicted.to_json(),
            "budget": self.budget.to_json(),
            "provenance": self.provenance.to_json(),
            "candidates": [c.to_json() for c in self.candidates],
        }


@dataclass(frozen=True)
class SolverFailure:
    """A turn or battle where the solver gave no usable answer."""

    reason: str
    captured_at_utc: str
    state_hash: str | None = None
    battle_turn: int | None = None
    detail: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    def to_json(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "captured_at_utc": self.captured_at_utc,
            "state_hash": self.state_hash,
            "battle_turn": self.battle_turn,
            "detail": self.detail,
        }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SnapshotError(message)


def _opt_int(value: Any, message: str) -> int | None:
    if value is None:
        return None
    _require(isinstance(value, int) and not isinstance(value, bool), message)
    return value


def _opt_str(value: Any, message: str) -> str | None:
    if value is None:
        return None
    _require(isinstance(value, str), message)
    return value


def parse_route_action(data: Any, context: str) -> RouteAction:
    _require(isinstance(data, dict), f"{context}: action must be an object")
    known = {"kind", "card_id", "target_index", "note"}
    unknown = set(data) - known
    _require(not unknown, f"{context}: unknown action keys {sorted(unknown)}")
    kind = data.get("kind")
    _require(kind in _ACTION_KINDS, f"{context}: kind must be one of {_ACTION_KINDS}")
    card_id = _opt_str(data.get("card_id"), f"{context}: card_id")
    target_index = _opt_int(data.get("target_index"), f"{context}: target_index")
    _require(
        kind in ("play", "potion") or card_id is None,
        f"{context}: card_id is only valid on play/potion actions",
    )
    _require(
        kind != "play" or card_id is not None,
        f"{context}: play actions require card_id",
    )
    note = _opt_str(data.get("note"), f"{context}: note")
    return RouteAction(kind=kind, card_id=card_id, target_index=target_index, note=note)


def _parse_actions(value: Any, context: str) -> tuple[RouteAction, ...]:
    _require(isinstance(value, list), f"{context}: actions must be a list")
    return tuple(
        parse_route_action(item, f"{context}[{i}]") for i, item in enumerate(value)
    )


def parse_route_step(data: Any, context: str) -> RouteStep:
    _require(isinstance(data, dict), f"{context}: step must be an object")
    known = {"turn", "actions", "predicted_hp_end", "predicted_hp_lost"}
    unknown = set(data) - known
    _require(not unknown, f"{context}: unknown step keys {sorted(unknown)}")
    turn = _opt_int(data.get("turn"), f"{context}: turn")
    _require(turn is not None and turn >= 1, f"{context}: turn must be a positive int")
    return RouteStep(
        turn=turn,
        actions=_parse_actions(data.get("actions"), f"{context}"),
        predicted_hp_end=_opt_int(
            data.get("predicted_hp_end"), f"{context}: predicted_hp_end"
        ),
        predicted_hp_lost=_opt_int(
            data.get("predicted_hp_lost"), f"{context}: predicted_hp_lost"
        ),
    )


def parse_state_hash(value: Any) -> str:
    _require(isinstance(value, str) and value, "state_hash must be a non-empty string")
    is_hex = len(value) == 64
    if is_hex:
        try:
            int(value, 16)
        except ValueError:
            is_hex = False
    _require(
        value.startswith(STATE_HASH_PREFIX) or is_hex,
        f"state_hash must start with {STATE_HASH_PREFIX!r} or be 64 hex chars",
    )
    return value


def snapshot_from_json(data: Any) -> SolverSnapshot:
    """Validate and normalize one snapshot payload (strict schema).

    ``state_hash`` is optional for records produced from the mod's own logs
    (the mod cannot compute the bridge's canonical state hash); such records
    bind by ``battle_turn`` with a freshness guard instead. At least one
    binding key must be present.
    """
    _require(isinstance(data, dict), "snapshot must be an object")
    known = {
        "schema_version",
        "state_hash",
        "battle_turn",
        "route",
        "predicted",
        "budget",
        "provenance",
        "candidates",
    }
    unknown = set(data) - known
    _require(not unknown, f"snapshot: unknown keys {sorted(unknown)}")
    version = data.get("schema_version")
    _require(
        version == SCHEMA_VERSION,
        f"snapshot: schema_version must be {SCHEMA_VERSION}, got {version!r}",
    )
    state_hash_value = data.get("state_hash")
    state_hash = None if state_hash_value is None else parse_state_hash(state_hash_value)

    route_data = data.get("route")
    _require(isinstance(route_data, list), "snapshot: route must be a list")
    route = tuple(
        parse_route_step(step, f"route[{i}]") for i, step in enumerate(route_data)
    )
    turns = [step.turn for step in route]
    _require(len(turns) == len(set(turns)), "snapshot: route turns must be unique")

    predicted_data = data.get("predicted")
    _require(isinstance(predicted_data, dict), "snapshot: predicted must be an object")
    known_pred = {"hp_loss", "hp_end", "win"}
    unknown_pred = set(predicted_data) - known_pred
    _require(not unknown_pred, f"snapshot: unknown predicted keys {sorted(unknown_pred)}")
    predicted = Prediction(
        hp_loss=_opt_int(predicted_data.get("hp_loss"), "predicted: hp_loss"),
        hp_end=_opt_int(predicted_data.get("hp_end"), "predicted: hp_end"),
        win=predicted_data.get("win"),
    )
    _require(
        predicted_data.get("win") is None or isinstance(predicted_data["win"], bool),
        "predicted: win must be bool or null",
    )
    _require(predicted.populated(), "snapshot: predicted must carry at least one field")

    budget_data = data.get("budget")
    _require(isinstance(budget_data, dict), "snapshot: budget must be an object")
    known_budget = {
        "tier",
        "time_limit_ms",
        "node_limit",
        "memory_limit_mb",
        "elapsed_ms",
        "nodes_expanded",
        "peak_memory_mb",
        "short_elapsed_ms",
        "deep_elapsed_ms",
        "process_working_set_mb",
    }
    unknown_budget = set(budget_data) - known_budget
    _require(not unknown_budget, f"snapshot: unknown budget keys {sorted(unknown_budget)}")
    budget = SearchBudget(
        tier=_opt_str(budget_data.get("tier"), "budget: tier"),
        time_limit_ms=_opt_int(budget_data.get("time_limit_ms"), "budget: time_limit_ms"),
        node_limit=_opt_int(budget_data.get("node_limit"), "budget: node_limit"),
        memory_limit_mb=_opt_int(
            budget_data.get("memory_limit_mb"), "budget: memory_limit_mb"
        ),
        elapsed_ms=_opt_int(budget_data.get("elapsed_ms"), "budget: elapsed_ms"),
        nodes_expanded=_opt_int(
            budget_data.get("nodes_expanded"), "budget: nodes_expanded"
        ),
        peak_memory_mb=_opt_int(
            budget_data.get("peak_memory_mb"), "budget: peak_memory_mb"
        ),
        short_elapsed_ms=_opt_int(
            budget_data.get("short_elapsed_ms"), "budget: short_elapsed_ms"
        ),
        deep_elapsed_ms=_opt_int(
            budget_data.get("deep_elapsed_ms"), "budget: deep_elapsed_ms"
        ),
        process_working_set_mb=_opt_int(
            budget_data.get("process_working_set_mb"), "budget: process_working_set_mb"
        ),
    )

    provenance_data = data.get("provenance")
    _require(isinstance(provenance_data, dict), "snapshot: provenance must be an object")
    known_prov = {"reader", "captured_at_utc", "mod_version", "source_file"}
    unknown_prov = set(provenance_data) - known_prov
    _require(not unknown_prov, f"snapshot: unknown provenance keys {sorted(unknown_prov)}")
    reader = provenance_data.get("reader")
    _require(isinstance(reader, str) and reader, "provenance: reader is required")
    captured = provenance_data.get("captured_at_utc")
    _require(
        isinstance(captured, str) and captured, "provenance: captured_at_utc is required"
    )
    provenance = Provenance(
        reader=reader,
        captured_at_utc=captured,
        mod_version=_opt_str(provenance_data.get("mod_version"), "provenance: mod_version"),
        source_file=_opt_str(provenance_data.get("source_file"), "provenance: source_file"),
    )

    candidates_data = data.get("candidates")
    _require(isinstance(candidates_data, list), "snapshot: candidates must be a list")
    candidates = tuple(_parse_candidate(c, f"candidates[{i}]") for i, c in enumerate(candidates_data))
    ranks = [c.rank for c in candidates]
    _require(
        ranks == sorted(ranks) and len(ranks) == len(set(ranks)),
        "snapshot: candidate ranks must be strictly increasing",
    )

    battle_turn = _opt_int(data.get("battle_turn"), "battle_turn")
    _require(
        state_hash is not None or battle_turn is not None,
        "snapshot: one of state_hash / battle_turn is required as a binding key",
    )

    return SolverSnapshot(
        schema_version=SCHEMA_VERSION,
        state_hash=state_hash,
        battle_turn=battle_turn,
        route=route,
        predicted=predicted,
        budget=budget,
        provenance=provenance,
        candidates=candidates,
        raw={},
    )


def _parse_candidate(data: Any, context: str) -> CandidateRoute:
    _require(isinstance(data, dict), f"{context}: candidate must be an object")
    known = {"rank", "hp_loss", "hp_end", "summary", "actions"}
    unknown = set(data) - known
    _require(not unknown, f"{context}: unknown candidate keys {sorted(unknown)}")
    rank = data.get("rank")
    _require(isinstance(rank, int) and not isinstance(rank, bool) and rank >= 0,
             f"{context}: rank must be a non-negative int")
    return CandidateRoute(
        rank=rank,
        hp_loss=_opt_int(data.get("hp_loss"), f"{context}: hp_loss"),
        hp_end=_opt_int(data.get("hp_end"), f"{context}: hp_end"),
        summary=_opt_str(data.get("summary"), f"{context}: summary"),
        actions=_parse_actions(data.get("actions", []), f"{context}"),
    )


def failure_from_json(data: Any) -> SolverFailure:
    _require(isinstance(data, dict), "failure must be an object")
    known = {"reason", "captured_at_utc", "state_hash", "battle_turn", "detail"}
    unknown = set(data) - known
    _require(not unknown, f"failure: unknown keys {sorted(unknown)}")
    reason = data.get("reason")
    _require(reason in FAILURE_REASONS, f"failure: reason must be one of {FAILURE_REASONS}")
    captured = data.get("captured_at_utc")
    _require(isinstance(captured, str) and captured, "failure: captured_at_utc is required")
    state_hash = data.get("state_hash")
    _require(state_hash is None or (isinstance(state_hash, str) and state_hash),
             "failure: state_hash must be null or a string")
    return SolverFailure(
        reason=reason,
        captured_at_utc=captured,
        state_hash=state_hash,
        battle_turn=_opt_int(data.get("battle_turn"), "failure: battle_turn"),
        detail=_opt_str(data.get("detail"), "failure: detail"),
    )


def snapshot_to_json(snapshot: SolverSnapshot, include_raw: bool = False) -> dict[str, Any]:
    data = snapshot.to_json()
    if include_raw and snapshot.raw:
        data["raw"] = snapshot.raw
    return data


def snapshot_to_line(snapshot: SolverSnapshot) -> str:
    return json.dumps(snapshot_to_json(snapshot), ensure_ascii=False, sort_keys=True)


def snapshot_from_line(line: str) -> SolverSnapshot:
    try:
        data = json.loads(line)
    except ValueError as exc:
        raise SnapshotError(f"invalid JSON line: {exc}") from exc
    snapshot = snapshot_from_json(data)
    return SolverSnapshot(
        schema_version=snapshot.schema_version,
        state_hash=snapshot.state_hash,
        battle_turn=snapshot.battle_turn,
        route=snapshot.route,
        predicted=snapshot.predicted,
        budget=snapshot.budget,
        provenance=snapshot.provenance,
        candidates=snapshot.candidates,
        raw=data.get("raw", {}),
    )
