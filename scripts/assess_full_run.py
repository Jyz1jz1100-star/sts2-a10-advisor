"""Audit real Slay the Spire 2 Ironclad A10 full-run traces.

This module is deliberately read-only with respect to the game.  It consumes
the JSONL trace emitted by ``bridge.autoplay``/``bridge.trace_controller`` and
optionally the manifests emitted by the supervisor.  It never sends a game
action and it never turns a battle record into a run win.

The command has two useful modes::

    # Create an empty, explicit pilot/formal plan.  No game is touched.
    python scripts/assess_full_run.py --plan --cohort assisted \
        --seed-mode observational --out runs/full_run_acceptance/plan.json

    # Audit one or more completed traces.
    python scripts/assess_full_run.py --trace runs/.../autoplay_trace.jsonl \
        --cohort assisted --seed-mode observational --out report.json

``observational`` and ``fixed`` are kept separate in every artifact.  A
fixed-seed claim requires a pre-registered seed file and every run's raw seed
must be present in it.  A missing or ambiguous terminal/identity/provenance
field is a blocker, never an implicit loss that can be hidden by dropping a
row.  In particular, a ``continue`` action is not evidence that the resumed
save is an Ironclad A10 standard run: it must be followed by a machine-readable
compendium/run-identity record.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from training.wilson import wilson_interval  # noqa: E402
from combat_solver.evidence import (  # noqa: E402
    EvidenceIntegrityError,
    VerifiedComparisonEvidence,
    adapt_comparison_evidence,
)


SCHEMA_VERSION = 1
COHORTS = ("assisted", "no-sl")
SEED_MODES = ("observational", "fixed")
DECISION_TYPES = {
    "map",
    "event",
    "shop",
    "fake_merchant",
    "rest_site",
    "rewards",
    "card_reward",
    "card_select",
    "relic_select",
    "treasure",
    "bundle_select",
    "crystal_sphere",
    "monster",
    "elite",
    "boss",
    "hand_select",
}
COMBAT_TYPES = {"monster", "elite", "boss", "hand_select"}
ACTS = (1, 2, 3)
DEFAULT_PILOT_RUNS = 20
DEFAULT_FORMAL_RUNS = 500

# A full-run report must have one and only one process that is authorized to
# perform each player's action.  The keeper is deliberately modelled as a
# watchdog below, not as an execution owner: it may recover the Combat Solver
# toggle but must never be allowed to run beside a second action executor.
EXECUTION_OWNERS = {
    "http_route_executor",
    "combat_solver_full_auto",
    "manual_player",
}
_EXECUTION_OWNER_ALIASES = {
    "http": "http_route_executor",
    "http_route": "http_route_executor",
    "http_route_executor": "http_route_executor",
    "route_executor": "http_route_executor",
    "bridge": "http_route_executor",
    "combat_solver_full_auto": "combat_solver_full_auto",
    "combat_solver_fullauto": "combat_solver_full_auto",
    "full_auto": "combat_solver_full_auto",
    "fullauto": "combat_solver_full_auto",
    "manual": "manual_player",
    "manual_player": "manual_player",
    "player": "manual_player",
}
_EXECUTION_WATCHDOGS = {"fullauto_keeper", "keeper", "full_auto_keeper"}
_MOD_DEPLOY_SOURCES = {
    "combat_solver_full_auto",
    "combat_solver_fullauto",
    "deploy_log",
    "mod_deploy",
    "solver_deploy",
    "full_auto_deploy",
}
_OWNER_FINGERPRINT_FIELDS = (
    "execution_owner",
    "execution_owners",
    "owner",
    "owners",
    "combat_execution_owner",
    "combat_owner",
    "execution_watchdog",
    "execution_watchdogs",
    "watchdog",
    "watchdogs",
    "source",
    "execution_source",
    "action_source",
)


def _owner_token(value: Any) -> str:
    """Normalise an owner/watchdog label without silently guessing."""

    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _owner_values(*values: Any) -> tuple[set[str], set[str], set[str]]:
    """Return canonical owners, watchdogs, and unrecognised owner labels."""

    owners: set[str] = set()
    watchdogs: set[str] = set()
    unknown: set[str] = set()

    def visit(value: Any) -> None:
        if value is None or value == "":
            return
        if isinstance(value, Mapping):
            for nested in value.values():
                visit(nested)
            return
        if isinstance(value, (list, tuple, set, frozenset)):
            for nested in value:
                visit(nested)
            return
        token = _owner_token(value)
        if not token:
            return
        if token in _EXECUTION_WATCHDOGS:
            watchdogs.add("fullauto_keeper")
            return
        canonical = _EXECUTION_OWNER_ALIASES.get(token)
        if canonical in EXECUTION_OWNERS:
            owners.add(canonical)
        else:
            unknown.add(token)

    for value in values:
        visit(value)
    return owners, watchdogs, unknown


def _mapping_owner_values(raw: Any) -> tuple[set[str], set[str], set[str]]:
    """Read only explicit ownership fields from one event/session object."""

    if not isinstance(raw, Mapping):
        return set(), set(), set()
    values: list[Any] = []
    for key in (
        "execution_owner",
        "execution_owners",
        "owner",
        "owners",
        "combat_execution_owner",
        "combat_owner",
        "execution_watchdog",
        "execution_watchdogs",
        "watchdog",
        "watchdogs",
    ):
        if key in raw:
            values.append(raw.get(key))
    return _owner_values(*values)


def _event_owner_values(
    kind: str, event: Mapping[str, Any], raw: Any
) -> tuple[set[str], set[str], set[str]]:
    """Extract explicit owner evidence, including Combat Solver deploy logs."""

    owners_a, watchdogs_a, unknown_a = _mapping_owner_values(event)
    owners_b, watchdogs_b, unknown_b = _mapping_owner_values(raw)
    owners = owners_a | owners_b
    watchdogs = watchdogs_a | watchdogs_b
    unknown = unknown_a | unknown_b
    source_values: list[Any] = []
    for item in (event, raw):
        if isinstance(item, Mapping):
            source_values.extend(
                item.get(key)
                for key in ("source", "execution_source", "action_source")
                if key in item
            )
    source_tokens = {_owner_token(value) for value in source_values if value not in (None, "")}
    if kind in {"deploy_log", "combat_deploy", "solver_deploy", "full_auto_deploy"}:
        owners.add("combat_solver_full_auto")
    if source_tokens & _MOD_DEPLOY_SOURCES:
        owners.add("combat_solver_full_auto")
    return owners, watchdogs, unknown


def _is_mod_deploy_evidence(
    kind: str,
    event: Mapping[str, Any],
    raw: Any,
    *,
    verified: bool = False,
) -> bool:
    if not verified:
        return False
    if kind in {"deploy_log", "combat_deploy", "solver_deploy", "full_auto_deploy"}:
        return True
    for item in (event, raw):
        if not isinstance(item, Mapping):
            continue
        for key in ("source", "execution_source", "action_source"):
            if _owner_token(item.get(key)) in _MOD_DEPLOY_SOURCES:
                return True
    return False


def _event_run_hint(raw: Any) -> str | None:
    """Extract a run id from producer-level deploy/provenance evidence."""

    if not isinstance(raw, Mapping):
        return None
    candidates: list[Any] = [raw.get("run_id")]
    for key in ("run", "state", "snapshot", "current_run"):
        nested = raw.get(key)
        if isinstance(nested, Mapping):
            candidates.append(nested.get("run_id"))
            if key == "run":
                candidates.append(nested.get("id"))
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None

IRONCLAD_NAMES = {
    "IRONCLAD",
    "THE IRONCLAD",
    "CHARACTER.IRONCLAD",
    "铁甲战士",
}
WIN_WORDS = (
    "victory",
    "victor",
    "triumph",
    "you win",
    "you won",
    "won the run",
    "beat the spire",
    "ascended",
    "run complete",
    "cleared",
    "胜利",
    "获胜",
    "通关",
    "战胜",
)
LOSS_WORDS = (
    "defeat",
    "defeated",
    "died",
    "death",
    "lost",
    "slain",
    "fell",
    "perish",
    "run over",
    "失败",
    "死亡",
    "战败",
)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _normalise(value: Any) -> str:
    return " ".join(str(value or "").strip().upper().replace("_", " ").split())


def _is_ironclad(value: Any) -> bool:
    normal = _normalise(value)
    return normal in {_normalise(item) for item in IRONCLAD_NAMES} or (
        normal.endswith(".IRONCLAD")
    )


def _scalar_from(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _run_block(state: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(state, Mapping):
        return {}
    run = state.get("run")
    return dict(run) if isinstance(run, Mapping) else {}


def _player_block(state: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(state, Mapping):
        return {}
    player = state.get("player")
    return dict(player) if isinstance(player, Mapping) else {}


def _state_run_id(state: Mapping[str, Any] | None) -> str | None:
    run = _run_block(state)
    value = _scalar_from(
        state.get("run_id") if isinstance(state, Mapping) else None,
        run.get("run_id"),
    )
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _state_seed(state: Mapping[str, Any] | None) -> Any:
    run = _run_block(state)
    return _scalar_from(
        state.get("seed") if isinstance(state, Mapping) else None,
        run.get("seed"),
    )


def _state_character(state: Mapping[str, Any] | None) -> Any:
    run = _run_block(state)
    player = _player_block(state)
    return _scalar_from(
        player.get("character_id"),
        player.get("character"),
        run.get("character_id"),
        run.get("character"),
        state.get("character_id") if isinstance(state, Mapping) else None,
        state.get("character") if isinstance(state, Mapping) else None,
    )


def _state_ascension(state: Mapping[str, Any] | None) -> Any:
    run = _run_block(state)
    return _scalar_from(
        run.get("ascension"),
        state.get("ascension") if isinstance(state, Mapping) else None,
        (state.get("lobby") or {}).get("ascension")
        if isinstance(state, Mapping) and isinstance(state.get("lobby"), Mapping)
        else None,
    )


def _state_mode(state: Mapping[str, Any] | None) -> Any:
    run = _run_block(state)
    return _scalar_from(
        run.get("game_mode"),
        run.get("mode"),
        state.get("game_mode") if isinstance(state, Mapping) else None,
        state.get("mode") if isinstance(state, Mapping) else None,
    )


def _active_state(state: Mapping[str, Any] | None) -> bool:
    if not isinstance(state, Mapping):
        return False
    run = _run_block(state)
    state_type = str(state.get("state_type") or "")
    if state_type in COMBAT_TYPES or state_type in {
        "map",
        "event",
        "shop",
        "fake_merchant",
        "rest_site",
        "rewards",
        "card_reward",
        "card_select",
        "relic_select",
        "treasure",
        "bundle_select",
        "crystal_sphere",
    }:
        return bool(run) or bool(state.get("player"))
    if state_type == "game_over":
        return bool(run) or bool(state.get("game_over"))
    return bool(run.get("floor") is not None or run.get("act") is not None)


def _timestamp(value: Any) -> float | None:
    """Parse trace ISO timestamps to UTC seconds without trusting clock order."""

    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _percentiles(values: Sequence[float]) -> dict[str, float | int | None]:
    clean = sorted(value for value in values if _finite_number(value) is not None)
    if not clean:
        return {"n": 0, "p50_ms": None, "p95_ms": None}

    def nearest_rank(q: float) -> float:
        index = max(0, min(len(clean) - 1, math.ceil(q * len(clean)) - 1))
        return round(clean[index], 3)

    return {
        "n": len(clean),
        "p50_ms": nearest_rank(0.50),
        "p95_ms": nearest_rank(0.95),
    }


def _interval(hits: int, total: int) -> dict[str, float | int | None]:
    if total <= 0:
        return {"hits": hits, "total": total, "rate": None, "wilson_95_low": None, "wilson_95_high": None}
    low, high = wilson_interval(hits, total)
    return {
        "hits": hits,
        "total": total,
        "rate": hits / total,
        "wilson_95_low": low,
        "wilson_95_high": high,
    }


def _sha256(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest().upper()


def _provided_sha256(value: Any) -> str | None:
    """Accept a producer-recorded digest only when it has the right shape."""

    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) != 64 or any(char not in "0123456789abcdefABCDEF" for char in text):
        return None
    return text.upper()


def _artifact_provenance(
    explicit_path: Path | None,
    supplied: Mapping[str, Any],
    *,
    path_key: str,
    hash_key: str,
) -> tuple[str | None, str | None, bool, str | None]:
    """Resolve and verify an artifact path/hash pair from CLI or trace data.

    Autoplay records both the local path and digest in its session metadata.
    When the same file is available to the assessor, recompute it and reject
    a conflicting producer digest.  A precomputed digest without a path is
    retained as explicitly *unverified* evidence (for example, a supervisor
    running on another host); a path that is present but unreadable is a hard
    blocker rather than silently trusting stale metadata.
    """

    supplied_path = supplied.get(path_key)
    display_path = str(explicit_path) if explicit_path is not None else (
        str(supplied_path) if supplied_path not in (None, "") else None
    )
    recorded_hash = _provided_sha256(supplied.get(hash_key))
    path_value: Path | None = explicit_path
    if path_value is None and isinstance(supplied_path, str) and supplied_path.strip():
        path_value = Path(supplied_path.strip())
    if path_value is not None:
        resolved = path_value if path_value.is_absolute() else PROJECT_ROOT / path_value
        digest = _sha256(resolved)
        if digest is None:
            return display_path, None, False, f"{path_key}_hash_unverifiable"
        if recorded_hash is not None and digest != recorded_hash:
            return display_path, digest, False, f"{path_key}_hash_mismatch"
        return display_path, digest, True, None
    if recorded_hash is not None:
        return display_path, recorded_hash, False, None
    return display_path, None, False, None


def _git_head() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"cannot read JSON {path}: {exc}") from exc


def _load_lock(path: Path) -> dict[str, Any]:
    payload = _load_json(path)
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError(f"unsupported live version lock: {path}")
    if not isinstance(payload.get("game"), dict) or not isinstance(payload.get("bridge"), dict):
        raise ValueError(f"version lock lacks game/bridge objects: {path}")
    return payload


def _load_seed_set(path: Path | None) -> tuple[set[str], str | None, str | None]:
    if path is None:
        return set(), None, None
    payload = _load_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("seeds"), list):
        raise ValueError("seed file must contain a JSON object with a seeds list")
    seeds = payload["seeds"]
    normalised: set[str] = set()
    for value in seeds:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError("seed file contains a non-scalar seed")
        text = str(value).strip()
        if not text:
            raise ValueError("seed file contains an empty seed")
        normalised.add(text)
    return normalised, _sha256(path), str(payload.get("partition", {}).get("name", ""))


def _event_run_identity(raw: Any) -> tuple[str | None, Any, Any, Any, Any]:
    if not isinstance(raw, Mapping):
        return None, None, None, None, None
    run_id = raw.get("run_id")
    return (
        str(run_id).strip() if isinstance(run_id, str) and run_id.strip() else None,
        raw.get("seed"),
        raw.get("character_id") or raw.get("character"),
        raw.get("ascension"),
        raw.get("game_mode") or raw.get("mode"),
    )


def _event_compendium_identity(raw: Any) -> tuple[str | None, Any, Any, Any, Any, bool]:
    if not isinstance(raw, Mapping):
        return None, None, None, None, None, False
    current = raw.get("current_run")
    if not isinstance(current, Mapping):
        return None, None, None, None, None, False
    run_id = current.get("run_id")
    return (
        str(run_id).strip() if isinstance(run_id, str) and run_id.strip() else None,
        current.get("seed"),
        current.get("character_id") or current.get("character"),
        current.get("ascension"),
        current.get("game_mode") or current.get("mode"),
        current.get("is_in_progress") is True,
    )


@dataclass
class _Action:
    sequence: int
    decision_id: str | None
    payload: dict[str, Any]
    state_type: str
    run_id: str | None
    timestamp: float | None
    result_status: str | None = None
    result_timestamp: float | None = None
    result_raw: dict[str, Any] | None = None
    state: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Run:
    run_id: str
    trace_paths: set[str] = field(default_factory=set)
    raw_seed: Any = None
    character: Any = None
    ascension: Any = None
    mode: Any = None
    identity_sources: set[str] = field(default_factory=set)
    compendium_verified: bool = False
    start_seen: bool = False
    terminal_seen: bool = False
    outcome: str = "unknown"
    outcome_source: str | None = None
    max_act: int = 0
    max_floor_by_act: dict[str, int] = field(default_factory=dict)
    state_count: int = 0
    decision_ids: set[str] = field(default_factory=set)
    acted_decision_ids: set[str] = field(default_factory=set)
    action_count: int = 0
    illegal_actions: int = 0
    missing_results: int = 0
    execution_deviations: int = 0
    save_load_used: bool = False
    forbidden_no_sl_markers: set[str] = field(default_factory=set)
    execution_owners: set[str] = field(default_factory=set)
    execution_watchdogs: set[str] = field(default_factory=set)
    unknown_execution_owners: set[str] = field(default_factory=set)
    combat_decision_ids: set[str] = field(default_factory=set)
    combat_http_actions: int = 0
    combat_mod_deploy_actions: int = 0
    issues: list[str] = field(default_factory=list)
    latency: dict[str, list[float]] = field(
        default_factory=lambda: {
            "ordinary_card_play": [],
            "first_action_combat": [],
            "strategic_decision": [],
        }
    )
    _combat_action_keys: set[str] = field(default_factory=set, repr=False)

    def merge_identity(
        self,
        *,
        seed: Any = None,
        character: Any = None,
        ascension: Any = None,
        mode: Any = None,
        source: str,
        compendium: bool = False,
    ) -> None:
        for name, value in (
            ("raw_seed", seed),
            ("character", character),
            ("ascension", ascension),
            ("mode", mode),
        ):
            if value is None or value == "":
                continue
            old = getattr(self, name)
            if old not in (None, "") and str(old) != str(value):
                self.issues.append(f"identity_conflict:{name}:{old!r}!={value!r}")
            else:
                setattr(self, name, value)
        self.identity_sources.add(source)
        if compendium:
            self.compendium_verified = True

    def identity_checks(self, cohort: str) -> list[str]:
        issues: list[str] = []
        if not self.run_id or self.run_id.startswith("RUNID:"):
            issues.append("missing_concrete_run_id")
        if self.raw_seed in (None, ""):
            issues.append("missing_raw_seed")
        if not _is_ironclad(self.character):
            issues.append("character_not_verified_ironclad")
        try:
            ascension = int(self.ascension)
        except (TypeError, ValueError):
            ascension = None
        if ascension != 10:
            issues.append("ascension_not_verified_a10")
        mode = _normalise(self.mode)
        if mode != "STANDARD":
            issues.append("game_mode_not_verified_standard")
        if cohort == "no-sl" and self.save_load_used:
            issues.append("save_load_used_in_no_sl")
        if cohort == "no-sl" and self.forbidden_no_sl_markers:
            issues.append("no_sl_forbidden_operation:" + ",".join(sorted(self.forbidden_no_sl_markers)))
        if self.save_load_used and not self.compendium_verified:
            issues.append("resume_without_compendium_identity")
        return issues

    def to_json(self, cohort: str) -> dict[str, Any]:
        issues = sorted(set(self.issues + self.identity_checks(cohort)))
        decision_total = len(self.decision_ids)
        decision_acted = len(self.acted_decision_ids & self.decision_ids)
        return {
            "run_id": self.run_id,
            "raw_seed": self.raw_seed,
            "identity": {
                "character": self.character,
                "ascension": self.ascension,
                "game_mode": self.mode,
                "sources": sorted(self.identity_sources),
                "compendium_verified": self.compendium_verified,
            },
            "outcome": self.outcome,
            "outcome_source": self.outcome_source,
            "full_run_win": self.outcome == "win" and self.terminal_seen,
            "terminal_seen": self.terminal_seen,
            "max_act": self.max_act,
            "max_floor_by_act": dict(self.max_floor_by_act),
            "act_survival": {
                "act_1": self.max_act >= 2,
                "act_2": self.max_act >= 3,
                "act_3": self.outcome == "win" and self.terminal_seen,
            },
            "decisions": {
                "observed": decision_total,
                "acted": decision_acted,
                "coverage": decision_acted / decision_total if decision_total else None,
            },
            "actions": {
                "total": self.action_count,
                "illegal": self.illegal_actions,
                "missing_results": self.missing_results,
                "execution_deviations": self.execution_deviations,
                "save_load_used": self.save_load_used,
            },
            "execution_owner": (
                next(iter(self.execution_owners))
                if len(self.execution_owners) == 1
                else None
            ),
            "execution_owners": sorted(self.execution_owners),
            "execution_watchdogs": sorted(self.execution_watchdogs),
            "unknown_execution_owners": sorted(self.unknown_execution_owners),
            "combat_execution": {
                "decisions_observed": len(self.combat_decision_ids),
                "http_actions": self.combat_http_actions,
                "mod_deploy_actions": self.combat_mod_deploy_actions,
            },
            "issues": issues,
            "valid_identity": not any(
                issue.startswith((
                    "missing_concrete_run_id",
                    "missing_raw_seed",
                    "character_not_verified",
                    "ascension_not_verified",
                    "game_mode_not_verified",
                    "identity_conflict",
                ))
                for issue in issues
            ),
            "trace_paths": sorted(self.trace_paths),
        }


@dataclass
class TraceInput:
    path: Path
    events: list[dict[str, Any]] = field(default_factory=list)
    parse_errors: list[str] = field(default_factory=list)
    session_metadata: dict[str, Any] = field(default_factory=dict)
    observed_game: dict[str, Any] = field(default_factory=dict)
    observed_mods: list[str] = field(default_factory=list)
    model_metadata: dict[str, Any] = field(default_factory=dict)
    comparison_evidence: dict[str, Any] = field(default_factory=dict)
    # In-memory trust boundary: only events inserted (or matched exactly) by
    # ``merge_comparison_evidence`` after raw-range verification are accepted.
    # Serializing and reloading a trace deliberately loses this trust and
    # requires the comparison directory to be supplied again.
    verified_deploy_event_ids: set[int] = field(default_factory=set, repr=False)


def _evidence_identity_pairs(evidence: Mapping[str, Any]) -> set[tuple[str, str]]:
    """Return the concrete run/seed pairs emitted by the durable adapter."""

    pairs: set[tuple[str, str]] = set()
    for event in evidence.get("events") or []:
        if not isinstance(event, Mapping):
            continue
        raw = event.get("raw")
        if not isinstance(raw, Mapping):
            continue
        run_id = raw.get("run_id")
        seed = raw.get("seed")
        if isinstance(run_id, str) and run_id.strip() and seed not in (None, ""):
            pairs.add((run_id.strip(), str(seed)))
    return pairs


def _trace_identity_pairs(trace: TraceInput) -> dict[str, set[str]]:
    """Collect unique run_id -> seed bindings already present in a trace."""

    bindings: dict[str, set[str]] = defaultdict(set)
    for event in trace.events:
        if not isinstance(event, Mapping):
            continue
        raw = event.get("raw")
        if not isinstance(raw, Mapping):
            continue
        kind = str(event.get("event_type") or "")
        run_id: str | None = None
        seed: Any = None
        if kind == "run_identity":
            run_id, seed, _character, _ascension, _mode = _event_run_identity(raw)
        elif kind == "compendium":
            run_id, seed, _character, _ascension, _mode, active = _event_compendium_identity(raw)
            if not active:
                run_id = None
        elif kind == "state":
            run_id = _state_run_id(raw)
            seed = _state_seed(raw)
        else:
            # A copied deploy/state envelope can still provide a concrete
            # identity, but never use a battle id as a substitute.
            run_id = _event_run_hint(raw)
            seed = raw.get("seed")
        if isinstance(run_id, str) and run_id.strip() and seed not in (None, ""):
            bindings[run_id.strip()].add(str(seed))
    return bindings


def _comparison_matches_trace(trace: TraceInput, evidence: Mapping[str, Any]) -> bool:
    bindings = _trace_identity_pairs(trace)
    for run_id, seed in _evidence_identity_pairs(evidence):
        # A run with two observed seeds is ambiguous even if one happens to
        # match the comparison row.  Never silently attach a foreign batch.
        if bindings.get(run_id) != {seed}:
            return False
    return True


def merge_comparison_evidence(
    trace: TraceInput,
    comparison: Path | str | Mapping[str, Any],
) -> dict[str, Any]:
    """Attach durable comparison deploy events to an assessor trace.

    The adapter emits only authoritative ``deploy_log`` events.  Identical
    events already present in a trace are harmless retries; a same-key event
    with different payload is rejected so a copied/partial journal cannot be
    made to look like a clean full-run execution history.
    """
    if isinstance(comparison, Mapping):
        if not isinstance(comparison, VerifiedComparisonEvidence):
            raise EvidenceIntegrityError(
                "in-memory comparison evidence must come directly from the verified adapter"
            )
        evidence = comparison
    else:
        evidence = adapt_comparison_evidence(comparison)
    if evidence.get("valid") is not True or not isinstance(evidence.get("events"), list):
        raise EvidenceIntegrityError("comparison evidence is not a valid adapter result")
    if not _comparison_matches_trace(trace, evidence):
        raise EvidenceIntegrityError(
            "comparison deploy evidence has no unique run_id/seed identity in target trace"
        )
    existing_by_key: dict[tuple[Any, ...], tuple[str, dict[str, Any]]] = {}
    for event in trace.events:
        if not isinstance(event, Mapping) or event.get("event_type") != "deploy_log":
            continue
        raw = event.get("raw")
        if not isinstance(raw, Mapping):
            continue
        key = (
            raw.get("run_id"),
            str(raw.get("seed")),
            raw.get("battle_id"),
            event.get("decision_id") or raw.get("decision_id"),
            raw.get("turn"),
        )
        existing_by_key[key] = (
            json.dumps(raw, ensure_ascii=False, sort_keys=True),
            event,
        )
    for event in evidence["events"]:
        if not isinstance(event, dict) or event.get("event_type") != "deploy_log":
            raise EvidenceIntegrityError("comparison evidence contains a non-deploy event")
        raw = event.get("raw")
        if not isinstance(raw, dict):
            raise EvidenceIntegrityError("comparison evidence event lacks raw payload")
        key = (
            raw.get("run_id"),
            str(raw.get("seed")),
            raw.get("battle_id"),
            event.get("decision_id") or raw.get("decision_id"),
            raw.get("turn"),
        )
        encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True)
        prior = existing_by_key.get(key)
        if prior is None:
            trace.events.append(event)
            trace.verified_deploy_event_ids.add(id(event))
            existing_by_key[key] = (encoded, event)
        elif prior[0] != encoded:
            raise EvidenceIntegrityError(f"trace/comparison deploy evidence conflict for {key!r}")
        else:
            trace.verified_deploy_event_ids.add(id(prior[1]))
    source = evidence.get("source")
    if isinstance(source, dict):
        trace.comparison_evidence = dict(source)
        trace.session_metadata["comparison_evidence"] = dict(source)
    trace.session_metadata["comparison_evidence_counts"] = dict(evidence.get("counts") or {})
    return evidence


def load_trace(path: Path) -> TraceInput:
    """Read one JSONL trace, preserving parse errors for fail-closed audits."""

    result = TraceInput(path=path)
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        result.parse_errors.append(f"{path}:unreadable:{type(exc).__name__}")
        return result
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError as exc:
            result.parse_errors.append(f"{path}:{line_number}:invalid_json:{exc}")
            continue
        if not isinstance(event, dict):
            result.parse_errors.append(f"{path}:{line_number}:event_not_object")
            continue
        result.events.append(event)
        if event.get("event_type") == "session" and isinstance(event.get("raw"), dict):
            result.session_metadata.update(event["raw"])
            game = event["raw"].get("observed_game")
            if isinstance(game, dict):
                result.observed_game.update(game)
            mods = event["raw"].get("observed_mods")
            if isinstance(mods, list):
                result.observed_mods = sorted(str(value) for value in mods)
            model = event["raw"].get("model") or event["raw"].get("model_metadata")
            if isinstance(model, dict):
                result.model_metadata.update(model)
            # Autoplay/supervisor session metadata is the source of truth for
            # an offline audit.  Keep scalar artifact references and hashes as
            # model metadata too; otherwise a real trace would lose them
            # before ``build_report`` gets to provenance validation.
            for key in (
                "model_id",
                "checkpoint",
                "checkpoint_sha256",
                "data_manifest",
                "data_sha256",
                "emulator",
                "emulator_sha256",
                "git_head",
                "lock_path",
                "bridge_observed",
            ):
                if key in event["raw"]:
                    result.model_metadata[key] = event["raw"].get(key)
        if event.get("event_type") in {"model", "model_metadata", "provenance"} and isinstance(event.get("raw"), dict):
            result.model_metadata.update(event["raw"])
    return result


def _terminal_outcome(state: Mapping[str, Any]) -> tuple[str, str] | None:
    """Classify only explicit terminal evidence; no stale combat flag wins."""

    game_over = state.get("game_over")
    if not isinstance(game_over, Mapping) and state.get("state_type") != "game_over":
        return None
    candidates = [
        state.get("outcome"),
        state.get("result"),
        game_over.get("outcome") if isinstance(game_over, Mapping) else None,
        game_over.get("result") if isinstance(game_over, Mapping) else None,
    ]
    for value in candidates:
        normal = _normalise(value)
        if normal in {"WIN", "VICTORY", "WON", "TRUE"}:
            return "win", "explicit_outcome"
        if normal in {"LOSS", "DEFEAT", "LOST", "FALSE"}:
            return "loss", "explicit_outcome"
    for value in (
        game_over.get("message") if isinstance(game_over, Mapping) else None,
        state.get("message"),
    ):
        text = str(value or "").strip().lower()
        if any(word in text for word in WIN_WORDS):
            return "win", "terminal_message"
        if any(word in text for word in LOSS_WORDS):
            return "loss", "terminal_message"
    player = _player_block(state)
    hp = _finite_number(player.get("hp"))
    if hp is not None and hp <= 0:
        return "loss", "terminal_zero_hp"
    # A positive HP fallback is intentionally not a win: some bridges expose
    # the generic "Run ended" text for both a victory and a defeat.
    return "unknown", "terminal_ambiguous"


def _combat_key(state: Mapping[str, Any]) -> str:
    run = _run_block(state)
    battle = state.get("battle") if isinstance(state.get("battle"), Mapping) else {}
    combat_id = _scalar_from(battle.get("combat_id"), state.get("combat_id"))
    if combat_id is None:
        enemies = battle.get("enemies") or state.get("enemies") or []
        names = []
        for enemy in enemies if isinstance(enemies, list) else []:
            if isinstance(enemy, Mapping):
                names.append(str(enemy.get("entity_id") or enemy.get("id") or enemy.get("name") or "?"))
        combat_id = ",".join(names)
    return f"{run.get('act')}:{run.get('floor')}:{combat_id}"


def _payload_has_forbidden_marker(payload: Mapping[str, Any], state: Mapping[str, Any] | None = None) -> set[str]:
    markers: set[str] = set()
    blob = json.dumps({"payload": payload, "state": state or {}}, ensure_ascii=False).lower()
    for name, needles in {
        "save_load": ("save_load", "load_save", "rewind", "retry_room"),
        "future_rng": ("future_rng", "peek_rng", "hidden_draw", "draw_order"),
    }.items():
        if any(needle in blob for needle in needles):
            markers.add(name)
    return markers


def _new_run(runs: dict[str, _Run], run_id: str, path: Path) -> _Run:
    if run_id not in runs:
        runs[run_id] = _Run(run_id=run_id)
    runs[run_id].trace_paths.add(str(path))
    return runs[run_id]


def _attach_execution_evidence(
    run: _Run,
    owners: Iterable[str],
    watchdogs: Iterable[str],
    unknown: Iterable[str],
) -> None:
    run.execution_owners.update(owners)
    run.execution_watchdogs.update(watchdogs)
    run.unknown_execution_owners.update(unknown)


def analyze_traces(
    traces: Sequence[TraceInput],
    *,
    cohort: str,
    seed_mode: str,
    allowed_seeds: set[str] | None = None,
) -> tuple[list[_Run], dict[str, Any]]:
    """Convert trace events to run records and quality counters."""

    if cohort not in COHORTS:
        raise ValueError(f"unknown cohort {cohort!r}")
    if seed_mode not in SEED_MODES:
        raise ValueError(f"unknown seed mode {seed_mode!r}")
    allowed_seeds = allowed_seeds or set()
    runs: dict[str, _Run] = {}
    all_parse_errors: list[str] = []
    all_events = 0
    session_provenance: list[dict[str, Any]] = []
    execution_owners_seen: set[str] = set()
    execution_watchdogs_seen: set[str] = set()
    unknown_execution_owners_seen: set[str] = set()
    trace_execution_context: dict[str, tuple[set[str], set[str], set[str]]] = {}
    seen_event_fingerprints: set[str] = set()
    unique_events = 0
    actions_observed = 0

    for trace in traces:
        # Event-joining state is scoped to one recorder stream.  Only the
        # resulting ``runs`` map (keyed by concrete run_id) is shared across
        # files.  This prevents a menu/continue or pending action in one file
        # from being silently attributed to the next file's first event.
        current_run_id: str | None = None
        pending_identity: tuple[str | None, Any, Any, Any, Any] | None = None
        pending_resume = False
        pending_mode_selection: str | None = None
        last_state_by_decision: dict[str, tuple[str | None, dict[str, Any], str]] = {}
        actions: list[_Action] = []
        actions_by_decision: dict[str, list[_Action]] = defaultdict(list)
        latest_run_for_trace: dict[str, str | None] = {}
        latest_decision_by_run: dict[str, str] = {}
        all_parse_errors.extend(trace.parse_errors)
        all_events += len(trace.events)
        trace_owners, trace_watchdogs, trace_unknown = _mapping_owner_values(
            trace.session_metadata
        )
        trace_execution_context[str(trace.path)] = (
            trace_owners,
            trace_watchdogs,
            trace_unknown,
        )
        execution_owners_seen.update(trace_owners)
        execution_watchdogs_seen.update(trace_watchdogs)
        unknown_execution_owners_seen.update(trace_unknown)
        session_provenance.append(
            {
                "trace": str(trace.path),
                "metadata": trace.session_metadata,
                "observed_game": trace.observed_game,
                "observed_mods": trace.observed_mods,
                "model_metadata": trace.model_metadata,
                "execution_owners": sorted(trace_owners),
                "execution_watchdogs": sorted(trace_watchdogs),
                "unknown_execution_owners": sorted(trace_unknown),
            }
        )
        for event in sorted(
            trace.events,
            key=lambda item: int(item.get("sequence", 0))
            if isinstance(item.get("sequence", 0), int)
            else 0,
        ):
            kind = str(event.get("event_type") or "")
            raw = event.get("raw")
            verified_deploy = id(event) in trace.verified_deploy_event_ids
            # Multiple recorder files may contain the same run (for example,
            # a supervisor copy and an autoplay copy).  Do not double-count
            # action/result/terminal evidence merely because the trace path
            # differs.  The timestamp/session id is intentionally excluded so
            # copied events still have the same fingerprint.
            if kind != "session":
                fingerprint_payload = {
                    "kind": kind,
                    "sequence": event.get("sequence"),
                    "decision_id": event.get("decision_id"),
                    "state_type": event.get("state_type"),
                    "owner_fields": {
                        key: event.get(key)
                        for key in _OWNER_FINGERPRINT_FIELDS
                        if key in event
                    },
                    "raw": raw,
                }
                try:
                    fingerprint = json.dumps(
                        fingerprint_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                except (TypeError, ValueError):
                    fingerprint = None
                if fingerprint is not None and fingerprint in seen_event_fingerprints:
                    duplicate_run_id = _event_run_hint(raw) or _state_run_id(raw) or current_run_id
                    if duplicate_run_id is not None:
                        # Preserve the audit trail showing which files carried
                        # this run even though their evidence is counted once.
                        _new_run(runs, duplicate_run_id, trace.path)
                    continue
                if fingerprint is not None:
                    seen_event_fingerprints.add(fingerprint)
            unique_events += 1
            event_owners, event_watchdogs, event_unknown = _event_owner_values(
                kind, event, raw
            )
            execution_owners_seen.update(event_owners)
            execution_watchdogs_seen.update(event_watchdogs)
            unknown_execution_owners_seen.update(event_unknown)
            if kind == "run_identity":
                run_id, seed, character, ascension, mode = _event_run_identity(raw)
                if run_id is None:
                    pending_identity = (None, seed, character, ascension, mode)
                    continue
                current_run_id = run_id
                run = _new_run(runs, run_id, trace.path)
                _attach_execution_evidence(
                    run, event_owners, event_watchdogs, event_unknown
                )
                run.merge_identity(
                    seed=seed,
                    character=character,
                    ascension=ascension,
                    mode=mode,
                    source="run_identity",
                    compendium=isinstance(raw, Mapping) and bool(raw.get("save_source")),
                )
                if pending_resume and run.compendium_verified:
                    pending_resume = False
                pending_identity = None
                continue
            if kind == "compendium":
                run_id, seed, character, ascension, mode, active = _event_compendium_identity(raw)
                if run_id is not None and active:
                    current_run_id = run_id
                    run = _new_run(runs, run_id, trace.path)
                    _attach_execution_evidence(
                        run, event_owners, event_watchdogs, event_unknown
                    )
                    run.merge_identity(
                        seed=seed,
                        character=character,
                        ascension=ascension,
                        mode=mode,
                        source="compendium.current_run",
                        compendium=True,
                    )
                    if pending_resume:
                        pending_resume = False
                continue
            if kind == "machine_verification" and isinstance(raw, Mapping):
                if current_run_id is not None:
                    run = _new_run(runs, current_run_id, trace.path)
                    _attach_execution_evidence(
                        run, event_owners, event_watchdogs, event_unknown
                    )
                    run.merge_identity(
                        character=raw.get("character_id") or raw.get("character"),
                        ascension=raw.get("ascension"),
                        source="machine_verification",
                    )
                continue
            if kind == "state" and isinstance(raw, dict):
                state = raw
                state_run_id = _state_run_id(state)
                identity_run_id = pending_identity[0] if pending_identity else None
                run_id = state_run_id or identity_run_id or current_run_id
                if _active_state(state) and run_id is None:
                    # Keep an explicit invalid candidate instead of silently
                    # dropping an unknown saved run from the denominator.
                    run_id = f"UNATTRIBUTED:{trace.path.name}:{event.get('sequence', len(runs))}"
                if run_id is not None:
                    current_run_id = run_id
                    run = _new_run(runs, run_id, trace.path)
                    _attach_execution_evidence(
                        run, event_owners, event_watchdogs, event_unknown
                    )
                    if pending_identity:
                        _, seed, character, ascension, mode = pending_identity
                        run.merge_identity(
                            seed=seed,
                            character=character,
                            ascension=ascension,
                            mode=mode,
                            source="run_identity",
                            compendium=False,
                        )
                        pending_identity = None
                    run.merge_identity(
                        seed=_state_seed(state),
                        character=_state_character(state),
                        ascension=_state_ascension(state),
                        mode=_state_mode(state) or pending_mode_selection,
                        source="state",
                    )
                    state_type = str(state.get("state_type") or "")
                    if _active_state(state):
                        run.start_seen = True
                        run.state_count += 1
                        act = _finite_number(_run_block(state).get("act"))
                        floor = _finite_number(_run_block(state).get("floor"))
                        if act is not None and act >= 1:
                            act_int = int(act)
                            run.max_act = max(run.max_act, act_int)
                            if floor is not None:
                                key = str(act_int)
                                run.max_floor_by_act[key] = max(
                                    int(floor), run.max_floor_by_act.get(key, 0)
                                )
                        decision = str(event.get("decision_id") or "")
                        if state_type in DECISION_TYPES and decision:
                            run.decision_ids.add(decision)
                            last_state_by_decision[decision] = (run_id, state, state_type)
                            latest_decision_by_run[run_id] = decision
                            if state_type in COMBAT_TYPES:
                                run.combat_decision_ids.add(decision)
                        latest_run_for_trace[str(trace.path)] = run_id
                        terminal = _terminal_outcome(state)
                        if terminal is not None:
                            outcome, source = terminal
                            run.terminal_seen = True
                            if run.outcome not in {"unknown", outcome}:
                                run.issues.append("conflicting_terminal_outcomes")
                            elif run.outcome == "unknown" or outcome != "unknown":
                                run.outcome = outcome
                                run.outcome_source = source
                continue
            if kind == "action" and isinstance(raw, dict):
                decision = str(event.get("decision_id") or "") or None
                state_info = last_state_by_decision.get(decision or "")
                run_id = state_info[0] if state_info else current_run_id
                state = state_info[1] if state_info else {}
                state_type = str(event.get("state_type") or (state_info[2] if state_info else ""))
                trace_owners, trace_watchdogs, trace_unknown = trace_execution_context.get(
                    str(trace.path), (set(), set(), set())
                )
                effective_owners = event_owners or trace_owners
                effective_watchdogs = event_watchdogs or trace_watchdogs
                effective_unknown = event_unknown or trace_unknown
                # A deploy_log is already authoritative Mod execution
                # evidence.  It is not an HTTP POST and therefore must not
                # create a synthetic missing-result/HTTP-combat action.
                if _is_mod_deploy_evidence(
                    kind, event, raw, verified=verified_deploy
                ):
                    run_id = _event_run_hint(raw) or run_id
                    if run_id is None:
                        run_id = f"UNATTRIBUTED_DEPLOY:{trace.path.name}:{event.get('sequence', len(actions))}"
                    run = _new_run(runs, run_id, trace.path)
                    _attach_execution_evidence(
                        run,
                        effective_owners | {"combat_solver_full_auto"},
                        effective_watchdogs,
                        effective_unknown,
                    )
                    if isinstance(raw, Mapping):
                        # Durable comparison evidence carries the run seed even
                        # when the companion trace was copied without an
                        # identity state.  Merge it as evidence, never infer it
                        # from a battle id or from an ``inferred`` execution.
                        run.merge_identity(
                            seed=raw.get("seed"),
                            source="comparison.deploy_log",
                        )
                    run.action_count += 1
                    if decision:
                        run.acted_decision_ids.add(decision)
                    elif run_id in latest_decision_by_run:
                        run.acted_decision_ids.add(latest_decision_by_run[run_id])
                    if state_type in COMBAT_TYPES or isinstance(raw.get("turn"), (int, str)):
                        run.combat_mod_deploy_actions += 1
                    status = str(raw.get("status") or raw.get("result") or "").lower()
                    if status in {"error", "failed", "failure", "rejected", "illegal"}:
                        run.illegal_actions += 1
                    continue
                payload = dict(raw)
                option = _normalise(payload.get("option"))
                if payload.get("action") == "menu_select" and option == "STANDARD":
                    pending_mode_selection = "standard"
                if payload.get("action") == "menu_select" and option in {"CONTINUE", "LOAD"}:
                    pending_resume = True
                    if run_id is not None:
                        _new_run(runs, run_id, trace.path).save_load_used = True
                if run_id is None:
                    run_id = f"UNATTRIBUTED_ACTION:{trace.path.name}:{event.get('sequence', len(actions))}"
                run = _new_run(runs, run_id, trace.path)
                _attach_execution_evidence(
                    run, effective_owners, effective_watchdogs, effective_unknown
                )
                action = _Action(
                    sequence=int(event.get("sequence", len(actions))),
                    decision_id=decision,
                    payload=payload,
                    state_type=state_type,
                    run_id=run_id,
                    timestamp=_timestamp(event.get("timestamp_utc")),
                    state=state,
                )
                actions.append(action)
                actions_observed += 1
                if decision:
                    actions_by_decision[decision].append(action)
                    run.acted_decision_ids.add(decision)
                run.action_count += 1
                if state_type in COMBAT_TYPES:
                    # Actions emitted by TraceRecorder are bridge/HTTP
                    # actions.  Under a declared Mod full-auto owner they are
                    # the forbidden second execution path; count them so the
                    # report can fail closed instead of treating the absence
                    # of a deploy log as a player omission.
                    run.combat_http_actions += 1
                run.forbidden_no_sl_markers.update(_payload_has_forbidden_marker(payload, state))
                # Explicit producer fields, if present, are accepted only as
                # evidence of a deviation; no inference from a route is made.
                expected = payload.get("expected_action") or payload.get("advised_action")
                if expected is not None and expected != payload:
                    run.execution_deviations += 1
                if state_type in COMBAT_TYPES:
                    key = _combat_key(state)
                    if key not in run._combat_action_keys:
                        run._combat_action_keys.add(key)
                        action._first_combat = True  # type: ignore[attr-defined]
                else:
                    action._first_combat = False  # type: ignore[attr-defined]
                continue
            if kind == "result" and isinstance(raw, dict):
                decision = str(event.get("decision_id") or "") or None
                queue = actions_by_decision.get(decision or "")
                action = queue.pop(0) if queue else None
                if action is None:
                    continue
                action.result_status = str(raw.get("status") or "")
                action.result_timestamp = _timestamp(event.get("timestamp_utc"))
                action.result_raw = raw
                run = _new_run(runs, action.run_id or f"UNATTRIBUTED_RESULT:{trace.path.name}", trace.path)
                if action.result_status.lower() not in {"ok", "applied", "terminal"} or raw.get("observed") is False:
                    run.illegal_actions += 1
                if isinstance(raw.get("execution_deviation"), bool) and raw["execution_deviation"]:
                    run.execution_deviations += 1
                if action.timestamp is not None and action.result_timestamp is not None:
                    delta = max(0.0, (action.result_timestamp - action.timestamp) * 1000.0)
                    if action.state_type in COMBAT_TYPES:
                        run.latency["ordinary_card_play"].append(delta)
                        if getattr(action, "_first_combat", False):
                            run.latency["first_action_combat"].append(delta)
                    else:
                        run.latency["strategic_decision"].append(delta)
                continue
            if kind in {"deploy_log", "combat_deploy", "solver_deploy", "full_auto_deploy"}:
                run_id = _event_run_hint(raw) or current_run_id
                if run_id is None:
                    run_id = f"UNATTRIBUTED_DEPLOY:{trace.path.name}:{event.get('sequence', len(actions))}"
                run = _new_run(runs, run_id, trace.path)
                if not verified_deploy:
                    run.issues.append("unverified_combat_deploy_evidence")
                    continue
                _attach_execution_evidence(
                    run, event_owners, event_watchdogs, event_unknown
                )
                if isinstance(raw, Mapping):
                    run.merge_identity(seed=raw.get("seed"), source="deploy_log")
                run.action_count += 1
                decision = str(event.get("decision_id") or "") or None
                if decision:
                    run.acted_decision_ids.add(decision)
                elif run_id in latest_decision_by_run:
                    run.acted_decision_ids.add(latest_decision_by_run[run_id])
                run.combat_mod_deploy_actions += 1
                status = str(raw.get("status") or raw.get("result") or "").lower()
                if status in {"error", "failed", "failure", "rejected", "illegal"}:
                    run.illegal_actions += 1
                continue
            if kind in {"error", "failure", "timeout", "crash"}:
                run_id = current_run_id
                if run_id is not None:
                    run = _new_run(runs, run_id, trace.path)
                    _attach_execution_evidence(
                        run, event_owners, event_watchdogs, event_unknown
                    )
                    label = kind
                    if isinstance(raw, Mapping):
                        label = str(raw.get("reason") or raw.get("error") or kind)
                    run.issues.append(f"runtime_{label.lower()}")
                continue
            # Optional producer-level evidence of forbidden operations.
            if kind in {"save_load", "rewind", "retry_room", "future_rng", "hidden_draw"}:
                if current_run_id is not None:
                    run = _new_run(runs, current_run_id, trace.path)
                    _attach_execution_evidence(
                        run, event_owners, event_watchdogs, event_unknown
                    )
                    run.save_load_used |= kind in {"save_load", "rewind", "retry_room"}
                    run.forbidden_no_sl_markers.add(kind)
        for action in actions:
            if action.result_status is None and action.run_id is not None:
                run = runs.get(action.run_id)
                if run is not None:
                    run.missing_results += 1
        # Every trace with an explicit continue but no compendium identity
        # must retain the blocker on the active run, including traces that
        # ended at the menu before a state was observed.  This check stays
        # inside the trace loop so it cannot leak into the next file.
        if pending_resume and current_run_id is not None:
            runs[current_run_id].issues.append("resume_without_compendium_identity")
    # Session ownership applies to every run in that trace.  Applying it
    # after event joining also covers traces that start with a menu and only
    # later expose a concrete run_id.
    for run in runs.values():
        for path in run.trace_paths:
            owners, watchdogs, unknown = trace_execution_context.get(
                path, (set(), set(), set())
            )
            _attach_execution_evidence(run, owners, watchdogs, unknown)
    quality = {
        "parse_errors": sorted(set(all_parse_errors)),
        "event_count": all_events,
        "unique_event_count": unique_events,
        "session_provenance": session_provenance,
        # Keep the durable comparison provenance visible in the aggregate
        # report.  The source hashes are copied from the adapter; this report
        # never recomputes or invents deployment evidence from inferred turns.
        "comparison_evidence": [
            dict(trace.comparison_evidence)
            for trace in traces
            if trace.comparison_evidence
        ],
        "actions_observed": actions_observed,
        "seed_mode": seed_mode,
        "cohort": cohort,
        "allowed_seed_count": len(allowed_seeds),
        "execution_owners": sorted(execution_owners_seen),
        "execution_watchdogs": sorted(execution_watchdogs_seen),
        "unknown_execution_owners": sorted(unknown_execution_owners_seen),
    }
    return list(runs.values()), quality


def _provenance_single(
    trace: TraceInput | None,
    *,
    lock: dict[str, Any],
    provenance: Mapping[str, Any] | None,
    checkpoint: Path | None,
    data_manifest: Path | None,
    emulator: Path | None,
    model_id: str | None,
) -> tuple[dict[str, Any], list[str]]:
    """Validate one trace's provenance without borrowing another trace's data."""

    supplied = dict(provenance or {})
    trace_metadata = dict(trace.model_metadata) if trace is not None else {}
    if trace is not None:
        supplied.update(trace_metadata)
        # Game/mod/bridge values must come from this trace itself.  A common
        # CLI provenance object may supplement the plan, but cannot mask a
        # missing per-trace observation in a real acceptance report.
        observed_game = dict(trace.observed_game)
        observed_mods = list(trace.observed_mods)
        bridge_observed = trace_metadata.get("bridge_observed")
        artifact_supplied = dict(trace_metadata)
        if checkpoint is not None:
            artifact_supplied["checkpoint"] = str(checkpoint)
        if data_manifest is not None:
            artifact_supplied["data_manifest"] = str(data_manifest)
        if emulator is not None:
            artifact_supplied["emulator"] = str(emulator)
        # A trace-level model id/hash is mandatory.  Explicit CLI values are
        # not allowed to conceal a second trace that omitted its provenance.
        model_value = trace_metadata.get("model_id") or trace_metadata.get("model")
        git_head = trace_metadata.get("git_head")
    else:
        observed_game = dict(supplied.get("game_observed") or {})
        observed_mods = list(
            supplied.get("allowed_mods_observed")
            or supplied.get("observed_mods")
            or []
        )
        bridge_observed = supplied.get("bridge_observed")
        artifact_supplied = supplied
        model_value = model_id or supplied.get("model_id") or supplied.get("model")
        git_head = supplied.get("git_head") or _git_head()
    game_expected = dict(lock.get("game") or {})
    bridge_expected = dict(lock.get("bridge") or {})
    game_keys = (
        "app_id",
        "steam_build_id",
        "version",
        "commit",
        "branch",
        "main_assembly_hash",
    )
    game_match = bool(observed_game) and all(
        str(observed_game.get(key)) == str(game_expected.get(key))
        for key in game_keys
        if game_expected.get(key) is not None
    )
    expected_mods = sorted(
        str(value)
        for value in (
            (lock.get("evaluation_environment") or {}).get("allowed_mod_ids")
            or []
        )
    )
    observed_mod_set = sorted(set(str(value) for value in observed_mods))
    mods_match = bool(observed_mod_set) and observed_mod_set == expected_mods
    if isinstance(model_value, Mapping):
        model_value = model_value.get("model_id") or model_value.get("id")
    model_value = str(model_value) if model_value not in (None, "") else None
    bridge_match = False
    if isinstance(bridge_observed, Mapping):
        observed_status = str(bridge_observed.get("status") or "").lower()
        observed_message = str(bridge_observed.get("message") or "")
        expected_version = str(bridge_expected.get("version") or "")
        bridge_match = (
            observed_status == "ok"
            and bool(expected_version)
            and (
                expected_version in observed_message
                or f"v{expected_version}" in observed_message
            )
        )
    checkpoint_path, checkpoint_hash, checkpoint_verified, checkpoint_issue = _artifact_provenance(
        checkpoint if trace is None else None,
        artifact_supplied,
        path_key="checkpoint",
        hash_key="checkpoint_sha256",
    )
    data_path, data_hash, data_verified, data_issue = _artifact_provenance(
        data_manifest if trace is None else None,
        artifact_supplied,
        path_key="data_manifest",
        hash_key="data_sha256",
    )
    emulator_path, emulator_hash, emulator_verified, emulator_issue = _artifact_provenance(
        emulator if trace is None else None,
        artifact_supplied,
        path_key="emulator",
        hash_key="emulator_sha256",
    )
    issues: list[str] = []
    if not game_match:
        issues.append("game_identity_missing_or_mismatch")
    if not mods_match:
        issues.append("allowed_mod_inventory_missing_or_mismatch")
    if not bridge_match:
        issues.append("bridge_identity_missing_or_mismatch")
    if not model_value:
        issues.append("model_id_missing")
    elif any(
        marker in model_value.lower()
        for marker in ("heuristic", "baseline", "untrained", "smoke")
    ):
        issues.append("model_not_trained")
    if checkpoint_issue:
        issues.append(checkpoint_issue)
    if data_issue:
        issues.append(data_issue)
    if emulator_issue:
        issues.append(emulator_issue)
    if not checkpoint_hash:
        issues.append("checkpoint_hash_missing")
    if not data_hash:
        issues.append("data_hash_missing")
    # Formal local acceptance requires a path-backed, recomputed digest.  A
    # bare producer hash is useful diagnostic evidence but is not trusted as a
    # gate without a signature mechanism (none exists in this project).
    if checkpoint_hash and not checkpoint_verified:
        issues.append("checkpoint_hash_unverified")
    if data_hash and not data_verified:
        issues.append("data_hash_unverified")
    if emulator_hash and not emulator_verified:
        issues.append("emulator_hash_unverified")
    if not git_head:
        issues.append("git_head_missing")
    report = {
        "game_expected": game_expected,
        "game_observed": observed_game,
        "game_match": game_match,
        "bridge_expected": bridge_expected,
        "bridge_observed": bridge_observed,
        "bridge_match": bridge_match,
        "allowed_mods_expected": expected_mods,
        "allowed_mods_observed": observed_mod_set,
        "mods_match": mods_match,
        "model_id": model_value,
        "checkpoint": checkpoint_path,
        "checkpoint_sha256": checkpoint_hash,
        "checkpoint_hash_verified": checkpoint_verified,
        "data_manifest": data_path,
        "data_sha256": data_hash,
        "data_hash_verified": data_verified,
        "emulator": emulator_path,
        "emulator_sha256": emulator_hash,
        "emulator_hash_verified": emulator_verified,
        "git_head": git_head,
        "lock_path": str(
            supplied.get("lock_path")
            or PROJECT_ROOT / "config" / "live_version.lock.json"
        ),
    }
    return report, sorted(set(issues))


def _provenance_report(
    traces: Sequence[TraceInput],
    *,
    lock: dict[str, Any],
    provenance: Mapping[str, Any] | None,
    checkpoint: Path | None,
    data_manifest: Path | None,
    emulator: Path | None,
    model_id: str | None,
) -> tuple[dict[str, Any], list[str]]:
    """Validate every trace and require one consistent provenance identity."""

    if not traces:
        return _provenance_single(
            None,
            lock=lock,
            provenance=provenance,
            checkpoint=checkpoint,
            data_manifest=data_manifest,
            emulator=emulator,
            model_id=model_id,
        )
    trace_reports: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    all_issues: list[str] = []
    for trace in traces:
        report, issues = _provenance_single(
            trace,
            lock=lock,
            provenance=provenance,
            checkpoint=checkpoint,
            data_manifest=data_manifest,
            emulator=emulator,
            model_id=model_id,
        )
        reports.append(report)
        all_issues.extend(issues)
        trace_reports.append(
            {
                "trace": str(trace.path),
                "issues": issues,
                "game_match": report["game_match"],
                "mods_match": report["mods_match"],
                "bridge_match": report["bridge_match"],
                "model_id": report["model_id"],
                "checkpoint_sha256": report["checkpoint_sha256"],
                "checkpoint_hash_verified": report["checkpoint_hash_verified"],
                "data_sha256": report["data_sha256"],
                "data_hash_verified": report["data_hash_verified"],
            }
        )
    first = reports[0]
    consistency_fields = {
        "game_provenance_inconsistent": lambda item: tuple(
            str(item["game_observed"].get(key))
            for key in ("app_id", "steam_build_id", "version", "commit", "branch", "main_assembly_hash")
        ),
        "allowed_mod_provenance_inconsistent": lambda item: tuple(
            item["allowed_mods_observed"]
        ),
        "bridge_provenance_inconsistent": lambda item: (
            item["bridge_match"],
            (item["bridge_observed"] or {}).get("status")
            if isinstance(item["bridge_observed"], Mapping)
            else None,
            (item["bridge_observed"] or {}).get("message")
            if isinstance(item["bridge_observed"], Mapping)
            else None,
        ),
        "model_provenance_inconsistent": lambda item: item["model_id"],
        "checkpoint_provenance_inconsistent": lambda item: (
            item["checkpoint_sha256"],
            item["checkpoint_hash_verified"],
        ),
        "data_provenance_inconsistent": lambda item: (
            item["data_sha256"],
            item["data_hash_verified"],
        ),
    }
    for issue, selector in consistency_fields.items():
        values = [selector(item) for item in reports]
        if any(value != values[0] for value in values[1:]):
            all_issues.append(issue)
    aggregate = dict(first)
    aggregate["trace_count"] = len(reports)
    aggregate["trace_provenance_consistent"] = not any(
        issue.endswith("_inconsistent") for issue in all_issues
    )
    aggregate["trace_reports"] = trace_reports
    return aggregate, sorted(set(all_issues))


def build_report(
    runs: Sequence[_Run],
    quality: Mapping[str, Any],
    *,
    cohort: str,
    seed_mode: str,
    pilot_runs: int,
    formal_runs: int,
    lock: dict[str, Any],
    provenance: Mapping[str, Any] | None = None,
    checkpoint: Path | None = None,
    data_manifest: Path | None = None,
    emulator: Path | None = None,
    model_id: str | None = None,
    allowed_seeds: set[str] | None = None,
    seed_file: Path | None = None,
    seed_file_sha256: str | None = None,
) -> dict[str, Any]:
    allowed_seeds = allowed_seeds or set()
    # A plan has no trace evidence, so it must use the conservative empty
    # input and expose every missing precondition.  A real report has trace
    # evidence, so validate that evidence exactly once.  Merging an initial
    # empty-input result with the trace result would retain false
    # ``*_missing`` blockers even when the trace session supplied them.
    trace_inputs = [
        TraceInput(
            path=Path(meta.get("trace", "")),
            observed_game=meta.get("observed_game", {}),
            observed_mods=meta.get("observed_mods", []),
            model_metadata=meta.get("model_metadata", {}),
        )
        for meta in quality.get("session_provenance", [])
        if meta.get("trace")
    ]
    provenance_inputs = [] if quality.get("plan") else trace_inputs
    provenance_report, provenance_issues = _provenance_report(
        provenance_inputs,
        lock=lock,
        provenance=provenance,
        checkpoint=checkpoint,
        data_manifest=data_manifest,
        emulator=emulator,
        model_id=model_id,
    )
    rows = [run.to_json(cohort) for run in runs]
    n_runs = len(rows)
    wins = sum(int(row["full_run_win"]) for row in rows)
    losses = sum(int(row["outcome"] == "loss" and row["terminal_seen"]) for row in rows)
    unknown = n_runs - wins - losses
    valid_identity = sum(int(row["valid_identity"]) for row in rows)
    identity_invalid = n_runs - valid_identity
    # A run can be excluded for a reason that is only knowable while joining
    # its event stream (for example, a menu ``continue`` without a fresh
    # compendium identity).  Preserve those concrete run-level reasons in the
    # aggregate verdict; otherwise a report would expose an invalid run but
    # silently lose the evidence that made it invalid.
    run_issues = sorted(
        {
            str(issue)
            for row in rows
            for issue in (row.get("issues") or [])
            if str(issue)
        }
    )
    execution_owners = sorted(
        set(str(value) for value in (quality.get("execution_owners") or []))
        | {
            str(value)
            for row in rows
            for value in (row.get("execution_owners") or [])
        }
    )
    execution_watchdogs = sorted(
        set(str(value) for value in (quality.get("execution_watchdogs") or []))
        | {
            str(value)
            for row in rows
            for value in (row.get("execution_watchdogs") or [])
        }
    )
    unknown_execution_owners = sorted(
        set(str(value) for value in (quality.get("unknown_execution_owners") or []))
        | {
            str(value)
            for row in rows
            for value in (row.get("unknown_execution_owners") or [])
        }
    )
    owner_issues: list[str] = []
    if not execution_owners:
        owner_issues.append("execution_owner_missing")
    if len(execution_owners) > 1:
        owner_issues.append("execution_owner_conflict")
    if unknown_execution_owners:
        owner_issues.append("execution_owner_unknown")
    for row in rows:
        if not row.get("execution_owners") and not row.get("unknown_execution_owners"):
            owner_issues.append(f"execution_owner_missing_for_run:{row['run_id']}")
        row_owners = {str(value) for value in (row.get("execution_owners") or [])}
        combat = row.get("combat_execution") or {}
        if (
            "combat_solver_full_auto" in row_owners
            and int(combat.get("decisions_observed") or 0) > 0
            and int(combat.get("mod_deploy_actions") or 0) == 0
        ):
            owner_issues.append(
                f"combat_deploy_log_missing_for_run:{row['run_id']}"
            )
    combat_http_actions = sum(
        int((row.get("combat_execution") or {}).get("http_actions") or 0)
        for row in rows
    )
    combat_mod_deploy_actions = sum(
        int((row.get("combat_execution") or {}).get("mod_deploy_actions") or 0)
        for row in rows
    )
    combat_decisions = sum(
        int((row.get("combat_execution") or {}).get("decisions_observed") or 0)
        for row in rows
    )
    if "combat_solver_full_auto" in execution_owners and combat_http_actions:
        owner_issues.append("combat_http_actions_under_full_auto")
    if "combat_solver_full_auto" in execution_owners and combat_decisions and not combat_mod_deploy_actions:
        owner_issues.append("combat_deploy_log_missing")
    if "http_route_executor" in execution_owners and combat_mod_deploy_actions:
        owner_issues.append("combat_mod_deploy_actions_under_http_owner")
    owner_issues = sorted(set(owner_issues))
    raw_seed_values = {str(row["raw_seed"]).strip() for row in rows if row["raw_seed"] not in (None, "")}
    seed_issues: list[str] = []
    if any(row["raw_seed"] in (None, "") for row in rows):
        seed_issues.append("raw_seed_missing")
    if seed_mode == "fixed":
        if seed_file is None or not allowed_seeds:
            seed_issues.append("fixed_seed_file_missing")
        elif not raw_seed_values.issubset(allowed_seeds):
            seed_issues.append("observed_seed_outside_registered_partition")
        if len(raw_seed_values) != n_runs:
            seed_issues.append("duplicate_or_missing_run_seeds")
    decision_total = sum(int(row["decisions"]["observed"]) for row in rows)
    decision_acted = sum(int(row["decisions"]["acted"]) for row in rows)
    illegal = sum(int(row["actions"]["illegal"]) for row in rows)
    missing_results = sum(int(row["actions"]["missing_results"]) for row in rows)
    deviations = sum(int(row["actions"]["execution_deviations"]) for row in rows)
    action_total = sum(int(row["actions"]["total"]) for row in rows)
    act_metrics: dict[str, Any] = {}
    for act in ACTS:
        hits = sum(int(row["act_survival"][f"act_{act}"]) for row in rows)
        act_metrics[f"act_{act}"] = _interval(hits, n_runs)
    latency_values = {
        "ordinary_card_play": [value for run in runs for value in run.latency["ordinary_card_play"]],
        "first_action_combat": [value for run in runs for value in run.latency["first_action_combat"]],
        "strategic_decision": [value for run in runs for value in run.latency["strategic_decision"]],
    }
    quality_issues = list(quality.get("parse_errors") or [])
    if not rows:
        quality_issues.append("no_run_records")
    if unknown:
        quality_issues.append("unknown_or_nonterminal_runs")
    if identity_invalid:
        quality_issues.append("invalid_run_identity")
    if illegal:
        quality_issues.append("illegal_actions_observed")
    if missing_results:
        quality_issues.append("action_results_missing")
    if deviations:
        quality_issues.append("player_execution_deviation_observed")
    quality_issues.extend(run_issues)
    quality_issues.extend(owner_issues)
    quality_issues.extend(seed_issues)
    quality_issues.extend(provenance_issues)
    if cohort == "no-sl" and any(row["actions"]["save_load_used"] for row in rows):
        quality_issues.append("no_sl_save_load_used")
    # The trace must explicitly identify no-SL.  Merely not seeing a continue
    # action cannot prove that a hidden save/load or RNG intervention did not
    # happen.
    if cohort == "no-sl":
        session_cohorts = {
            str(meta.get("metadata", {}).get("cohort") or "").lower()
            for meta in quality.get("session_provenance", [])
        }
        if "no-sl" not in session_cohorts:
            quality_issues.append("no_sl_cohort_marker_missing")
    if any(row["outcome"] == "unknown" and row["terminal_seen"] for row in rows):
        quality_issues.append("ambiguous_terminal_outcome")
    gate_results = {
        "minimum_pilot_runs": {
            "value": n_runs,
            "limit": pilot_runs,
            "passed": n_runs >= pilot_runs,
        },
        "minimum_formal_runs": {
            "value": n_runs,
            "limit": formal_runs,
            "passed": n_runs >= formal_runs,
        },
        "terminal_outcomes_complete": {
            "value": unknown,
            "limit": 0,
            "passed": unknown == 0 and n_runs > 0,
        },
        "identity_valid": {
            "value": identity_invalid,
            "limit": 0,
            "passed": identity_invalid == 0 and n_runs > 0,
        },
        "decision_coverage": {
            "value": decision_acted / decision_total if decision_total else None,
            "limit": 0.90,
            "passed": decision_total > 0 and decision_acted / decision_total >= 0.90,
        },
        "illegal_actions": {
            "value": illegal,
            "limit": 0,
            "passed": illegal == 0,
        },
        "execution_deviations": {
            "value": deviations,
            "limit": 0,
            "passed": deviations == 0,
        },
        "provenance_complete": {
            "value": provenance_issues,
            "limit": 0,
            "passed": not provenance_issues,
        },
        "execution_owner_unique": {
            "value": execution_owners,
            "limit": 1,
            "passed": bool(n_runs and len(execution_owners) == 1 and not owner_issues),
        },
        "seed_evidence_complete": {
            "value": seed_issues,
            "limit": 0,
            "passed": not seed_issues,
        },
    }
    blockers = sorted(set(quality_issues))
    pilot_ready = n_runs >= pilot_runs and not blockers
    formal_ready = n_runs >= formal_runs and not blockers
    # The target is a point-estimate requirement; the Wilson interval is always
    # reported and never used to erase uncertainty.  All quality gates still
    # apply before an acceptance claim can be true.
    win_interval = _interval(wins, n_runs)
    pilot_gate = dict(gate_results["minimum_pilot_runs"])
    pilot_gate["accepted"] = pilot_ready and (win_interval["rate"] or 0.0) >= 0.50
    formal_gate = dict(gate_results["minimum_formal_runs"])
    formal_gate["accepted"] = formal_ready and (win_interval["rate"] or 0.0) >= 0.50
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": _now(),
        "report_type": "ironclad_a10_full_run_acceptance",
        "target": {
            "character": "IRONCLAD",
            "ascension": 10,
            "game_mode": "standard",
            "game_lock": provenance_report["lock_path"],
        },
        "cohort": cohort,
        "seed_mode": seed_mode,
        "seed_registration": {
            "file": str(seed_file) if seed_file else None,
            "sha256": seed_file_sha256,
            "partition": sorted(allowed_seeds),
            "observed_raw_seeds": sorted(raw_seed_values),
        },
        "execution": {
            "owner": execution_owners[0] if len(execution_owners) == 1 else None,
            "owners": execution_owners,
            "watchdogs": execution_watchdogs,
            "unknown_owners": unknown_execution_owners,
            "combat_http_actions": combat_http_actions,
            "combat_mod_deploy_actions": combat_mod_deploy_actions,
            "owner_issues": owner_issues,
        },
        "execution_evidence": [
            dict(item)
            for item in (quality.get("comparison_evidence") or [])
            if isinstance(item, Mapping)
        ],
        "provenance": provenance_report,
        "runs": rows,
        "counts": {
            "runs": n_runs,
            "valid_identity_runs": valid_identity,
            "identity_invalid_runs": identity_invalid,
            "full_run_wins": wins,
            "terminal_losses": losses,
            "unknown_or_incomplete": unknown,
            "decision_states": decision_total,
            "decisions_acted": decision_acted,
            "actions": action_total,
            "illegal_actions": illegal,
            "missing_action_results": missing_results,
            "player_execution_deviations": deviations,
            "save_load_runs": sum(int(row["actions"]["save_load_used"]) for row in rows),
        },
        "full_run_win_rate": win_interval,
        "act_survival": act_metrics,
        "latency_ms": {key: _percentiles(value) for key, value in latency_values.items()},
        "quality": {
            **dict(quality),
            "blockers": blockers,
            "execution_owner": execution_owners[0] if len(execution_owners) == 1 else None,
            "run_issues": run_issues,
            "provenance_issues": provenance_issues,
            "seed_issues": seed_issues,
        },
        "gates": gate_results,
        "pilot": {
            "required_runs": pilot_runs,
            "ready": pilot_ready,
            "gate": pilot_gate,
        },
        "formal": {
            "required_runs": formal_runs,
            "ready": formal_ready,
            "gate": formal_gate,
        },
        "verdict": {
            "decidable": bool(n_runs and not blockers),
            "accepted": bool(formal_gate["accepted"]),
            "accepted_cohort": cohort,
            "blockers": blockers,
            "disclaimer": (
                "Battle wins and simulator wins are not full-run wins. "
                "This report counts only terminal game evidence from the supplied trace."
            ),
        },
    }


def build_plan(
    *,
    cohort: str,
    seed_mode: str,
    pilot_runs: int,
    formal_runs: int,
    lock_path: Path,
    seed_file: Path | None = None,
) -> dict[str, Any]:
    """Create a no-side-effect run plan and explicit preconditions."""

    lock = _load_lock(lock_path)
    allowed, seed_hash, partition = _load_seed_set(seed_file)
    plan = build_report(
        [],
        {"parse_errors": [], "event_count": 0, "session_provenance": [], "plan": True},
        cohort=cohort,
        seed_mode=seed_mode,
        pilot_runs=pilot_runs,
        formal_runs=formal_runs,
        lock=lock,
        allowed_seeds=allowed,
        seed_file=seed_file,
        seed_file_sha256=seed_hash,
    )
    plan["plan"] = {
        "execution_command": (
            "python -m bridge.autoplay --allow-actions "
            f"--cohort {cohort} --seed-mode {seed_mode} "
            "--out-of-combat-only --execution-owner combat_solver_full_auto "
            f"--max-runs {formal_runs} --trace <trace.jsonl> "
            "--model-id <trained-model-id> --checkpoint <checkpoint> "
            "--data-manifest <data-manifest>"
        ),
        "pilot_command": (
            "python -m bridge.autoplay --allow-actions "
            f"--cohort {cohort} --seed-mode {seed_mode} "
            "--out-of-combat-only --execution-owner combat_solver_full_auto "
            f"--max-runs {pilot_runs} --trace <pilot-trace.jsonl> "
            "--model-id <trained-model-id> --checkpoint <checkpoint> "
            "--data-manifest <data-manifest>"
        ),
        "formal_command_is_not_started": True,
        "execution_owner": {
            "required": True,
            "recommended": "combat_solver_full_auto",
            "alternatives": ["http_route_executor", "manual_player"],
            "keeper_is_watchdog_only": True,
            "forbidden": [
                "combat_solver_full_auto + HTTP combat actions",
                "http_route_executor + Mod full-auto deploys",
                "two execution owners in one cohort",
            ],
        },
        "seed_partition_name": partition,
        "preconditions": [
            "start from a fresh, explicitly named batch; never reuse phase1-50",
            "verify config/live_version.lock.json and live mod inventory",
            "machine-verify every run as standard Ironclad A10",
            "keep assisted and no-SL traces in separate cohorts",
            "record raw seed and run_id for every run",
            "do not count a nonterminal or ambiguous game-over as a win",
            "do not present battle/simulator wins as full-run wins",
        ],
    }
    return plan


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, nargs="*", default=[])
    parser.add_argument(
        "--comparison-dir",
        "--comparison-evidence",
        dest="comparison_dirs",
        type=Path,
        action="append",
        default=[],
        help="merge durable battles.jsonl/records_checkpoint.json deploy evidence",
    )
    parser.add_argument("--plan", action="store_true", help="write an empty pilot/formal plan")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cohort", choices=COHORTS, required=True)
    parser.add_argument("--seed-mode", choices=SEED_MODES, required=True)
    parser.add_argument("--lock-file", type=Path, default=PROJECT_ROOT / "config" / "live_version.lock.json")
    parser.add_argument("--seed-file", type=Path, default=None)
    parser.add_argument("--pilot-runs", type=int, default=DEFAULT_PILOT_RUNS)
    parser.add_argument("--formal-runs", type=int, default=DEFAULT_FORMAL_RUNS)
    parser.add_argument("--provenance", type=Path, default=None)
    parser.add_argument("--checkpoint", type=Path, default=None)
    parser.add_argument("--data-manifest", type=Path, default=None)
    parser.add_argument("--emulator", type=Path, default=None)
    parser.add_argument("--model-id", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.pilot_runs <= 0 or args.formal_runs <= 0:
        raise SystemExit("pilot/formal run counts must be positive")
    if args.plan and (args.trace or args.comparison_dirs):
        raise SystemExit("--plan cannot be combined with trace or comparison evidence")
    try:
        lock = _load_lock(args.lock_file)
        allowed, seed_hash, _partition = _load_seed_set(args.seed_file)
        supplied = _load_json(args.provenance) if args.provenance else None
        if supplied is not None and not isinstance(supplied, dict):
            raise ValueError("--provenance must contain a JSON object")
        if args.plan:
            report = build_plan(
                cohort=args.cohort,
                seed_mode=args.seed_mode,
                pilot_runs=args.pilot_runs,
                formal_runs=args.formal_runs,
                lock_path=args.lock_file,
                seed_file=args.seed_file,
            )
        else:
            if not args.trace:
                raise ValueError("--trace is required unless --plan is used")
            traces = [load_trace(path) for path in args.trace]
            if args.comparison_dirs:
                # A comparison batch is joined to the supplied trace before
                # analysis so its provenance is carried into the same
                # session/run accounting.  Multiple batches are allowed only
                # when their durable keys/payloads are idempotent; the merge
                # helper rejects conflicts.
                for comparison_dir in args.comparison_dirs:
                    evidence = adapt_comparison_evidence(comparison_dir)
                    candidates = [
                        trace
                        for trace in traces
                        if _comparison_matches_trace(trace, evidence)
                    ]
                    if not candidates:
                        raise EvidenceIntegrityError(
                            f"comparison evidence does not match any trace: {comparison_dir}"
                        )
                    if len(candidates) != 1:
                        raise EvidenceIntegrityError(
                            f"comparison evidence ambiguously matches traces: {comparison_dir}"
                        )
                    merge_comparison_evidence(candidates[0], evidence)
            runs, quality = analyze_traces(
                traces,
                cohort=args.cohort,
                seed_mode=args.seed_mode,
                allowed_seeds=allowed,
            )
            # Feed trace provenance through the report helper by embedding it
            # as session metadata; no output row contains raw full state.
            report = build_report(
                runs,
                quality,
                cohort=args.cohort,
                seed_mode=args.seed_mode,
                pilot_runs=args.pilot_runs,
                formal_runs=args.formal_runs,
                lock=lock,
                provenance=supplied,
                checkpoint=args.checkpoint,
                data_manifest=args.data_manifest,
                emulator=args.emulator,
                model_id=args.model_id,
                allowed_seeds=allowed,
                seed_file=args.seed_file,
                seed_file_sha256=seed_hash,
            )
            report["artifacts"] = {"traces": [str(path) for path in args.trace]}
        _atomic_write(args.out, report)
        print(json.dumps({"report": str(args.out), "verdict": report.get("verdict")}, ensure_ascii=False))
        return 0 if report.get("verdict", {}).get("accepted") else 1
    except (OSError, ValueError) as exc:
        print(f"full-run audit blocked: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
