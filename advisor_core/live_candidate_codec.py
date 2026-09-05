"""Read-only, variable-candidate mapping for STS2MCP live JSON.

The codec consumes one already observed STS2MCP state and returns the
currently visible, legal candidates.  It never polls the game, posts an
action, imports the bridge/autoplay stack, or invents a policy explanation.

The accepted state families are derived from the local STS2MCP v0.4.0 C#
state builder (game build ``public-beta-v0.111.0``): ``map``, ``card_reward``,
``shop``, ``rest_site`` and ``event``.  A state whose event id is ``NEOW`` is
classified as the ``neow`` family; the source still reports its screen type
as ``event``.  ``state_type: "neow"`` is also accepted as an explicit input
alias when its container is ``event`` or ``neow``.

The wire payloads are the exact action bodies documented by STS2MCP.  A
candidate's ``comment_zh`` is deliberately ``None``: a later human/teacher
annotation may fill that slot, but this module does not manufacture strategy
text.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, TypeAlias
from urllib.parse import quote


LIVE_BUILD = "public-beta-v0.111.0"
CONTRACT_VERSION = "live-candidates-v1"
SUPPORTED_SCREEN_TYPES = frozenset(
    {"map", "card_reward", "shop", "rest_site", "event", "neow"}
)

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class LiveCandidateContractError(ValueError):
    """Base error for an untrusted or non-actionable live state."""


class UnsupportedScreenError(LiveCandidateContractError):
    """The state is not one of the supported out-of-combat families."""


class MissingStateError(LiveCandidateContractError):
    """Required visible state or a required field is absent/unusable."""


class EmptyCandidateError(LiveCandidateContractError):
    """The state has no legal visible candidate."""


class AmbiguousCandidateError(LiveCandidateContractError):
    """Two candidates cannot be distinguished by the visible contract."""


class IllegalCandidateError(LiveCandidateContractError):
    """A candidate would not be accepted by the documented wire endpoint."""


# Friendly aliases for callers that prefer state/action terminology.
UnknownScreenError = UnsupportedScreenError
AmbiguousStateError = AmbiguousCandidateError
InvalidCandidateError = IllegalCandidateError


def _error(
    path: str,
    message: str,
    error_type: type[LiveCandidateContractError] = MissingStateError,
) -> None:
    raise error_type(f"{path}: {message}")


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _error(path, "must be a JSON object")
    return dict(value)


def _nonempty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _error(path, "must be a non-empty visible string")
    return value.strip()


def _nullable_string(value: Any, path: str) -> str | None:
    if value is not None and not isinstance(value, str):
        _error(path, "must be a string or null")
    return value


def _required_string(container: Mapping[str, Any], key: str, path: str) -> str:
    if key not in container:
        _error(f"{path}.{key}", "is required")
    return _nonempty_string(container[key], f"{path}.{key}")


def _required_nullable_string(
    container: Mapping[str, Any], key: str, path: str
) -> str | None:
    if key not in container:
        _error(f"{path}.{key}", "is required")
    return _nullable_string(container[key], f"{path}.{key}")


def _index(value: Any, path: str) -> int:
    # bool is an int subclass but is not a legal STS2MCP index.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _error(path, "must be a non-negative integer", IllegalCandidateError)
    return value


def _coordinate(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _error(path, "must be a non-negative integer", IllegalCandidateError)
    return value


def _required_index(container: Mapping[str, Any], key: str, path: str) -> int:
    if key not in container:
        _error(f"{path}.{key}", "is required")
    return _index(container[key], f"{path}.{key}")


def _required_bool(container: Mapping[str, Any], key: str, path: str) -> bool:
    if key not in container:
        _error(f"{path}.{key}", "is required")
    value = container[key]
    if not isinstance(value, bool):
        _error(f"{path}.{key}", "must be a boolean")
    return value


def _required_nonnegative_int(
    container: Mapping[str, Any], key: str, path: str
) -> int:
    if key not in container:
        _error(f"{path}.{key}", "is required")
    value = container[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _error(f"{path}.{key}", "must be a non-negative integer")
    return value


def _json_value(value: Any, path: str = "$") -> JsonValue:
    """Copy only JSON values and reject non-finite numbers/opaque objects."""

    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            _error(path, "must not contain NaN or infinity", IllegalCandidateError)
        return value
    if isinstance(value, Mapping):
        copied: dict[str, JsonValue] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                _error(path, "JSON object keys must be strings", IllegalCandidateError)
            copied[key] = _json_value(child, f"{path}.{key}")
        return copied
    if isinstance(value, (list, tuple)):
        return [_json_value(child, f"{path}[{i}]") for i, child in enumerate(value)]
    _error(path, f"contains unsupported value type {type(value).__name__}", IllegalCandidateError)
    raise AssertionError("unreachable")


def _freeze_json(value: JsonValue) -> Any:
    """Recursively freeze validated JSON for an immutable candidate record."""

    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_json(child) for key, child in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(child) for child in value)
    return value


def _thaw_json(value: Any) -> JsonValue:
    """Return a fresh ordinary JSON tree to prevent caller mutation."""

    if isinstance(value, Mapping):
        return {key: _thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(child) for child in value]
    return value


def _quote(value: str) -> str:
    return quote(value, safe="-._~")


def _visible_text(item: Mapping[str, Any], *keys: str, path: str) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    _error(path, "candidate has no visible text")
    raise AssertionError("unreachable")


_STRING_FEATURES = frozenset(
    {
        "id",
        "name",
        "type",
        "cost",
        "star_cost",
        "description",
        "rarity",
        "category",
        "card_id",
        "card_name",
        "card_type",
        "card_cost",
        "card_star_cost",
        "card_rarity",
        "card_description",
        "relic_id",
        "relic_name",
        "relic_description",
        "potion_id",
        "potion_name",
        "potion_description",
        "title",
        "event_id",
        "event_name",
        "body",
    }
)
_BOOL_FEATURES = frozenset(
    {
        "is_upgraded",
        "is_stocked",
        "can_afford",
        "on_sale",
        "is_enabled",
        "is_locked",
        "is_proceed",
        "was_chosen",
        "is_ancient",
    }
)
_INT_FEATURES = frozenset({"index", "col", "row", "price"})


def _project_keywords(value: Any, path: str) -> list[JsonValue]:
    """Project the exact visible ``BuildHoverTips`` shape."""

    if not isinstance(value, list):
        _error(path, "must be an array")
    output: list[JsonValue] = []
    for number, raw in enumerate(value):
        tip_path = f"{path}[{number}]"
        tip = _mapping(raw, tip_path)
        projected: dict[str, JsonValue] = {}
        for key in ("name", "description"):
            if key not in tip:
                continue
            projected[key] = _nullable_string(tip[key], f"{tip_path}.{key}")
        if not projected:
            _error(tip_path, "must expose name or description")
        if all(value is None or not str(value).strip() for value in projected.values()):
            _error(tip_path, "must expose non-empty visible text")
        output.append(projected)
    return output


def _project_leads_to(value: Any, path: str) -> list[JsonValue]:
    """Project map lookahead children; hidden child fields are discarded."""

    if not isinstance(value, list):
        _error(path, "must be an array")
    output: list[JsonValue] = []
    for number, raw in enumerate(value):
        child_path = f"{path}[{number}]"
        child = _mapping(raw, child_path)
        col = _coordinate(child.get("col"), f"{child_path}.col")
        row = _coordinate(child.get("row"), f"{child_path}.row")
        node_type = _nonempty_string(child.get("type"), f"{child_path}.type")
        output.append({"col": col, "row": row, "type": node_type})
    return output


def _present_features(
    item: Mapping[str, Any], fields: Sequence[str], path: str
) -> dict[str, JsonValue]:
    """Select and validate a known visible subset, never raw nested state."""

    output: dict[str, JsonValue] = {}
    for field_name in fields:
        if field_name not in item:
            continue
        value = item[field_name]
        field_path = f"{path}.{field_name}"
        if field_name == "keywords":
            output[field_name] = _project_keywords(value, field_path)
        elif field_name == "leads_to":
            output[field_name] = _project_leads_to(value, field_path)
        elif field_name in _STRING_FEATURES:
            output[field_name] = _nullable_string(value, field_path)
        elif field_name in _BOOL_FEATURES:
            if not isinstance(value, bool):
                _error(field_path, "must be a boolean")
            output[field_name] = value
        elif field_name in _INT_FEATURES:
            if isinstance(value, bool) or not isinstance(value, int):
                _error(field_path, "must be an integer")
            output[field_name] = value
        else:
            # This is a programmer error in the allowlist, not permission to
            # retain an opaque object from the live state.
            if isinstance(value, (Mapping, list, tuple)):
                _error(field_path, "nested values are not in the visible allowlist")
            output[field_name] = _json_value(value, field_path)
    return output


@dataclass(frozen=True, slots=True, init=False)
class LiveCandidate:
    """One legal candidate from one state-local live decision window.

    The public mapping properties return fresh JSON copies.  Internally the
    validated trees are recursively frozen, so mutating a returned action or
    feature dictionary cannot mutate the legal set that was checked.
    """

    family: str
    identity: str
    _wire_action: Mapping[str, Any] = field(repr=False)
    text: str
    _features: Mapping[str, Any] = field(repr=False)
    comment_zh: str | None = None

    def __init__(
        self,
        family: str,
        identity: str,
        wire_action: Mapping[str, Any],
        text: str,
        features: Mapping[str, Any],
        comment_zh: str | None = None,
    ) -> None:
        if not isinstance(family, str) or not family.strip():
            _error("candidate.family", "must be non-empty", IllegalCandidateError)
        if not isinstance(identity, str) or not identity.strip():
            _error("candidate.identity", "must be non-empty", IllegalCandidateError)
        if not isinstance(wire_action, Mapping) or not wire_action.get("action"):
            _error(
                "candidate.wire_action",
                "must contain an action name",
                IllegalCandidateError,
            )
        if not isinstance(features, Mapping):
            _error("candidate.features", "must be a JSON object", IllegalCandidateError)
        action = _json_value(dict(wire_action), "candidate.wire_action")
        feature_copy = _json_value(dict(features), "candidate.features")
        if not isinstance(action, dict) or not isinstance(feature_copy, dict):
            raise AssertionError("candidate mappings must remain objects")
        visible = _nonempty_string(text, "candidate.text")
        if comment_zh is not None and not isinstance(comment_zh, str):
            _error("candidate.comment_zh", "must be a string or null", IllegalCandidateError)
        object.__setattr__(self, "family", family)
        object.__setattr__(self, "identity", identity)
        object.__setattr__(self, "_wire_action", _freeze_json(action))
        object.__setattr__(self, "text", visible)
        object.__setattr__(self, "_features", _freeze_json(feature_copy))
        object.__setattr__(self, "comment_zh", comment_zh)

    @property
    def wire_action(self) -> dict[str, JsonValue]:
        return _thaw_json(self._wire_action)  # type: ignore[return-value]

    @property
    def features(self) -> dict[str, JsonValue]:
        return _thaw_json(self._features)  # type: ignore[return-value]

    @property
    def action(self) -> dict[str, JsonValue]:
        """Compatibility spelling for code that calls the payload ``action``."""

        return self.wire_action

    @property
    def action_id(self) -> str:
        """Compatibility spelling used by the trace contract."""

        return self.identity

    @property
    def visible_text(self) -> str:
        return self.text

    @property
    def structural_features(self) -> dict[str, JsonValue]:
        return self.features

    @property
    def annotation_zh(self) -> str | None:
        return self.comment_zh

    @property
    def zh_comment(self) -> str | None:
        return self.comment_zh

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "family": self.family,
            "identity": self.identity,
            "wire_action": self.wire_action,
            "text": self.text,
            "features": self.features,
            "comment_zh": self.comment_zh,
        }

    as_dict = to_dict


def _candidate(
    family: str,
    identity: str,
    wire_action: Mapping[str, Any],
    text: str,
    features: Mapping[str, Any],
) -> LiveCandidate:
    return LiveCandidate(family, identity, wire_action, text, features)


@dataclass(frozen=True, slots=True)
class LiveCandidateSet(Sequence[LiveCandidate]):
    """A state-local candidate list plus an identity/action codec."""

    family: str
    state_type: str
    candidates: tuple[LiveCandidate, ...]
    # This is a contract label when the observed state has no build field; it
    # is not a runtime version check.  Lock/preflight code owns that check.
    build: str = LIVE_BUILD

    def __post_init__(self) -> None:
        if self.family not in SUPPORTED_SCREEN_TYPES:
            raise UnsupportedScreenError(f"unsupported candidate family {self.family!r}")
        if not self.candidates:
            raise EmptyCandidateError("candidate set must not be empty")
        identities = [candidate.identity for candidate in self.candidates]
        if len(identities) != len(set(identities)):
            raise AmbiguousCandidateError("duplicate candidate identity")
        actions = [self._action_key(candidate.wire_action) for candidate in self.candidates]
        if len(actions) != len(set(actions)):
            raise AmbiguousCandidateError("duplicate wire action")

    @staticmethod
    def _action_key(action: Mapping[str, Any]) -> str:
        try:
            return json.dumps(
                action,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise IllegalCandidateError("wire action is not JSON serializable") from exc

    def __len__(self) -> int:
        return len(self.candidates)

    def __iter__(self) -> Iterator[LiveCandidate]:
        return iter(self.candidates)

    def __getitem__(self, index: int | slice) -> LiveCandidate | tuple[LiveCandidate, ...]:
        return self.candidates[index]

    def candidate_for(self, identity: str) -> LiveCandidate:
        for candidate in self.candidates:
            if candidate.identity == identity:
                return candidate
        raise IllegalCandidateError(f"identity {identity!r} is not legal in this state")

    def identity_for(self, wire_action: Mapping[str, Any]) -> str:
        if not isinstance(wire_action, Mapping):
            raise IllegalCandidateError("wire action must be an object")
        key = self._action_key(wire_action)
        for candidate in self.candidates:
            if self._action_key(candidate.wire_action) == key:
                return candidate.identity
        raise IllegalCandidateError("wire action is not legal in this state")

    def decode(self, identity: str) -> dict[str, JsonValue]:
        """Resolve a stable identity into the exact current wire payload."""

        return self.candidate_for(identity).action

    def encode(self, wire_action: Mapping[str, Any]) -> str:
        """Resolve an exact wire payload into its stable current identity."""

        return self.identity_for(wire_action)

    action_for = decode
    identity_for_action = encode

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "contract_version": CONTRACT_VERSION,
            "build": self.build,
            "state_type": self.state_type,
            "family": self.family,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }

    as_dict = to_dict

    def to_json(self, **kwargs: Any) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, allow_nan=False, **kwargs
        )


class LiveCandidateCodec:
    """Convenience wrapper that builds a read-only codec from one live state."""

    def __init__(self, state: Mapping[str, Any]) -> None:
        self._set = extract_live_candidates(state)

    @classmethod
    def from_state(cls, state: Mapping[str, Any]) -> "LiveCandidateCodec":
        return cls(state)

    @classmethod
    def extract(cls, state: Mapping[str, Any]) -> LiveCandidateSet:
        return extract_live_candidates(state)

    @property
    def family(self) -> str:
        return self._set.family

    @property
    def state_type(self) -> str:
        return self._set.state_type

    @property
    def build(self) -> str:
        return self._set.build

    @property
    def candidates(self) -> tuple[LiveCandidate, ...]:
        return self._set.candidates

    def __len__(self) -> int:
        return len(self._set)

    def __iter__(self) -> Iterator[LiveCandidate]:
        return iter(self._set)

    def __getitem__(self, index: int | slice) -> LiveCandidate | tuple[LiveCandidate, ...]:
        return self._set[index]

    def candidate_for(self, identity: str) -> LiveCandidate:
        return self._set.candidate_for(identity)

    def identity_for(self, wire_action: Mapping[str, Any]) -> str:
        return self._set.identity_for(wire_action)

    identity_for_action = identity_for

    def decode(self, identity: str) -> dict[str, JsonValue]:
        return self._set.decode(identity)

    action_for = decode

    def encode(self, wire_action: Mapping[str, Any]) -> str:
        return self._set.encode(wire_action)

    def to_dict(self) -> dict[str, JsonValue]:
        return self._set.to_dict()

    as_dict = to_dict

    def to_json(self, **kwargs: Any) -> str:
        return self._set.to_json(**kwargs)


def _unwrap_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Accept raw state or the fixture envelope without inferring a screen."""

    root = _mapping(state, "state")
    if "state_type" in root:
        value = root
    elif isinstance(root.get("state"), Mapping):
        value = _mapping(root["state"], "state.state")
    elif "state_type" in state:
        value = dict(state)
    else:
        _error("state.state_type", "is required; refusing to infer it")

    # A supplied build is a compatibility assertion.  When the wrapper has a
    # build and the nested state does not, retain that assertion as well.
    for source, path in ((state, "state"), (value, "state")):
        if "build" in source:
            supplied_build = source["build"]
            if supplied_build != LIVE_BUILD:
                _error(
                    f"{path}.build",
                    f"unsupported live build {supplied_build!r}",
                    UnsupportedScreenError,
                )
    state_type = value.get("state_type")
    if not isinstance(state_type, str) or not state_type.strip():
        _error("state.state_type", "is required; refusing to infer it")
    return value


def _container(state: Mapping[str, Any], key: str) -> dict[str, Any]:
    if key not in state:
        _error(f"state.{key}", "container is required")
    return _mapping(state[key], f"state.{key}")


def _array(container: Mapping[str, Any], key: str, path: str) -> list[Any]:
    """Require a source array, but allow an empty array for a real transition."""

    if key not in container:
        _error(f"{path}.{key}", "is required")
    value = container[key]
    if not isinstance(value, list):
        _error(f"{path}.{key}", "must be an array")
    return value


def _check_indexes(indexes: set[int], path: str, index: int) -> None:
    if index in indexes:
        _error(
            f"{path}.index",
            f"duplicate wire index {index}",
            AmbiguousCandidateError,
        )
    indexes.add(index)


def _finish(
    family: str, state_type: str, candidates: list[LiveCandidate]
) -> LiveCandidateSet:
    if not candidates:
        raise EmptyCandidateError(f"{family}: no legal visible candidates")
    return LiveCandidateSet(family, state_type, tuple(candidates))


def _map_candidates(state: Mapping[str, Any]) -> LiveCandidateSet:
    container = _container(state, "map")
    options = _array(container, "next_options", "state.map")
    candidates: list[LiveCandidate] = []
    indexes: set[int] = set()
    for number, raw in enumerate(options):
        path = f"state.map.next_options[{number}]"
        option = _mapping(raw, path)
        index = _required_index(option, "index", path)
        _check_indexes(indexes, path, index)
        col = _coordinate(option.get("col"), f"{path}.col")
        row = _coordinate(option.get("row"), f"{path}.row")
        node_type = _nonempty_string(option.get("type"), f"{path}.type")
        identity = f"map:{_quote(node_type)}:{col}:{row}"
        features = _present_features(
            option, ("index", "col", "row", "type", "leads_to"), path
        )
        candidates.append(
            _candidate(
                "map",
                identity,
                {"action": "choose_map_node", "index": index},
                node_type,
                features,
            )
        )
    return _finish("map", "map", candidates)


def _card_reward_candidates(state: Mapping[str, Any]) -> LiveCandidateSet:
    container = _container(state, "card_reward")
    cards = _array(container, "cards", "state.card_reward")
    can_skip = _required_bool(container, "can_skip", "state.card_reward")
    candidates: list[LiveCandidate] = []
    indexes: set[int] = set()
    for number, raw in enumerate(cards):
        path = f"state.card_reward.cards[{number}]"
        card = _mapping(raw, path)
        index = _required_index(card, "index", path)
        _check_indexes(indexes, path, index)
        card_id = _required_string(card, "id", path)
        upgraded = _required_bool(card, "is_upgraded", path)
        text = _visible_text(card, "name", "id", path=path)
        identity = (
            f"card_reward:{_quote(card_id)}:upgraded:{int(upgraded)}:slot:{index}"
        )
        fields = (
            "index",
            "id",
            "name",
            "type",
            "cost",
            "star_cost",
            "description",
            "rarity",
            "is_upgraded",
            "keywords",
        )
        candidates.append(
            _candidate(
                "card_reward",
                identity,
                {"action": "select_card_reward", "card_index": index},
                text,
                _present_features(card, fields, path),
            )
        )
    if can_skip:
        candidates.append(
            _candidate(
                "card_reward",
                "card_reward:skip",
                {"action": "skip_card_reward"},
                "跳过卡牌奖励",
                {"can_skip": True},
            )
        )
    return _finish("card_reward", "card_reward", candidates)


def _shop_item_identity(
    item: Mapping[str, Any], path: str, category: str
) -> tuple[str, str]:
    if category == "card":
        item_id = _required_string(item, "card_id", path)
        text = _visible_text(item, "card_name", "card_id", path=path)
    elif category == "relic":
        item_id = _required_string(item, "relic_id", path)
        text = _visible_text(item, "relic_name", "relic_id", path=path)
    elif category == "potion":
        item_id = _required_string(item, "potion_id", path)
        text = _visible_text(item, "potion_name", "potion_id", path=path)
    elif category == "card_removal":
        # The C# source emits no id/name for this service.  Its category is
        # the only visible identity; the label is a display translation, not
        # a strategy annotation.
        item_id = "card_removal"
        text = "移除卡牌"
    else:
        _error(
            f"{path}.category",
            f"unsupported shop category {category!r}",
            IllegalCandidateError,
        )
    return item_id, text


def _shop_candidates(state: Mapping[str, Any]) -> LiveCandidateSet:
    container = _container(state, "shop")
    if "error" in container:
        error = container["error"]
        if not isinstance(error, str) or not error.strip():
            _error("state.shop.error", "must be a non-empty string when present")
        _error("state.shop.error", "inventory is not ready; refusing transitional state")
    items = _array(container, "items", "state.shop")
    can_proceed = _required_bool(container, "can_proceed", "state.shop")
    candidates: list[LiveCandidate] = []
    indexes: set[int] = set()
    for number, raw in enumerate(items):
        path = f"state.shop.items[{number}]"
        item = _mapping(raw, path)
        index = _required_index(item, "index", path)
        _check_indexes(indexes, path, index)
        category = _required_string(item, "category", path)
        if category not in {"card", "relic", "potion", "card_removal"}:
            _error(
                f"{path}.category",
                f"unsupported shop category {category!r}",
                IllegalCandidateError,
            )
        _required_nonnegative_int(item, "price", path)
        stocked = _required_bool(item, "is_stocked", path)
        affordable = _required_bool(item, "can_afford", path)
        if category == "card":
            _required_bool(item, "on_sale", path)
        if not stocked or not affordable:
            continue
        item_id, text = _shop_item_identity(item, path, category)
        identity = f"shop:{_quote(category)}:{_quote(item_id)}:slot:{index}"
        fields = (
            "index",
            "category",
            "price",
            "is_stocked",
            "can_afford",
            "on_sale",
            "card_id",
            "card_name",
            "card_type",
            "card_cost",
            "card_star_cost",
            "card_rarity",
            "card_description",
            "relic_id",
            "relic_name",
            "relic_description",
            "potion_id",
            "potion_name",
            "potion_description",
            "keywords",
        )
        candidates.append(
            _candidate(
                "shop",
                identity,
                {"action": "shop_purchase", "index": index},
                text,
                _present_features(item, fields, path),
            )
        )
    if can_proceed:
        candidates.append(
            _candidate(
                "shop",
                "shop:proceed",
                {"action": "proceed"},
                "离开商店",
                {"can_proceed": True},
            )
        )
    return _finish("shop", "shop", candidates)


def _rest_candidates(state: Mapping[str, Any]) -> LiveCandidateSet:
    container = _container(state, "rest_site")
    options = _array(container, "options", "state.rest_site")
    can_proceed = _required_bool(container, "can_proceed", "state.rest_site")
    candidates: list[LiveCandidate] = []
    indexes: set[int] = set()
    identities: set[str] = set()
    for number, raw in enumerate(options):
        path = f"state.rest_site.options[{number}]"
        option = _mapping(raw, path)
        index = _required_index(option, "index", path)
        _check_indexes(indexes, path, index)
        option_id = _required_string(option, "id", path)
        enabled = _required_bool(option, "is_enabled", path)
        if not enabled:
            continue
        identity = f"rest_site:{_quote(option_id)}"
        if identity in identities:
            _error(
                path,
                f"duplicate visible option identity {identity!r}",
                AmbiguousCandidateError,
            )
        identities.add(identity)
        text = _visible_text(option, "name", "description", "id", path=path)
        fields = ("index", "id", "name", "description", "is_enabled")
        candidates.append(
            _candidate(
                "rest_site",
                identity,
                {"action": "choose_rest_option", "index": index},
                text,
                _present_features(option, fields, path),
            )
        )
    if can_proceed:
        candidates.append(
            _candidate(
                "rest_site",
                "rest_site:proceed",
                {"action": "proceed"},
                "前往地图",
                {"can_proceed": True},
            )
        )
    return _finish("rest_site", "rest_site", candidates)


def _event_container(
    state: Mapping[str, Any], screen_type: str
) -> tuple[str, dict[str, Any]]:
    if screen_type == "event":
        if "event" in state and "neow" in state:
            _error(
                "state",
                "both event and neow containers are present",
                AmbiguousCandidateError,
            )
        return "event", _container(state, "event")
    if "event" in state and "neow" in state:
        _error(
            "state",
            "both event and neow containers are present",
            AmbiguousCandidateError,
        )
    if "neow" in state:
        return "neow", _container(state, "neow")
    return "event", _container(state, "event")


def _event_candidates(
    state: Mapping[str, Any], screen_type: str
) -> LiveCandidateSet:
    container_key, container = _event_container(state, screen_type)
    path = f"state.{container_key}"
    event_id = _required_string(container, "event_id", path)
    if screen_type == "neow" and event_id.upper() != "NEOW":
        _error(
            f"{path}.event_id",
            "state_type neow requires event_id NEOW",
            UnsupportedScreenError,
        )
    event_name = _required_nullable_string(container, "event_name", path)
    is_ancient = _required_bool(container, "is_ancient", path)
    body = _required_nullable_string(container, "body", path)
    in_dialogue = _required_bool(container, "in_dialogue", path)
    options = _array(container, "options", path)
    family = "neow" if event_id.upper() == "NEOW" else "event"
    if in_dialogue:
        _error(f"{path}.in_dialogue", "choice list is not actionable during dialogue")

    candidates: list[LiveCandidate] = []
    indexes: set[int] = set()
    identities: set[str] = set()
    context: dict[str, JsonValue] = {
        "event_id": event_id,
        "event_name": event_name,
        "is_ancient": is_ancient,
        "body": body,
    }
    for number, raw in enumerate(options):
        option_path = f"{path}.options[{number}]"
        option = _mapping(raw, option_path)
        index = _required_index(option, "index", option_path)
        _check_indexes(indexes, option_path, index)
        locked = _required_bool(option, "is_locked", option_path)
        _required_bool(option, "is_proceed", option_path)
        chosen = _required_bool(option, "was_chosen", option_path)
        # The source always emits keywords, including an empty array.  Validate
        # it when present while keeping compatibility with older documented
        # fixtures that omitted optional descriptive fields.
        if "keywords" in option:
            _project_keywords(option["keywords"], f"{option_path}.keywords")
        if locked or chosen:
            continue
        identity = f"{family}:{_quote(event_id)}:{index}"
        if identity in identities:
            _error(
                option_path,
                f"duplicate visible option identity {identity!r}",
                AmbiguousCandidateError,
            )
        identities.add(identity)
        text = _visible_text(
            option,
            "title",
            "description",
            "relic_name",
            "relic_description",
            path=option_path,
        )
        fields = (
            "index",
            "title",
            "description",
            "is_locked",
            "is_proceed",
            "was_chosen",
            "relic_name",
            "relic_description",
            "keywords",
        )
        features = dict(context)
        features.update(_present_features(option, fields, option_path))
        candidates.append(
            _candidate(
                family,
                identity,
                {"action": "choose_event_option", "index": index},
                text,
                features,
            )
        )
    return _finish(family, screen_type, candidates)


def extract_live_candidates(state: Mapping[str, Any]) -> LiveCandidateSet:
    """Extract legal visible candidates from one v0.111.0 live state."""

    current = _unwrap_state(state)
    raw_state_type = current["state_type"]
    state_type = raw_state_type.strip().lower()
    if state_type not in SUPPORTED_SCREEN_TYPES:
        raise UnsupportedScreenError(f"unsupported live screen {state_type!r}")
    if state_type == "map":
        return _map_candidates(current)
    if state_type == "card_reward":
        return _card_reward_candidates(current)
    if state_type == "shop":
        return _shop_candidates(current)
    if state_type == "rest_site":
        return _rest_candidates(current)
    if state_type in {"event", "neow"}:
        return _event_candidates(current, state_type)
    raise UnsupportedScreenError(f"unsupported live screen {state_type!r}")


# Short aliases for training/data-collection call sites.
extract_candidates = extract_live_candidates
build_live_candidate_codec = extract_live_candidates


def decode_live_action(state: Mapping[str, Any], identity: str) -> dict[str, JsonValue]:
    return extract_live_candidates(state).decode(identity)


def encode_live_action(
    state: Mapping[str, Any], wire_action: Mapping[str, Any]
) -> str:
    return extract_live_candidates(state).encode(wire_action)


__all__ = [
    "LIVE_BUILD",
    "CONTRACT_VERSION",
    "SUPPORTED_SCREEN_TYPES",
    "LiveCandidateContractError",
    "UnsupportedScreenError",
    "MissingStateError",
    "EmptyCandidateError",
    "AmbiguousCandidateError",
    "IllegalCandidateError",
    "UnknownScreenError",
    "AmbiguousStateError",
    "InvalidCandidateError",
    "LiveCandidate",
    "LiveCandidateSet",
    "LiveCandidateCodec",
    "extract_live_candidates",
    "extract_candidates",
    "build_live_candidate_codec",
    "decode_live_action",
    "encode_live_action",
]
