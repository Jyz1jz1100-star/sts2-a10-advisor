"""Fail-closed adapter for durable comparison execution evidence.

The comparison runner's journal is append-only and may be ahead of its
aggregate ``records_checkpoint.json`` after a crash.  This adapter joins the
two durable views and emits the small event shape consumed by
``scripts.assess_full_run``.  Only an exact ``executed.source ==
\"deploy_log\"`` is authoritative; inferred state-delta actions are retained
as a diagnostic count but are never promoted to deployment evidence.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from combat_solver.logranges import (
    LogRange,
    LogRangeError,
    ranges_overlap,
    validate_deploy_grammar,
    verify_log_range,
)


class EvidenceIntegrityError(ValueError):
    """Durable evidence is missing, ambiguous, or internally contradictory."""


class VerifiedComparisonEvidence(dict[str, Any]):
    """In-process marker for adapter output whose raw ranges were verified."""


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise EvidenceIntegrityError(f"evidence is not canonical JSON: {exc}") from exc


def _read_source(path: Path, *, required: bool) -> tuple[dict[str, Any], bytes | None]:
    """Read one immutable byte snapshot and derive its provenance from it.

    Parsing must consume the returned bytes rather than reopening ``path``.
    This closes the journal/checkpoint hash-vs-read TOCTOU window and also
    ensures an optional checkpoint that appears after this read is not
    silently adopted without a matching hash.
    """

    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        if required:
            raise EvidenceIntegrityError(f"required evidence source is missing: {path}")
        return (
            {
                "path": str(path),
                "sha256": None,
                "exists": False,
                "hash_verified": False,
            },
            None,
        )
    except OSError as exc:
        raise EvidenceIntegrityError(f"evidence source is unreadable: {path}: {exc}") from exc
    digest = hashlib.sha256(payload).hexdigest().upper()
    return (
        {
            "path": str(path),
            "sha256": digest,
            "exists": True,
            "hash_verified": True,
        },
        payload,
    )


def _read_jsonl(payload: bytes, path: Path) -> list[dict[str, Any]]:
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise EvidenceIntegrityError(f"cannot read evidence journal {path}: {exc}") from exc
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except ValueError as exc:
            raise EvidenceIntegrityError(
                f"invalid JSON in evidence journal {path}:{line_number}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise EvidenceIntegrityError(f"evidence journal {path}:{line_number} is not an object")
        rows.append(payload)
    return rows


def _dedupe_by_id(items: Iterable[Mapping[str, Any]], *, label: str) -> tuple[dict[str, dict[str, Any]], int]:
    result: dict[str, dict[str, Any]] = {}
    fingerprints: dict[str, str] = {}
    deduped = 0
    for item in items:
        raw_id = item.get("battle_id")
        if not isinstance(raw_id, str) or not raw_id.strip():
            raise EvidenceIntegrityError(f"{label} entry lacks a non-empty battle_id")
        battle_id = raw_id.strip()
        fingerprint = _canonical(dict(item))
        previous = fingerprints.get(battle_id)
        if previous is None:
            fingerprints[battle_id] = fingerprint
            result[battle_id] = dict(item)
        elif previous == fingerprint:
            # A retry/copy of the same durable append is harmless and is
            # explicitly counted so an assessor can see that de-duplication
            # occurred.
            deduped += 1
        else:
            raise EvidenceIntegrityError(
                f"conflicting duplicate {label} battle_id {battle_id!r}"
            )
    return result, deduped


def _load_checkpoint(payload: bytes, path: Path) -> tuple[dict[str, dict[str, Any]], int]:
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise EvidenceIntegrityError(f"cannot read records checkpoint {path}: {exc}") from exc
    if not isinstance(decoded, dict) or decoded.get("schema_version") != 1:
        raise EvidenceIntegrityError(f"unsupported records checkpoint schema: {path}")
    records = decoded.get("records")
    if not isinstance(records, list):
        raise EvidenceIntegrityError(f"records checkpoint lacks records[]: {path}")
    return _dedupe_by_id(records, label="checkpoint")


def _scalar_match(left: Mapping[str, Any], right: Mapping[str, Any], key: str) -> None:
    if key in left and key in right and left.get(key) != right.get(key):
        raise EvidenceIntegrityError(
            f"journal/checkpoint {key} conflict for battle_id {left.get('battle_id')!r}"
        )


def _record_for_row(row: Mapping[str, Any], checkpoint: Mapping[str, Any] | None) -> dict[str, Any]:
    battle_id = row.get("battle_id")
    embedded = row.get("record_checkpoint")
    if embedded is not None and not isinstance(embedded, dict):
        raise EvidenceIntegrityError(f"record_checkpoint for {battle_id!r} is not an object")
    if checkpoint is not None and embedded is not None and _canonical(embedded) != _canonical(checkpoint):
        raise EvidenceIntegrityError(f"embedded/checkpoint record conflict for battle_id {battle_id!r}")
    record = dict(checkpoint or embedded or row)
    if record.get("battle_id") != battle_id:
        raise EvidenceIntegrityError(f"record battle_id does not match journal row {battle_id!r}")
    for key in ("run_id", "seed", "act", "floor", "hp_start", "hp_end", "outcome"):
        _scalar_match(row, record, key)
    return record


def _nonempty_id(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


_ACTION_KIND_ALIASES = {
    "play": "play",
    "play_card": "play",
    "card": "play",
    "potion": "potion",
    "use_potion": "potion",
    "drink_potion": "potion",
}


def _first_present(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping.get(key) is not None:
            return mapping.get(key)
    return None


def _normalise_action_kind(value: Any) -> str | None:
    if value is None:
        return None
    token = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if not token:
        return None
    canonical = _ACTION_KIND_ALIASES.get(token)
    if canonical is not None:
        return canonical
    if "potion" in token:
        return "potion"
    if "card" in token or "play" in token:
        return "play"
    return token


def _normalise_target_index(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise EvidenceIntegrityError("target index cannot be boolean")
    if isinstance(value, int):
        return None if value < 0 else value
    if isinstance(value, float):
        if not value.is_integer():
            raise EvidenceIntegrityError(f"invalid target index {value!r}")
        parsed = int(value)
        return None if parsed < 0 else parsed
    text = str(value).strip().lower()
    if text in {"none", "null", "nil", "n/a", "-"}:
        return None
    try:
        parsed = int(text, 10)
    except ValueError as exc:
        raise EvidenceIntegrityError(f"invalid target index {value!r}") from exc
    return None if parsed < 0 else parsed


def _action_signature(
    *, kind: Any, card_id: Any, potion_id: Any, target_index: Any
) -> tuple[str | None, str | None, int | None]:
    normal_kind = _normalise_action_kind(kind)
    card = _nonempty_id(card_id)
    potion = _nonempty_id(potion_id)
    if normal_kind is None:
        if potion is not None and card is None:
            normal_kind = "potion"
        elif card is not None:
            normal_kind = "play"
    item_id = (potion or card) if normal_kind == "potion" else (card or potion)
    return normal_kind, item_id, _normalise_target_index(target_index)


def _expected_action_signature(
    action: Mapping[str, Any],
) -> tuple[str | None, str | None, int | None]:
    return _action_signature(
        kind=_first_present(action, "kind", "action", "action_type", "type"),
        card_id=_first_present(action, "card_id", "card", "card_uuid"),
        potion_id=_first_present(action, "potion_id", "potion", "item_id"),
        target_index=_first_present(action, "target_index", "target", "target_idx"),
    )


def _observed_action_signature(
    tokens: Mapping[str, str],
) -> tuple[str | None, str | None, int | None]:
    return _action_signature(
        kind=_first_present(tokens, "action", "kind", "action_type", "type"),
        card_id=_first_present(tokens, "card_id", "card", "card_uuid"),
        potion_id=_first_present(tokens, "potion_id", "potion", "item_id"),
        target_index=_first_present(tokens, "target_index", "target", "target_idx"),
    )


def _validate_deploy_action_alignment(
    marker_events: Iterable[tuple[str, Mapping[str, str]]],
    actions: Sequence[Mapping[str, Any]],
    *,
    battle_id: str,
    turn: int,
) -> None:
    observed = [
        _observed_action_signature(tokens)
        for marker, tokens in marker_events
        if marker == "DEPLOY_ACTION"
    ]
    expected = [_expected_action_signature(action) for action in actions]
    if observed != expected:
        raise EvidenceIntegrityError(
            f"deploy action mismatch for {battle_id!r} turn {turn}: "
            f"log={observed!r} executed={expected!r}"
        )


def _valid_seed(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, str))
        and not (isinstance(value, str) and not value.strip())
    )


def _validate_explicit_decision_bindings(
    turn: Mapping[str, Any],
    executed: Mapping[str, Any],
    record: Mapping[str, Any],
) -> None:
    """Reject contradictory producer-supplied decision identities.

    ``state_hash`` is deliberately not checked here.  A route snapshot can
    be reused across turn anchors and therefore carry a stale snapshot hash;
    the explicit ``decision_id`` fields are the producer bindings that must
    agree when more than one is present.
    """
    bindings: list[tuple[str, str]] = []
    for label, source in (
        ("turn.decision_id", turn),
        ("executed.decision_id", executed),
        ("record.decision_id", record),
    ):
        found = _nonempty_id(source.get("decision_id"))
        if found is not None:
            bindings.append((label, found))
    values = {value for _label, value in bindings}
    if len(values) > 1:
        detail = ", ".join(f"{label}={value!r}" for label, value in bindings)
        raise EvidenceIntegrityError(
            f"conflicting explicit decision_id bindings: {detail}"
        )


def _turn_decision(
    turn: Mapping[str, Any],
    executed: Mapping[str, Any],
    record: Mapping[str, Any],
    *,
    include_record_fallback: bool = True,
) -> str | None:
    _validate_explicit_decision_bindings(turn, executed, record)
    explicit = _nonempty_id(turn.get("decision_id")) or _nonempty_id(
        executed.get("decision_id")
    )
    if explicit is not None:
        return explicit
    if include_record_fallback:
        record_decision = _nonempty_id(record.get("decision_id"))
        if record_decision is not None:
            return record_decision
    snapshot = turn.get("snapshot")
    snapshot_hash = (
        snapshot.get("state_hash") if isinstance(snapshot, Mapping) else None
    )
    for value in (turn.get("state_hash"), snapshot_hash):
        found = _nonempty_id(value)
        if found:
            return found
    return None


@dataclass(frozen=True)
class DeployEvidence:
    run_id: str
    seed: Any
    battle_id: str
    decision_id: str
    turn: int
    actions: tuple[dict[str, Any], ...]
    ambiguous: bool
    log_range: dict[str, Any]

    def key(self) -> tuple[str, str, str, str, int]:
        return (
            self.run_id,
            str(self.seed),
            self.battle_id,
            self.decision_id,
            self.turn,
        )

    def to_event(self, sequence: int, source: Mapping[str, Any]) -> dict[str, Any]:
        executed = {
            "turn": self.turn,
            "actions": list(self.actions),
            "ambiguous": self.ambiguous,
            "notes": ["durable comparison record"],
            "source": "deploy_log",
            "source_evidence": dict(self.log_range),
        }
        return {
            "schema_version": 1,
            "sequence": sequence,
            "event_type": "deploy_log",
            "decision_id": self.decision_id,
            "state_type": "monster",
            "raw": {
                "run_id": self.run_id,
                "seed": self.seed,
                "battle_id": self.battle_id,
                "decision_id": self.decision_id,
                "turn": self.turn,
                "actions": list(self.actions),
                "executed": executed,
                "source": "deploy_log",
                "status": "applied",
                "log_range": dict(self.log_range),
                "provenance": {
                    **dict(source),
                    "log_range_verified": True,
                },
            },
        }


def adapt_comparison_evidence(
    comparison_dir: Path | str | None = None,
    *,
    battles_path: Path | str | None = None,
    checkpoint_path: Path | str | None = None,
    record_checkpoint_path: Path | str | None = None,
    manifest_path: Path | str | None = None,
) -> dict[str, Any]:
    """Read durable comparison output and return assessor-readable events.

    Exact duplicate journal/checkpoint entries are idempotently de-duplicated.
    Any missing binding (run, seed, decision, turn), source disagreement,
    inferred execution is never promoted, and missing bindings or duplicate
    conflicts raise ``EvidenceIntegrityError``.
    """
    if checkpoint_path is not None and record_checkpoint_path is not None:
        if Path(checkpoint_path) != Path(record_checkpoint_path):
            raise EvidenceIntegrityError(
                "checkpoint_path and record_checkpoint_path disagree"
            )
    checkpoint_arg = checkpoint_path if checkpoint_path is not None else record_checkpoint_path
    root = Path(comparison_dir) if comparison_dir is not None else None
    battles = Path(battles_path) if battles_path is not None else (root / "battles.jsonl" if root else None)
    if battles is None:
        raise EvidenceIntegrityError("battles_path or comparison_dir is required")
    checkpoint = Path(checkpoint_arg) if checkpoint_arg is not None else battles.with_name("records_checkpoint.json")
    manifest = Path(manifest_path) if manifest_path is not None else battles.with_name("manifest.json")
    battles_source, battles_bytes = _read_source(battles, required=True)
    checkpoint_source, checkpoint_bytes = _read_source(checkpoint, required=False)
    manifest_source, manifest_bytes = _read_source(manifest, required=False)
    # All parsing below is against these exact snapshots.  Do not replace
    # this with Path.is_file()/read_text() checks: an optional file can appear
    # between those operations and otherwise escape provenance accounting.
    source_files = {
        "battles_jsonl": battles_source,
        "records_checkpoint": checkpoint_source,
        "comparison_manifest": manifest_source,
    }
    assert battles_bytes is not None
    journal_by_id, journal_duplicates = _dedupe_by_id(
        _read_jsonl(battles_bytes, battles), label="journal"
    )
    checkpoint_by_id: dict[str, dict[str, Any]] = {}
    checkpoint_duplicates = 0
    if checkpoint_bytes is not None:
        checkpoint_by_id, checkpoint_duplicates = _load_checkpoint(checkpoint_bytes, checkpoint)
    if set(checkpoint_by_id) - set(journal_by_id):
        extras = sorted(set(checkpoint_by_id) - set(journal_by_id))
        raise EvidenceIntegrityError(f"checkpoint contains battles absent from journal: {extras[:5]}")

    source_provenance = {
        "adapter": "combat_solver.evidence",
        "schema_version": 1,
        "source_files": source_files,
        "source_hashes": {key: value["sha256"] for key, value in source_files.items()},
    }
    evidence: list[DeployEvidence] = []
    inferred_turns = 0
    non_deploy_turns = 0
    unverified_deploy_turns = 0
    seen: dict[tuple[str, str, str, str, int], DeployEvidence] = {}
    verified_ranges: list[tuple[LogRange, str, str, int]] = []
    manifest_payload: dict[str, Any] = {}
    if manifest_bytes is not None:
        try:
            decoded_manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise EvidenceIntegrityError(
                f"cannot parse comparison manifest {manifest}: {exc}"
            ) from exc
        if not isinstance(decoded_manifest, dict):
            raise EvidenceIntegrityError("comparison manifest is not an object")
        manifest_payload = decoded_manifest
    allowed_log_root = manifest_payload.get("solver_log_root")
    source_provenance["solver_log_root"] = allowed_log_root
    for battle_id, row in journal_by_id.items():
        record = _record_for_row(row, checkpoint_by_id.get(battle_id))
        run_id = _nonempty_id(record.get("run_id")) or _nonempty_id(row.get("run_id"))
        if run_id is None:
            raise EvidenceIntegrityError(f"missing run_id for battle_id {battle_id!r}")
        seed = record.get("seed", row.get("seed"))
        if not _valid_seed(seed):
            raise EvidenceIntegrityError(f"missing seed for battle_id {battle_id!r}")
        turns = record.get("turns")
        if not isinstance(turns, list):
            raise EvidenceIntegrityError(f"missing turns[] for battle_id {battle_id!r}")
        for item in turns:
            if not isinstance(item, dict):
                raise EvidenceIntegrityError(f"non-object turn for battle_id {battle_id!r}")
            turn_value = item.get("turn")
            if isinstance(turn_value, bool) or not isinstance(turn_value, int) or turn_value < 1:
                raise EvidenceIntegrityError(f"invalid turn for battle_id {battle_id!r}")
            executed = item.get("executed")
            if not isinstance(executed, dict):
                raise EvidenceIntegrityError(f"missing executed evidence for {battle_id!r} turn {turn_value}")
            executed_turn = executed.get("turn")
            if (
                isinstance(executed_turn, bool)
                or not isinstance(executed_turn, int)
                or executed_turn < 1
            ):
                raise EvidenceIntegrityError(
                    f"invalid executed turn for {battle_id!r} turn {turn_value}"
                )
            source = executed.get("source")
            if source == "inferred":
                inferred_turns += 1
                continue
            if source == "deploy_log_unverified":
                unverified_deploy_turns += 1
                continue
            if source != "deploy_log":
                non_deploy_turns += 1
                continue
            if executed_turn != turn_value:
                raise EvidenceIntegrityError(f"executed turn mismatch for {battle_id!r} turn {turn_value}")
            if executed.get("ambiguous") is not False:
                raise EvidenceIntegrityError(f"deploy evidence is ambiguous for {battle_id!r} turn {turn_value}")
            actions = executed.get("actions")
            if not isinstance(actions, list) or any(not isinstance(action, dict) for action in actions):
                raise EvidenceIntegrityError(f"invalid deploy actions for {battle_id!r} turn {turn_value}")
            raw_range = executed.get("source_evidence")
            if not isinstance(raw_range, Mapping):
                # Legacy records remain readable, but are diagnostic only.
                unverified_deploy_turns += 1
                continue
            if not isinstance(allowed_log_root, str) or not allowed_log_root.strip():
                raise EvidenceIntegrityError(
                    "verified deploy range requires solver_log_root in comparison manifest"
                )
            try:
                log_range, _payload, marker_events = verify_log_range(
                    raw_range, allowed_root=Path(allowed_log_root)
                )
                # The range itself declares which producer grammar its bytes
                # were written in; re-verifying a grammar v2 range under v1's
                # request marker would reject every reuse-anchored deploy.
                validate_deploy_grammar(
                    marker_events, turn=turn_value, grammar=log_range.grammar
                )
                _validate_deploy_action_alignment(
                    marker_events,
                    actions,
                    battle_id=battle_id,
                    turn=turn_value,
                )
            except LogRangeError as exc:
                raise EvidenceIntegrityError(
                    f"invalid deploy log range for {battle_id!r} turn {turn_value}: {exc}"
                ) from exc
            explicit_decision = _nonempty_id(item.get("decision_id")) or _nonempty_id(
                executed.get("decision_id")
            )
            record_decision = _nonempty_id(record.get("decision_id"))
            decision = _turn_decision(
                {key: item.get(key) for key in ("decision_id", "state_hash", "snapshot")},
                {key: executed.get(key) for key in ("decision_id",)},
                record,
                include_record_fallback=True,
            )
            if decision is None:
                raise EvidenceIntegrityError(f"missing decision_id for {battle_id!r} turn {turn_value}")
            if (
                explicit_decision is None
                and record_decision is not None
                and len(turns) != 1
            ):
                raise EvidenceIntegrityError(
                    f"record-level decision_id cannot bind multiple turns for {battle_id!r}"
                )
            candidate = DeployEvidence(
                run_id=run_id,
                seed=seed,
                battle_id=battle_id,
                decision_id=decision,
                turn=turn_value,
                actions=tuple(dict(action) for action in actions),
                ambiguous=False,
                log_range=log_range.to_json(),
            )
            prior = seen.get(candidate.key())
            if prior is not None:
                if prior.actions != candidate.actions or prior.ambiguous != candidate.ambiguous:
                    raise EvidenceIntegrityError(f"conflicting duplicate execution evidence for {candidate.key()!r}")
                continue
            seen[candidate.key()] = candidate
            evidence.append(candidate)
            verified_ranges.append((log_range, run_id, battle_id, turn_value))

    for index, (left, left_run, left_battle, left_turn) in enumerate(verified_ranges):
        for right, right_run, right_battle, right_turn in verified_ranges[index + 1 :]:
            if not ranges_overlap(left, right):
                continue
            if (
                left_run,
                left_battle,
                left_turn,
                left.to_json(),
            ) == (
                right_run,
                right_battle,
                right_turn,
                right.to_json(),
            ):
                continue
            if (left_run, left_battle) != (right_run, right_battle):
                raise EvidenceIntegrityError(
                    "overlapping deploy log ranges have conflicting run/battle ownership"
                )

    events = [item.to_event(index, source_provenance) for index, item in enumerate(evidence)]
    return VerifiedComparisonEvidence({
        "schema_version": 1,
        "valid": True,
        "source": source_provenance,
        "events": events,
        "execution_evidence": [
            {
                "run_id": item.run_id,
                "seed": item.seed,
                "battle_id": item.battle_id,
                "decision_id": item.decision_id,
                "turn": item.turn,
                "source": "deploy_log",
                "actions": list(item.actions),
                "ambiguous": item.ambiguous,
                "log_range": dict(item.log_range),
            }
            for item in evidence
        ],
        "counts": {
            "battles": len(journal_by_id),
            "deploy_log_turns": len(evidence),
            "inferred_turns": inferred_turns,
            "non_deploy_turns": non_deploy_turns,
            "unverified_deploy_turns": unverified_deploy_turns,
            "journal_duplicates_deduped": journal_duplicates,
            "checkpoint_duplicates_deduped": checkpoint_duplicates,
        },
    })


# Stable aliases make the adapter easy to discover for callers that use
# either "load" or "build" terminology.
load_comparison_evidence = adapt_comparison_evidence
build_comparison_evidence = adapt_comparison_evidence


__all__ = [
    "DeployEvidence",
    "EvidenceIntegrityError",
    "VerifiedComparisonEvidence",
    "adapt_comparison_evidence",
    "build_comparison_evidence",
    "load_comparison_evidence",
]
