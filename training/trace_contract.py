from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


TRACE_VERSION = 1
CURRENT_PUBLIC_BETA_BUILD = "public-beta-v0.111.0"
DEFAULT_CHARACTER = "IRONCLAD"
DEFAULT_ASCENSION = 10
DATASET_SPLITS = frozenset({"train", "validation", "test"})
RESULT_STATUSES = frozenset({"applied", "rejected", "terminal", "error"})

REQUIRED_FIELDS = (
    "trace_version",
    "run_id",
    "decision_id",
    "step",
    "split",
    "build",
    "seed",
    "character",
    "ascension",
    "save_load_used",
    "visible_state",
    "legal_actions",
    "chosen_action",
    "result",
)


@dataclass(frozen=True)
class ValidationIssue:
    line: int
    code: str
    path: str
    message: str

    def render(self, source: str = "<record>") -> str:
        return f"{source}:{self.line}: {self.code} at {self.path}: {self.message}"


def normalize_seed(seed: object) -> str | None:
    """Return a stable seed key without accepting bool as an integer seed."""

    if isinstance(seed, bool):
        return None
    if isinstance(seed, int):
        return str(seed)
    if isinstance(seed, str):
        normalized = seed.strip().upper()
        return normalized or None
    return None


def _is_nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _issue(
    issues: list[ValidationIssue],
    line: int,
    code: str,
    path: str,
    message: str,
) -> None:
    issues.append(ValidationIssue(line=line, code=code, path=path, message=message))


def _validate_action(
    action: object,
    *,
    line: int,
    path: str,
    issues: list[ValidationIssue],
) -> tuple[str, str] | None:
    if not isinstance(action, dict):
        _issue(issues, line, "type", path, "must be an object")
        return None

    action_id = action.get("action_id")
    action_type = action.get("action_type")
    if not _is_nonempty_string(action_id):
        _issue(issues, line, "type", f"{path}.action_id", "must be a non-empty string")
    if not _is_nonempty_string(action_type):
        _issue(issues, line, "type", f"{path}.action_type", "must be a non-empty string")
    if not (_is_nonempty_string(action_id) and _is_nonempty_string(action_type)):
        return None
    return str(action_id).strip(), str(action_type).strip()


def validate_record(
    record: object,
    *,
    line: int = 1,
    expected_build: str | None = CURRENT_PUBLIC_BETA_BUILD,
    expected_character: str | None = DEFAULT_CHARACTER,
    expected_ascension: int | None = DEFAULT_ASCENSION,
    require_no_save_load: bool = True,
) -> list[ValidationIssue]:
    """Validate one trace record and return every discovered contract violation."""

    issues: list[ValidationIssue] = []
    if not isinstance(record, dict):
        _issue(issues, line, "type", "$", "record must be a JSON object")
        return issues

    for field in REQUIRED_FIELDS:
        if field not in record:
            _issue(issues, line, "required", f"$.{field}", "field is required")

    if record.get("trace_version") != TRACE_VERSION:
        _issue(
            issues,
            line,
            "trace_version",
            "$.trace_version",
            f"must equal {TRACE_VERSION}",
        )

    for field in ("run_id", "decision_id", "build", "character"):
        if field in record and not _is_nonempty_string(record[field]):
            _issue(issues, line, "type", f"$.{field}", "must be a non-empty string")

    step = record.get("step")
    if isinstance(step, bool) or not isinstance(step, int) or step < 0:
        _issue(issues, line, "type", "$.step", "must be a non-negative integer")

    split = record.get("split")
    if split not in DATASET_SPLITS:
        _issue(
            issues,
            line,
            "split",
            "$.split",
            f"must be one of {sorted(DATASET_SPLITS)}",
        )

    if normalize_seed(record.get("seed")) is None:
        _issue(
            issues,
            line,
            "seed",
            "$.seed",
            "must be a non-empty string or an integer (bool is not accepted)",
        )

    ascension = record.get("ascension")
    if isinstance(ascension, bool) or not isinstance(ascension, int):
        _issue(issues, line, "type", "$.ascension", "must be an integer")

    save_load_used = record.get("save_load_used")
    if not isinstance(save_load_used, bool):
        _issue(issues, line, "type", "$.save_load_used", "must be a boolean")
    elif require_no_save_load and save_load_used:
        _issue(
            issues,
            line,
            "sl_not_allowed",
            "$.save_load_used",
            "must be false for the no-SL A10 corpus",
        )

    if expected_build is not None and record.get("build") != expected_build:
        _issue(
            issues,
            line,
            "build_mismatch",
            "$.build",
            f"expected {expected_build!r}",
        )
    if expected_character is not None and record.get("character") != expected_character:
        _issue(
            issues,
            line,
            "character_mismatch",
            "$.character",
            f"expected {expected_character!r}",
        )
    if expected_ascension is not None and ascension != expected_ascension:
        _issue(
            issues,
            line,
            "ascension_mismatch",
            "$.ascension",
            f"expected A{expected_ascension}",
        )

    visible_state = record.get("visible_state")
    if not isinstance(visible_state, dict) or not visible_state:
        _issue(
            issues,
            line,
            "visible_state",
            "$.visible_state",
            "must be a non-empty object containing visible information only",
        )

    legal_actions = record.get("legal_actions")
    legal_by_id: dict[str, str] = {}
    if not isinstance(legal_actions, list) or not legal_actions:
        _issue(
            issues,
            line,
            "legal_actions",
            "$.legal_actions",
            "must be a non-empty array",
        )
    else:
        for index, action in enumerate(legal_actions):
            parsed = _validate_action(
                action,
                line=line,
                path=f"$.legal_actions[{index}]",
                issues=issues,
            )
            if parsed is None:
                continue
            action_id, action_type = parsed
            if action_id in legal_by_id:
                _issue(
                    issues,
                    line,
                    "duplicate_action_id",
                    f"$.legal_actions[{index}].action_id",
                    f"duplicate legal action id {action_id!r}",
                )
            else:
                legal_by_id[action_id] = action_type

    chosen = _validate_action(
        record.get("chosen_action"),
        line=line,
        path="$.chosen_action",
        issues=issues,
    )
    if chosen is not None:
        chosen_id, chosen_type = chosen
        legal_type = legal_by_id.get(chosen_id)
        if legal_type is None:
            _issue(
                issues,
                line,
                "illegal_chosen_action",
                "$.chosen_action.action_id",
                f"{chosen_id!r} is not present in legal_actions",
            )
        elif legal_type != chosen_type:
            _issue(
                issues,
                line,
                "action_type_mismatch",
                "$.chosen_action.action_type",
                f"expected {legal_type!r} for action {chosen_id!r}",
            )

    result = record.get("result")
    if not isinstance(result, dict):
        _issue(issues, line, "type", "$.result", "must be an object")
    else:
        if result.get("status") not in RESULT_STATUSES:
            _issue(
                issues,
                line,
                "result_status",
                "$.result.status",
                f"must be one of {sorted(RESULT_STATUSES)}",
            )
        if not isinstance(result.get("observed"), bool):
            _issue(
                issues,
                line,
                "type",
                "$.result.observed",
                "must be a boolean",
            )

    return issues


def find_seed_leaks(
    records: Iterable[tuple[int, dict[str, Any]]],
) -> list[ValidationIssue]:
    """Reject a seed appearing in more than one dataset split.

    Leakage is intentionally checked by normalized seed alone, not by build or
    character. This conservative rule prevents a known map/RNG trajectory from
    crossing from training into validation or test after a patch or relabel.
    """

    occurrences: dict[str, dict[str, list[int]]] = {}
    for line, record in records:
        split = record.get("split")
        seed = normalize_seed(record.get("seed"))
        if seed is None or split not in DATASET_SPLITS:
            continue
        occurrences.setdefault(seed, {}).setdefault(str(split), []).append(line)

    issues: list[ValidationIssue] = []
    for seed, by_split in sorted(occurrences.items()):
        if len(by_split) < 2:
            continue
        locations = ", ".join(
            f"{split}:lines {','.join(str(line) for line in lines)}"
            for split, lines in sorted(by_split.items())
        )
        first_line = min(line for lines in by_split.values() for line in lines)
        _issue(
            issues,
            first_line,
            "seed_leakage",
            "$.seed",
            f"normalized seed {seed!r} occurs across splits ({locations})",
        )
    return issues

