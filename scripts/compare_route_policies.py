"""Compare the live map heuristic with the offline route lookahead policy.

This command consumes the *raw* JSONL event stream written by
``bridge.trace_controller`` or ``bridge.autoplay``.  It reads state events
one at a time, keeps only map decision states, drops polling duplicates, and
writes a new JSONL report.  It never sends an action, opens a game connection,
loads a training checkpoint, or uses a later event as policy input.

Example::

    python scripts/compare_route_policies.py \
        runs/live_traces/clean-a10-path-left-heuristic.jsonl \
        --out runs/route_comparison/pilot/comparison.jsonl

The report contains one row per unique map state.  Invalid map states are
represented by compact ``excluded`` rows so their source and reason remain
auditable.  Non-map state events are counted but intentionally omitted from
the row file; this keeps a large combat trace compact.

The output is a policy disagreement report, not a replay and not a win-rate
evaluation.  The route planner sees only the current visible state supplied
by that state event.  In particular, no ``result``/future state is passed to
either policy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from advisor_core.contracts import Candidate, Recommendation  # noqa: E402
from advisor_core.live_candidate_codec import LIVE_BUILD  # noqa: E402
from advisor_core.policy_live import LiveHeuristicPolicy  # noqa: E402
from bridge.convert_traces import visible_view  # noqa: E402


REPORT_SCHEMA_VERSION = 1
ROUTE_POLICY_ID = "route-lookahead-v1"
BASELINE_POLICY_ID = "heuristic-out-of-combat-v1"


class RouteComparisonError(RuntimeError):
    """The trace comparison input or output contract is invalid."""


@dataclass(frozen=True)
class RawEventRef:
    """Location of one raw event, without retaining the event payload."""

    path: Path
    line: int
    sequence: int | None = None
    session_id: str | None = None


@dataclass
class ComparisonReport:
    """Streaming counters and compact issues for one comparison run."""

    inputs: list[str] = field(default_factory=list)
    counters: Counter[str] = field(default_factory=Counter)
    exclusions: Counter[str] = field(default_factory=Counter)
    errors: Counter[str] = field(default_factory=Counter)
    issues: list[str] = field(default_factory=list)
    input_sha256: list[dict[str, Any]] = field(default_factory=list)
    route_parameters: dict[str, Any] = field(default_factory=dict)
    git_provenance: dict[str, Any] = field(default_factory=dict)

    def bump(self, name: str, amount: int = 1) -> None:
        self.counters[name] += amount

    def exclude(self, reason: str, *, issue: str | None = None) -> None:
        self.exclusions[reason] += 1
        self.counters["excluded"] += 1
        if issue is not None and len(self.issues) < 100:
            self.issues.append(issue)

    def error(self, reason: str, *, issue: str | None = None) -> None:
        self.errors[reason] += 1
        self.counters["errors"] += 1
        if issue is not None and len(self.issues) < 100:
            self.issues.append(issue)

    def to_dict(self, *, output: Path, baseline_id: str, route_id: str) -> dict[str, Any]:
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "kind": "route_policy_comparison",
            "inputs": list(self.inputs),
            "output": _safe_path(output),
            "policies": {
                "baseline": baseline_id,
                "route": route_id,
            },
            "counters": dict(sorted(self.counters.items())),
            "exclusions": dict(sorted(self.exclusions.items())),
            "errors": dict(sorted(self.errors.items())),
            "issues": list(self.issues),
            "input_sha256": list(self.input_sha256),
            "route_parameters": dict(self.route_parameters),
            "git_provenance": dict(self.git_provenance),
            "claims": {
                "uses_future_outcomes": False,
                "is_win_rate_evaluation": False,
                "loads_training_checkpoint": False,
                "game_io": False,
            },
        }


def _safe_path(path: Path) -> str:
    """Return a stable path without exporting an absolute user path."""

    resolved = path.resolve()
    try:
        return resolved.relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        # ``Path.name`` is sufficient to identify a source in the report and
        # avoids leaking the caller's home/profile directory.
        return resolved.name


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _git_provenance() -> dict[str, Any]:
    """Return commit plus a privacy-safe dirty flag for reproducibility."""

    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {
            "git_commit": None,
            "git_dirty": None,
            "description": "git provenance unavailable in this checkout",
        }
    return {
        "git_commit": commit or None,
        "git_dirty": bool(status.strip()),
        "description": "git_commit is HEAD; git_dirty includes tracked and untracked working-tree changes",
    }


def _policy_parameters(policy: Any) -> dict[str, Any]:
    """Record explicit route knobs when the policy exposes them."""

    parameters: dict[str, Any] = {}
    for name in ("max_nodes", "max_depth", "future_discount"):
        value = getattr(policy, name, None)
        if value is not None:
            parameters[name] = _jsonable(value)
    return parameters


def stable_state_hash(state: Mapping[str, Any]) -> str:
    """Hash a deterministic visible view of one state event.

    ``visible_view`` removes the transient message and scrubs ordered piles.
    The map planner only consumes map/player fields, but using the shared
    visible filter makes duplicate detection conservative and keeps the hash
    independent of hidden draw order.
    """

    if not isinstance(state, Mapping):
        raise TypeError("state must be a mapping")
    visible = visible_view(dict(state))
    canonical = json.dumps(
        visible,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + _hash_text(canonical)


def _event_state(raw: Any) -> dict[str, Any] | None:
    """Extract a raw state object without inferring a screen from other data."""

    if not isinstance(raw, Mapping):
        return None
    # Recorder state events normally store the state directly.  Accepting a
    # fixture-like one-level envelope is useful for offline tests, but does
    # not infer a state from ``visible_state`` contract records.
    if isinstance(raw.get("state"), Mapping) and "state_type" not in raw:
        nested = raw["state"]
        if isinstance(nested, Mapping):
            state = dict(nested)
            # Keep the outer compatibility assertion visible to the codec.
            # In particular, a bad outer build must not be hidden by a
            # nested state that happens to carry the current build.  When
            # both are present, retaining a bad value from either layer is
            # sufficient to make validation fail closed.
            if "build" in raw:
                outer_build = raw["build"]
                if outer_build != LIVE_BUILD or "build" not in state:
                    state["build"] = outer_build
            return state
    if "state_type" not in raw:
        return None
    return dict(raw)


def _first_nonempty(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            text = str(value).strip()
            if text:
                return text
    return None


def _session_run_token(session_run_id: str | None, state: Mapping[str, Any]) -> str | None:
    run = state.get("run")
    run_map = run if isinstance(run, Mapping) else {}
    return _first_nonempty(
        run_map.get("run_id"),
        state.get("run_id"),
        session_run_id,
    )


def _safe_run_key(
    run_token: str | None,
    session_id: str | None,
    source_path: Path | None = None,
) -> str:
    """Return a pseudonymous run key; raw run/profile ids never leave memory."""

    if run_token:
        # A real run id is the strongest available boundary and may continue
        # across rotated trace files.
        basis = "run:" + run_token
    elif session_id:
        # Session ids are recorder-local.  Include the source boundary so two
        # files with the same placeholder session id cannot cross-deduplicate.
        boundary = str(source_path.resolve()) if source_path is not None else "unknown-file"
        basis = "session:" + session_id + ":" + boundary
    else:
        # With no identity at all, each source file is its own run boundary.
        boundary = str(source_path.resolve()) if source_path is not None else "unknown-file"
        basis = "file:" + boundary
    return "run-" + _hash_text(basis)[:16]


def _safe_decision_key(decision_id: Any) -> str | None:
    value = _first_nonempty(decision_id)
    if value is None:
        return None
    return "decision-" + _hash_text(value)[:16]


def _source(ref: RawEventRef) -> dict[str, Any]:
    source: dict[str, Any] = {
        "file": _safe_path(ref.path),
        "line": ref.line,
    }
    if ref.sequence is not None:
        source["sequence"] = ref.sequence
    return source


def iter_input_files(paths: Iterable[Path]) -> Iterator[Path]:
    """Yield JSONL sources in deterministic order, recursively for directories."""

    seen: set[Path] = set()
    for value in paths:
        path = Path(value)
        if path.is_dir():
            candidates = sorted(path.rglob("*.jsonl"))
        else:
            candidates = [path]
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            yield candidate


def _assert_new_outputs(
    outputs: Iterable[Path], input_resolved: set[Path]
) -> None:
    """Reject every output collision before creating any output directory/file."""

    seen: set[Path] = set()
    for value in outputs:
        path = Path(value)
        resolved = path.resolve()
        if resolved in input_resolved:
            raise RouteComparisonError(
                f"output must be a new path and cannot replace an input trace: {_safe_path(path)}"
            )
        if resolved in seen:
            raise RouteComparisonError(
                f"output paths must be distinct: {_safe_path(path)}"
            )
        seen.add(resolved)
        if path.exists():
            raise RouteComparisonError(
                f"refusing to overwrite an existing output: {_safe_path(path)}"
            )


def _write_text_exclusive(path: Path, text: str) -> None:
    """Create a UTF-8 text file without replacing a pre-existing file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _copy_file_exclusive(source: Path, target: Path) -> None:
    """Copy a small summary only when the destination is still absent."""

    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as source_handle, target.open("xb") as target_handle:
        shutil.copyfileobj(source_handle, target_handle)


def _write_json(handle: TextIO, payload: Mapping[str, Any]) -> None:
    handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _map_summary(state: Mapping[str, Any]) -> dict[str, Any]:
    map_state = state.get("map")
    if not isinstance(map_state, Mapping):
        return {}
    current = map_state.get("current_position")
    current_out = _node_summary(current) if isinstance(current, Mapping) else None
    next_options = map_state.get("next_options")
    options_out: list[dict[str, Any]] = []
    if isinstance(next_options, list):
        for item in next_options:
            if isinstance(item, Mapping):
                options_out.append(_option_summary(item))
    run = state.get("run")
    run_map = run if isinstance(run, Mapping) else {}
    player = state.get("player")
    player_map = player if isinstance(player, Mapping) else {}
    out: dict[str, Any] = {
        "current_position": current_out,
        "next_options": options_out,
        "node_count": (
            len(map_state.get("nodes"))
            if isinstance(map_state.get("nodes"), list)
            else None
        ),
        "visited_count": (
            len(map_state.get("visited"))
            if isinstance(map_state.get("visited"), list)
            else None
        ),
    }
    if isinstance(run_map.get("act"), int):
        out["act"] = run_map["act"]
    if isinstance(run_map.get("floor"), int):
        out["floor"] = run_map["floor"]
    if isinstance(player_map.get("hp"), (int, float)):
        out["hp"] = player_map["hp"]
    if isinstance(player_map.get("max_hp"), (int, float)):
        out["max_hp"] = player_map["max_hp"]
    return out


def _node_summary(node: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("col", "row", "type"):
        value = node.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            out[key] = value
    return out


def _option_summary(option: Mapping[str, Any]) -> dict[str, Any]:
    out = _node_summary(option)
    value = option.get("index")
    if isinstance(value, int) and not isinstance(value, bool):
        out["index"] = value
    leads = option.get("leads_to")
    if isinstance(leads, list):
        out["leads_to"] = [
            _node_summary(item) for item in leads if isinstance(item, Mapping)
        ]
    return out


def _jsonable(value: Any) -> Any:
    """Convert policy contract values to JSON while keeping output compact."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return str(value)


def _candidate_dict(
    state: Mapping[str, Any], candidate: Candidate, *, policy: Any
) -> dict[str, Any]:
    action = _jsonable(candidate.action)
    wire = policy.wire_action_for(state, candidate.action)
    return {
        "action": action,
        "wire_action": _jsonable(wire),
        "label": str(candidate.label),
        "score": float(candidate.score),
        "confidence": float(candidate.confidence),
        "facts": [str(fact) for fact in candidate.facts],
        "metrics": _jsonable(candidate.metrics),
    }


def _recommendation_dict(
    state: Mapping[str, Any],
    recommendation: Recommendation,
    *,
    policy: Any,
    candidate_set: Any,
) -> dict[str, Any]:
    if not isinstance(recommendation, Recommendation):
        raise RouteComparisonError("policy returned a non-Recommendation value")
    recommendation.validate()
    surfaced = (recommendation.primary, *recommendation.alternatives)
    candidates = [_candidate_dict(state, candidate, policy=policy) for candidate in surfaced]
    # Candidate wire identity validation is part of the live policy contract.
    # RoutePlannerPolicy exposes the same helper; this check catches a planner
    # that returns a structurally valid but currently illegal index.
    for candidate in candidates:
        candidate_set.identity_for(candidate["wire_action"])
    primary = candidates[0]
    return {
        "phase": str(recommendation.phase),
        "model_id": str(recommendation.model_id),
        "game_build": str(recommendation.game_build),
        "search_nodes": int(recommendation.search_nodes),
        "primary": primary,
        "alternatives": candidates[1:],
        "score_decomposition": {
            "score": primary["score"],
            "metrics": primary["metrics"],
            "facts": primary["facts"],
            "search_nodes": int(recommendation.search_nodes),
        },
        "warnings": [str(warning) for warning in recommendation.warnings],
    }


def _route_action_key(rec: Mapping[str, Any]) -> str:
    return json.dumps(rec["primary"]["wire_action"], ensure_ascii=False, sort_keys=True)


def _excluded_row(ref: RawEventRef, reason: str, detail: str | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"reason": reason}
    if detail:
        error["detail"] = detail[:500]
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "kind": "excluded",
        "source": _source(ref),
        "status": "excluded",
        "error": error,
    }


def _compare_state(
    state: dict[str, Any],
    *,
    ref: RawEventRef,
    run_key: str,
    decision_key: str | None,
    state_hash: str,
    baseline: LiveHeuristicPolicy,
    route_policy: Any,
    report: ComparisonReport,
) -> dict[str, Any] | None:
    """Compare only this state; no neighboring event is available here."""

    try:
        # Candidate extraction happens before either recommendation and is
        # shared by both policies for a strict legal-action comparison.
        candidate_set = baseline.candidates(state)
    except Exception as exc:  # contract errors are represented in the report
        reason = "invalid_map_state"
        report.exclude(reason, issue=f"{_safe_path(ref.path)}:{ref.line}: {exc}")
        return _excluded_row(ref, reason, type(exc).__name__ + ": " + str(exc))

    legal_actions = []
    for candidate in candidate_set:
        legal_actions.append(
            {
                "identity": str(candidate.identity),
                "text": str(candidate.text),
                "wire_action": _jsonable(candidate.wire_action),
                "features": _jsonable(candidate.features),
            }
        )

    baseline_result: dict[str, Any] | None = None
    route_result: dict[str, Any] | None = None
    baseline_error: dict[str, Any] | None = None
    route_error: dict[str, Any] | None = None
    try:
        baseline_result = _recommendation_dict(
            state,
            baseline.recommend(state),
            policy=baseline,
            candidate_set=candidate_set,
        )
        report.bump("baseline_success")
    except Exception as exc:
        report.error("baseline_recommendation_error", issue=f"{_safe_path(ref.path)}:{ref.line}: baseline: {exc}")
        baseline_error = {"reason": "baseline_recommendation_error", "detail": str(exc)[:500]}

    try:
        route_result = _recommendation_dict(
            state,
            route_policy.recommend(state),
            policy=route_policy,
            candidate_set=candidate_set,
        )
        report.bump("route_success")
    except Exception as exc:
        report.error("route_recommendation_error", issue=f"{_safe_path(ref.path)}:{ref.line}: route: {exc}")
        route_error = {"reason": "route_recommendation_error", "detail": str(exc)[:500]}

    row: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "kind": "comparison",
        "source": _source(ref),
        "run_key": run_key,
        "decision_key": decision_key,
        "state_hash": state_hash,
        "state": _map_summary(state),
        "legal_actions": legal_actions,
        "baseline": baseline_result,
        "route": route_result,
        "errors": {
            "baseline": baseline_error,
            "route": route_error,
        },
        "divergence": {
            "comparable": baseline_result is not None and route_result is not None,
            "disagrees": (
                baseline_result is not None
                and route_result is not None
                and _route_action_key(baseline_result) != _route_action_key(route_result)
            ),
        },
    }
    if row["divergence"]["disagrees"]:
        report.bump("divergences")
    report.bump("comparisons")
    return row


def compare_trace_files(
    inputs: Iterable[Path],
    *,
    output: Path,
    route_policy: Any | None = None,
    baseline: LiveHeuristicPolicy | None = None,
    expected_character: str | None = None,
    expected_ascension: int | None = None,
) -> ComparisonReport:
    """Stream raw trace inputs into ``output`` and return counters.

    ``route_policy`` must expose the fixed
    ``RoutePlannerPolicy.recommend(state) -> Recommendation`` contract.  If
    omitted, the route policy is constructed lazily with its production
    defaults.  ``expected_character``/``expected_ascension`` are optional
    gates for a caller that wants to restrict an observational trace; they
    default to no gate so fixture states remain useful for protocol tests.
    """

    output = Path(output)
    paths = list(iter_input_files(inputs))
    if not paths:
        raise RouteComparisonError("no JSONL input files found")
    input_resolved = {path.resolve() for path in paths}
    summary_path = output.with_suffix(output.suffix + ".summary.json")
    _assert_new_outputs((output, summary_path), input_resolved)

    if route_policy is None:
        from advisor_core.route_planner import RoutePlannerPolicy

        route_policy = RoutePlannerPolicy()
    if baseline is None:
        baseline = LiveHeuristicPolicy()
    if not hasattr(route_policy, "recommend"):
        raise RouteComparisonError("route policy must expose recommend(state)")
    if not hasattr(route_policy, "wire_action_for"):
        raise RouteComparisonError("route policy must expose wire_action_for(state, action)")

    report = ComparisonReport(inputs=[_safe_path(path) for path in paths])
    report.route_parameters = _policy_parameters(route_policy)
    report.git_provenance = _git_provenance()
    decision_seen_hashes: dict[tuple[str, str], str] = {}
    seen_states: set[tuple[str, str]] = set()
    session_run_ids: dict[str, str] = {}
    session_ids: dict[Path, str] = {}
    path_run_ids: dict[Path, str] = {}
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8", newline="\n") as handle:
            for path in paths:
                try:
                    source = path.open("rb")
                except OSError as exc:
                    report.error("input_open_error", issue=f"{_safe_path(path)}: {exc}")
                    continue
                with source:
                    input_hasher = hashlib.sha256()
                    input_bytes = 0
                    for line_number, line_bytes in enumerate(source, start=1):
                        input_hasher.update(line_bytes)
                        input_bytes += len(line_bytes)
                        try:
                            line = line_bytes.decode(
                                "utf-8-sig" if line_number == 1 else "utf-8"
                            )
                        except UnicodeDecodeError as exc:
                            report.exclude(
                                "invalid_utf8",
                                issue=f"{_safe_path(path)}:{line_number}: invalid UTF-8",
                            )
                            _write_json(
                                handle,
                                _excluded_row(
                                    RawEventRef(path, line_number),
                                    "invalid_utf8",
                                    str(exc),
                                ),
                            )
                            continue
                        if not line.strip():
                            report.bump("blank_lines")
                            continue
                        report.bump("lines_seen")
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError as exc:
                            report.exclude(
                                "malformed_json",
                                issue=f"{_safe_path(path)}:{line_number}: {exc.msg}",
                            )
                            _write_json(
                                handle,
                                _excluded_row(
                                    RawEventRef(path, line_number),
                                    "malformed_json",
                                    exc.msg,
                                ),
                            )
                            continue
                        if not isinstance(event, Mapping):
                            report.exclude("event_not_object")
                            _write_json(
                                handle,
                                _excluded_row(
                                    RawEventRef(path, line_number),
                                    "event_not_object",
                                ),
                            )
                            continue
                        report.bump("events_seen")
                        event_type = event.get("event_type")
                        raw = event.get("raw")
                        raw_session_id = (
                            raw.get("session_id") if isinstance(raw, Mapping) else None
                        )
                        session_id = _first_nonempty(
                            event.get("session_id"), raw_session_id
                        )
                        if session_id:
                            session_ids[path] = session_id
                        if event_type == "session":
                            report.bump("session_events")
                            if isinstance(raw, Mapping):
                                run_id = _first_nonempty(
                                    raw.get("run_id"), event.get("run_id")
                                )
                                if run_id:
                                    path_run_ids[path] = run_id
                                    if session_id:
                                        session_run_ids[session_id] = run_id
                            continue
                        if event_type == "run_identity":
                            report.bump("run_identity_events")
                            if isinstance(raw, Mapping):
                                run_id = _first_nonempty(
                                    raw.get("run_id"), event.get("run_id")
                                )
                                if run_id:
                                    path_run_ids[path] = run_id
                                    if session_id:
                                        session_run_ids[session_id] = run_id
                            continue
                        if event_type != "state":
                            safe_event_type = (
                                event_type
                                if isinstance(event_type, str)
                                and event_type in {"health", "action", "result", "compendium"}
                                else "other"
                            )
                            report.bump(f"ignored_{safe_event_type}")
                            continue
                        report.bump("state_events")
                        state = _event_state(raw)
                        ref = RawEventRef(
                            path,
                            line_number,
                            event.get("sequence") if isinstance(event.get("sequence"), int) else None,
                            session_id,
                        )
                        if state is None:
                            report.exclude("state_missing_object", issue=f"{_safe_path(path)}:{line_number}: state object missing")
                            _write_json(handle, _excluded_row(ref, "state_missing_object"))
                            continue
                        state_type = str(state.get("state_type") or "").strip().lower()
                        if state_type != "map":
                            report.bump(f"non_map_{state_type or 'unknown'}")
                            continue
                        report.bump("map_events")
                        state_hash = stable_state_hash(state)
                        effective_session_id = session_id or session_ids.get(path)
                        session_run_id = (
                            session_run_ids.get(effective_session_id or "")
                            or path_run_ids.get(path)
                        )
                        run_token = _session_run_token(session_run_id, state)
                        run_key = _safe_run_key(
                            run_token, effective_session_id, source_path=path
                        )
                        decision_value = event.get("decision_id")
                        decision_key = _safe_decision_key(decision_value)
                        # Prefer run_id+decision_id, while also recognizing a
                        # polling duplicate whose recorder id changed but whose
                        # visible map state is identical within that run.
                        decision_seen_key = (
                            run_token or run_key,
                            str(decision_value),
                        ) if decision_key is not None else None
                        state_seen_key = (run_token or run_key, state_hash)
                        if decision_seen_key is not None:
                            previous_hash = decision_seen_hashes.get(decision_seen_key)
                            if previous_hash is not None:
                                if previous_hash != state_hash:
                                    conflict_reason = "decision_state_conflict"
                                    conflict_issue = (
                                        f"{_safe_path(path)}:{line_number}: same decision identity has different state hash"
                                    )
                                    report.error(conflict_reason, issue=conflict_issue)
                                    report.exclude(conflict_reason)
                                    report.bump("decision_conflicts")
                                    _write_json(
                                        handle,
                                        _excluded_row(
                                            ref,
                                            conflict_reason,
                                            "same run identity and decision id had a different visible state hash",
                                        ),
                                    )
                                else:
                                    report.bump("duplicates_dropped")
                                continue
                            decision_seen_hashes[decision_seen_key] = state_hash
                        if state_seen_key in seen_states:
                            report.bump("duplicates_dropped")
                            continue
                        seen_states.add(state_seen_key)
                        report.bump("unique_map_states")

                        if expected_character is not None:
                            player = state.get("player")
                            player_map = player if isinstance(player, Mapping) else {}
                            character = _first_nonempty(
                                player_map.get("character_id"),
                                player_map.get("character"),
                            )
                            if character is None or character.upper() != expected_character.upper():
                                report.exclude("character_mismatch")
                                _write_json(handle, _excluded_row(ref, "character_mismatch"))
                                continue
                        if expected_ascension is not None:
                            run = state.get("run")
                            run_map = run if isinstance(run, Mapping) else {}
                            if run_map.get("ascension") != expected_ascension:
                                report.exclude("ascension_mismatch")
                                _write_json(handle, _excluded_row(ref, "ascension_mismatch"))
                                continue

                        row = _compare_state(
                            state,
                            ref=ref,
                            run_key=run_key,
                            decision_key=decision_key,
                            state_hash=state_hash,
                            baseline=baseline,
                            route_policy=route_policy,
                            report=report,
                        )
                        if row is not None:
                            _write_json(handle, row)
                    report.input_sha256.append(
                        {
                            "file": _safe_path(path),
                            "sha256": "sha256:" + input_hasher.hexdigest(),
                            "bytes": input_bytes,
                        }
                    )
    except OSError as exc:
        raise RouteComparisonError(f"cannot write comparison output {output}: {exc}") from exc

    # A successful comparison always records explicit provenance.  The caller
    # can write this dictionary next to the JSONL file without retaining raw
    # states or account identifiers.
    summary = report.to_dict(
        output=output,
        baseline_id=str(getattr(baseline, "model_id", BASELINE_POLICY_ID)),
        route_id=str(getattr(route_policy, "model_id", ROUTE_POLICY_ID)),
    )
    _write_text_exclusive(
        summary_path,
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
    )
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path, help="raw recorder JSONL files or directories")
    parser.add_argument("--out", required=True, type=Path, help="new comparison JSONL output path")
    parser.add_argument("--max-nodes", type=int, default=256, help="route planner node budget")
    parser.add_argument("--max-depth", type=int, default=4, help="route planner visible depth")
    parser.add_argument("--future-discount", type=float, default=0.85, help="route planner future score discount")
    parser.add_argument("--expected-character", default=None, help="optional character gate, e.g. IRONCLAD")
    parser.add_argument("--expected-ascension", type=int, default=None, help="optional ascension gate")
    parser.add_argument("--summary", type=Path, default=None, help="optional copy of the summary JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    input_paths = list(iter_input_files(args.inputs))
    if not input_paths:
        raise RouteComparisonError("no JSONL input files found")
    default_summary = args.out.with_suffix(args.out.suffix + ".summary.json")
    input_resolved = {path.resolve() for path in input_paths}
    requested_outputs: list[Path] = [args.out, default_summary]
    if args.summary is not None and args.summary.resolve() != default_summary.resolve():
        requested_outputs.append(args.summary)
    _assert_new_outputs(requested_outputs, input_resolved)

    from advisor_core.route_planner import RoutePlannerPolicy

    policy = RoutePlannerPolicy(
        max_nodes=args.max_nodes,
        max_depth=args.max_depth,
        future_discount=args.future_discount,
    )
    report = compare_trace_files(
        args.inputs,
        output=args.out,
        route_policy=policy,
        expected_character=args.expected_character,
        expected_ascension=args.expected_ascension,
    )
    if args.summary is not None and args.summary.resolve() != default_summary.resolve():
        _copy_file_exclusive(default_summary, args.summary)
    print(
        json.dumps(
            report.to_dict(
                output=args.out,
                baseline_id=BASELINE_POLICY_ID,
                route_id=str(getattr(policy, "model_id", ROUTE_POLICY_ID)),
            ),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
