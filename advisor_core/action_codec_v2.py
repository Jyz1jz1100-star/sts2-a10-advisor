"""Versioned codec for the emulator's separate ``(action, target)`` API.

The emulator exposes legality for the base integer action only.  In combat it
accepts the enemy target through a second integer argument.  Treating the mask
as a complete action space either loses target choice or, when implemented as
an unconditional cartesian product, creates aliases such as one ``end turn``
action per enemy.  This module builds the exact, variable-length candidate set
without importing or modifying the emulator.

``action_id`` is semantic identity for traces, behaviour cloning, search and
rendering.  ``flat_index`` is deliberately only the position in one state's
candidate list and must not be persisted across states.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote


ACTION_ID_VERSION = "v2"


@dataclass(frozen=True, slots=True)
class EnemyTarget:
    """One currently visible and selectable enemy target.

    ``target_index`` is the integer passed to the emulator. ``target_id`` is
    the stable, visible identity used in datasets and recommendations.
    """

    target_index: int
    target_id: str
    label: str = ""


@dataclass(frozen=True, slots=True)
class FlatAction:
    """One legal member of a state-local flat action space."""

    flat_index: int
    action: int
    target: int | None
    action_id: str
    action_key: str
    target_id: str | None = None
    action_label: str = ""
    target_label: str = ""

    @property
    def emulator_pair(self) -> tuple[int, int]:
        """Return the exact pair expected by ``Sts2RunEnv.step``."""

        return self.action, -1 if self.target is None else self.target


def _escape(component: str) -> str:
    return quote(component, safe="-._~")


def stable_action_id(action_key: str, target_id: str | None = None) -> str:
    """Build a reversible-looking, delimiter-safe V2 semantic action id."""

    key = str(action_key).strip()
    if not key:
        raise ValueError("action_key must be non-empty")
    prefix = f"{ACTION_ID_VERSION}:a:{_escape(key)}"
    if target_id is None:
        return prefix
    target = str(target_id).strip()
    if not target:
        raise ValueError("target_id must be non-empty")
    return f"{prefix}:t:{_escape(target)}"


def visible_enemy_targets(enemies: Iterable[Mapping[str, Any]]) -> tuple[EnemyTarget, ...]:
    """Normalize raw visible enemies to target records.

    Dead, zero-HP, explicitly untargetable and invisible entries are removed.
    Input order supplies the native enemy index unless ``target_index`` or
    ``enemy_index`` is present.  An explicit visible identity is required so
    persisted ``action_id`` values never silently depend on a display label.
    """

    targets: list[EnemyTarget] = []
    for fallback_index, enemy in enumerate(enemies):
        if enemy.get("is_dead") or enemy.get("targetable") is False:
            continue
        if enemy.get("is_visible") is False:
            continue
        hp = enemy.get("hp", enemy.get("current_hp"))
        if hp is not None and float(hp) <= 0:
            continue

        raw_id = enemy.get("entity_id", enemy.get("id", enemy.get("combat_id")))
        if raw_id is None or not str(raw_id).strip():
            raise ValueError("every visible enemy target must have a stable id")
        raw_index = enemy.get("target_index", enemy.get("enemy_index", fallback_index))
        target_index = int(raw_index)
        if target_index < 0:
            raise ValueError("enemy target_index must be non-negative")
        label = str(enemy.get("name") or raw_id)
        targets.append(EnemyTarget(target_index, str(raw_id), label))

    _validate_targets(targets)
    return tuple(targets)


def _validate_targets(targets: Sequence[EnemyTarget]) -> None:
    indices = [target.target_index for target in targets]
    ids = [target.target_id for target in targets]
    if any(index < 0 for index in indices):
        raise ValueError("enemy target_index must be non-negative")
    if any(not target_id.strip() for target_id in ids):
        raise ValueError("enemy target_id must be non-empty")
    if len(indices) != len(set(indices)):
        raise ValueError("enemy target_index values must be unique")
    if len(ids) != len(set(ids)):
        raise ValueError("enemy target_id values must be unique")


class ActionTargetCodecV2:
    """State-local bijection between flat indices and emulator action pairs.

    Args:
        legal_action_mask: Boolean-like values indexed by emulator action.
        targeted_actions: Base action indices which require one enemy target.
        targets: Already normalized, visible and selectable enemy targets.
        action_keys: Optional semantic keys (for example a card instance id).
            Missing keys fall back to ``simulator:<index>``.
        action_labels: Optional human-readable labels for later explanation.
        valid_target_ids: Optional per-action allow-list.  This represents
            card-specific target restrictions without changing the base mask.
    """

    def __init__(
        self,
        legal_action_mask: Sequence[Any],
        targeted_actions: Iterable[int],
        targets: Sequence[EnemyTarget],
        *,
        action_keys: Mapping[int, str] | None = None,
        action_labels: Mapping[int, str] | None = None,
        valid_target_ids: Mapping[int, Iterable[str]] | None = None,
    ) -> None:
        # NumPy masks deliberately reject truth-value coercion, so legality
        # checks must be element-wise and emptiness must use ``len``.
        if len(legal_action_mask) == 0:
            raise ValueError("legal_action_mask must be non-empty")

        targeted = {int(action) for action in targeted_actions}
        if any(action < 0 or action >= len(legal_action_mask) for action in targeted):
            raise ValueError("targeted action index is outside legal_action_mask")
        _validate_targets(targets)

        keys = dict(action_keys or {})
        labels = dict(action_labels or {})
        restrictions = {
            int(action): {str(target_id) for target_id in allowed}
            for action, allowed in (valid_target_ids or {}).items()
        }
        unknown_restrictions = set(restrictions) - targeted
        if unknown_restrictions:
            raise ValueError("valid_target_ids may only restrict targeted actions")

        candidates: list[FlatAction] = []
        for action, is_legal in enumerate(legal_action_mask):
            if not bool(is_legal):
                continue
            action_key = str(keys.get(action, f"simulator:{action}"))
            action_label = str(labels.get(action, action_key))
            if action in targeted:
                allowed = restrictions.get(action)
                for target in targets:
                    if allowed is not None and target.target_id not in allowed:
                        continue
                    candidates.append(
                        FlatAction(
                            flat_index=len(candidates),
                            action=action,
                            target=target.target_index,
                            action_id=stable_action_id(action_key, target.target_id),
                            action_key=action_key,
                            target_id=target.target_id,
                            action_label=action_label,
                            target_label=target.label,
                        )
                    )
            else:
                candidates.append(
                    FlatAction(
                        flat_index=len(candidates),
                        action=action,
                        target=None,
                        action_id=stable_action_id(action_key),
                        action_key=action_key,
                        action_label=action_label,
                    )
                )

        ids = [candidate.action_id for candidate in candidates]
        pairs = [(candidate.action, candidate.target) for candidate in candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("action_keys and target_ids produced duplicate action_id values")
        if len(pairs) != len(set(pairs)):
            raise ValueError("candidate action/target pairs must be unique")

        self._candidates = tuple(candidates)
        self._index_by_pair = {
            (candidate.action, candidate.target): candidate.flat_index
            for candidate in self._candidates
        }
        self._index_by_id = {
            candidate.action_id: candidate.flat_index for candidate in self._candidates
        }

    @classmethod
    def from_visible_enemies(
        cls,
        legal_action_mask: Sequence[Any],
        targeted_actions: Iterable[int],
        enemies: Iterable[Mapping[str, Any]],
        **kwargs: Any,
    ) -> ActionTargetCodecV2:
        """Build directly from a simulator mask and visible enemy mappings."""

        return cls(
            legal_action_mask,
            targeted_actions,
            visible_enemy_targets(enemies),
            **kwargs,
        )

    @property
    def candidates(self) -> tuple[FlatAction, ...]:
        return self._candidates

    def __len__(self) -> int:
        return len(self._candidates)

    def decode(self, flat_index: int) -> FlatAction:
        """Decode a state-local flat index, rejecting Python negative aliases."""

        if not isinstance(flat_index, int) or isinstance(flat_index, bool):
            raise TypeError("flat_index must be an integer")
        if flat_index < 0 or flat_index >= len(self._candidates):
            raise IndexError("flat_index is outside the legal candidate set")
        return self._candidates[flat_index]

    def encode(self, action: int, target: int | None = None) -> int:
        """Encode one exact pair; ``-1`` is accepted as the native no-target value."""

        normalized_target = None if target is None or target == -1 else int(target)
        try:
            return self._index_by_pair[(int(action), normalized_target)]
        except KeyError as exc:
            raise ValueError("action/target pair is not legal in this state") from exc

    def encode_action_id(self, action_id: str) -> int:
        """Resolve a persisted semantic id to the current flat index."""

        try:
            return self._index_by_id[action_id]
        except KeyError as exc:
            raise ValueError("action_id is not legal in this state") from exc
