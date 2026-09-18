"""Bounded, visible-information route planning for live STS2 map states.

The live STS2MCP map contract exposes two useful, deliberately different
views of the map:

* ``map.next_options[*].leads_to`` is a one-level preview of each legal move;
* ``map.nodes`` is the complete visible node/child projection emitted by the
  local ``BuildMapState`` implementation.

This module consumes only those fields.  It never reads a seed, asks the game
for another state, or tries to infer an unseen route.  A complete ``nodes``
view is evaluated with a bounded max-over-branches dynamic program.  Without
that view the planner stops at the one visible ``leads_to`` layer (or at the
current node when no successor is visible).

Scores are the existing live heuristic node scores, split into an immediate
score and a discounted future contribution.  They are useful for deterministic
offline comparison, but they are not combat survival probabilities and do not
claim to estimate run win rate.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeAlias

from .contracts import Candidate, Recommendation
from .live_candidate_codec import (
    AmbiguousCandidateError,
    IllegalCandidateError,
    LiveCandidate,
    LiveCandidateContractError,
    LiveCandidateSet,
    MissingStateError,
    UnsupportedScreenError,
    extract_live_candidates,
)
from .policy_live import _NODE_SCORES, _REST_HEAL_THRESHOLD


Coordinate: TypeAlias = tuple[int, int]

ROUTE_MODEL_ID = "route-lookahead-v1"
DEFAULT_MAX_NODES = 256
DEFAULT_MAX_DEPTH = 4
DEFAULT_FUTURE_DISCOUNT = 0.85
MAX_DEPTH_LIMIT = 64

# Copy the policy constants at import time so callers can inspect the route
# policy's score contract without obtaining a mutable reference to the live
# policy's dictionary.  Values remain exactly the current policy values.
BASELINE_NODE_SCORES: dict[str, tuple[float, float]] = {
    key: (float(value[0]), float(value[1])) for key, value in _NODE_SCORES.items()
}
REST_HEAL_THRESHOLD = float(_REST_HEAL_THRESHOLD)


class RoutePlannerError(LiveCandidateContractError):
    """Base error for a malformed or unusable visible map graph."""


@dataclass(frozen=True, slots=True)
class _VisibleNode:
    coordinate: Coordinate
    node_type: str
    children: tuple[Coordinate, ...]

    @property
    def signature(self) -> tuple[str, tuple[Coordinate, ...]]:
        # Child order is presentation-only in the source projection.  Sorting
        # makes an identical duplicate robust to source list ordering while a
        # different type/edge set remains an ambiguity.
        return (_canonical_node_type(self.node_type), tuple(sorted(self.children)))


@dataclass(frozen=True, slots=True)
class _VisibleGraph:
    nodes: Mapping[Coordinate, _VisibleNode]
    duplicate_count: int = 0


@dataclass(frozen=True, slots=True)
class _RouteValue:
    value: float
    path: tuple[Coordinate, ...]


@dataclass(frozen=True, slots=True)
class _CandidateEvaluation:
    immediate: float
    future: float
    path: tuple[Coordinate, ...]
    known_path_nodes: int

    @property
    def total(self) -> float:
        return self.immediate + self.future


def _state_view(state: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the state object while preserving the codec's envelope support."""

    if not isinstance(state, Mapping):
        raise MissingStateError("state: must be a JSON object")
    if "state_type" in state:
        return state
    nested = state.get("state")
    if isinstance(nested, Mapping):
        return nested
    raise MissingStateError("state.state_type: is required; refusing to infer it")


def _nonnegative_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise IllegalCandidateError(f"{path}: must be a non-negative integer")
    return value


def _required_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MissingStateError(f"{path}: must be a JSON object")
    return value


def _required_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MissingStateError(f"{path}: must be a non-empty string")
    return value.strip()


def _coordinate(value: Any, path: str) -> int:
    return _nonnegative_int(value, path)


def _parse_coordinate(value: Any, path: str) -> Coordinate:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise MissingStateError(f"{path}: must be a [col, row] pair")
    return (
        _coordinate(value[0], f"{path}[0]"),
        _coordinate(value[1], f"{path}[1]"),
    )


def _canonical_node_type(node_type: str) -> str:
    """Map source spellings to the keys used by the existing heuristic."""

    compact = "".join(ch.lower() for ch in node_type if ch.isalnum())
    aliases = {
        "campfire": "campfire",
        "rest": "rest",
        "restsite": "rest",
        "shop": "shop",
        "merchant": "shop",
        "treasure": "treasure",
        "event": "event",
        "monster": "monster",
        "elite": "elite",
        "unknown": "unknown",
        "question": "unknown",
        "questionmark": "unknown",
        "ancient": "ancient",
        "start": "ancient",
        "boss": "boss",
    }
    return aliases.get(compact, "unknown")


def score_node(node_type: str, hp_fraction: float) -> float:
    """Return the current live heuristic score for one visible node type.

    ``hp_fraction`` uses the same strict ``< 75%`` split as
    ``LiveHeuristicPolicy``.  Ancient/start and boss markers are structural
    endpoints, so they contribute zero rather than being treated as an
    unknown encounter.  Unrecognised non-empty source types use the existing
    ``unknown`` bucket and are surfaced as a warning by ``recommend``.
    """

    if not isinstance(node_type, str) or not node_type.strip():
        raise ValueError("node_type must be a non-empty string")
    fraction = float(hp_fraction)
    if not math.isfinite(fraction):
        raise ValueError("hp_fraction must be finite")
    canonical = _canonical_node_type(node_type)
    if canonical in {"ancient", "boss"}:
        return 0.0
    band = BASELINE_NODE_SCORES.get(canonical)
    if band is None:
        band = BASELINE_NODE_SCORES["unknown"]
    return band[0] if fraction < REST_HEAL_THRESHOLD else band[1]


# Friendly alias for callers that name the operation after the visible map
# projection rather than the generic node primitive.
score_visible_node = score_node


def _hp_context(state: Mapping[str, Any]) -> tuple[float, int | None, int | None, str | None]:
    """Read valid HP fields, preserving a conservative baseline fallback."""

    player = state.get("player")
    if not isinstance(player, Mapping):
        return 0.0, None, None, "生命字段缺失，按低血分档保守计算"
    raw_hp = player.get("hp")
    raw_max = player.get("max_hp")
    if (
        isinstance(raw_hp, bool)
        or isinstance(raw_max, bool)
        or not isinstance(raw_hp, (int, float))
        or not isinstance(raw_max, (int, float))
        or not math.isfinite(float(raw_hp))
        or not math.isfinite(float(raw_max))
        or float(raw_max) <= 0
    ):
        return 0.0, None, None, "生命字段不可用，按低血分档保守计算"
    hp = int(raw_hp)
    max_hp = int(raw_max)
    fraction = min(1.0, max(0.0, float(raw_hp) / float(raw_max)))
    return fraction, hp, max_hp, None


def _game_build(original: Mapping[str, Any], state: Mapping[str, Any]) -> str:
    for source in (state, original):
        game = source.get("game")
        if isinstance(game, Mapping):
            build = game.get("build")
            if isinstance(build, str) and build.strip():
                return build.strip()
        build = source.get("build")
        if isinstance(build, str) and build.strip():
            return build.strip()
    return "unknown"


def _parse_graph(map_state: Mapping[str, Any]) -> _VisibleGraph | None:
    """Strictly parse the source's complete ``map.nodes`` projection.

    A missing field means the caller only has the codec's one-level view.  A
    present but malformed field is rejected: silently downgrading malformed
    full topology would make a damaged graph look like a safe route.
    """

    if "nodes" not in map_state:
        return None
    raw_nodes = map_state["nodes"]
    if not isinstance(raw_nodes, list):
        raise MissingStateError("state.map.nodes: must be an array")
    if not raw_nodes:
        # Some transitional states expose an empty container before the map is
        # populated.  It is not a graph; the caller may use already-visible
        # leads_to values, but receives an explicit mode warning.
        return None

    parsed: dict[Coordinate, _VisibleNode] = {}
    duplicate_count = 0
    for number, raw in enumerate(raw_nodes):
        path = f"state.map.nodes[{number}]"
        node = _required_mapping(raw, path)
        coordinate = (
            _coordinate(node.get("col"), f"{path}.col"),
            _coordinate(node.get("row"), f"{path}.row"),
        )
        node_type = _required_string(node.get("type"), f"{path}.type")
        if "children" not in node:
            raise MissingStateError(f"{path}.children: is required")
        raw_children = node["children"]
        if not isinstance(raw_children, list):
            raise MissingStateError(f"{path}.children: must be an array")
        children: list[Coordinate] = []
        for child_number, raw_child in enumerate(raw_children):
            children.append(
                _parse_coordinate(raw_child, f"{path}.children[{child_number}]")
            )
        # An identical edge repeated in one source node cannot create a new
        # route; collapse it before DP, preserving deterministic coordinates.
        unique_children = tuple(sorted(set(children)))
        candidate = _VisibleNode(coordinate, node_type, unique_children)
        previous = parsed.get(coordinate)
        if previous is None:
            parsed[coordinate] = candidate
        elif previous.signature == candidate.signature:
            duplicate_count += 1
        else:
            raise AmbiguousCandidateError(
                f"{path}: conflicting duplicate map node at {coordinate}"
            )
    return _VisibleGraph(parsed, duplicate_count)


def _candidate_coordinate(candidate: LiveCandidate) -> Coordinate:
    features = candidate.features
    try:
        col = _coordinate(features.get("col"), f"candidate[{candidate.identity}].col")
        row = _coordinate(features.get("row"), f"candidate[{candidate.identity}].row")
    except IllegalCandidateError:
        raise
    return col, row


def _candidate_node_type(candidate: LiveCandidate) -> str:
    features = candidate.features
    return _required_string(features.get("type"), f"candidate[{candidate.identity}].type")


def _candidate_children(
    candidate: LiveCandidate,
) -> tuple[tuple[Coordinate, str], ...]:
    """Validate and deduplicate the one-level ``leads_to`` projection.

    A coordinate is the source's node identity.  Seeing the same coordinate
    with two different types is therefore an ambiguous state, and a child
    pointing back to the selected node is a malformed self-cycle.  Identical
    repeated entries are harmless presentation duplication and collapse to one
    visible child.
    """

    root = _candidate_coordinate(candidate)
    raw_children = candidate.features.get("leads_to", [])
    if not isinstance(raw_children, list):
        raise MissingStateError(
            f"candidate[{candidate.identity}].leads_to: must be an array"
        )
    by_coordinate: dict[Coordinate, str] = {}
    for number, child in enumerate(raw_children):
        child_map = _required_mapping(
            child, f"candidate[{candidate.identity}].leads_to[{number}]"
        )
        coordinate = (
            _coordinate(child_map.get("col"), "lookahead.col"),
            _coordinate(child_map.get("row"), "lookahead.row"),
        )
        if coordinate == root:
            raise RoutePlannerError(
                f"candidate[{candidate.identity}].leads_to[{number}]: self-cycle at {root}"
            )
        node_type = _required_string(child_map.get("type"), "lookahead.type")
        previous = by_coordinate.get(coordinate)
        if previous is not None and _canonical_node_type(previous) != _canonical_node_type(
            node_type
        ):
            raise AmbiguousCandidateError(
                f"candidate[{candidate.identity}].leads_to: coordinate {coordinate} has conflicting types"
            )
        by_coordinate.setdefault(coordinate, node_type)
    return tuple(
        sorted(
            by_coordinate.items(),
            key=lambda item: _stable_child_key(item[0], item[1]),
        )
    )


def _validate_one_level_consistency(candidates: Sequence[LiveCandidate]) -> None:
    """Reject contradictory child types across the visible option list."""

    by_coordinate: dict[Coordinate, str] = {}
    for candidate in candidates:
        for coordinate, node_type in _candidate_children(candidate):
            previous = by_coordinate.get(coordinate)
            if previous is not None and _canonical_node_type(previous) != _canonical_node_type(
                node_type
            ):
                raise AmbiguousCandidateError(
                    f"leads_to: coordinate {coordinate} has conflicting visible types"
                )
            by_coordinate.setdefault(coordinate, node_type)


def _stable_child_key(coordinate: Coordinate, node_type: str = "") -> tuple[int, int, str]:
    return (coordinate[0], coordinate[1], _canonical_node_type(node_type))


def _reachable_nodes(
    graph: _VisibleGraph, roots: Sequence[Coordinate], depth: int
) -> set[Coordinate]:
    """Return the common visible search footprint up to ``depth`` edges."""

    seen: set[Coordinate] = set()
    frontier = {root for root in roots if root in graph.nodes}
    for _ in range(depth + 1):
        fresh = frontier - seen
        if not fresh:
            break
        seen.update(fresh)
        frontier = {
            child
            for coordinate in fresh
            for child in graph.nodes[coordinate].children
            if child not in seen and child in graph.nodes
        }
    return seen


def _validate_full_graph_roots(
    candidates: Sequence[LiveCandidate], graph: _VisibleGraph
) -> None:
    """Ensure the legal option projection agrees with the full graph roots."""

    for candidate in candidates:
        coordinate = _candidate_coordinate(candidate)
        node = graph.nodes.get(coordinate)
        if node is not None and _canonical_node_type(node.node_type) != _canonical_node_type(
            _candidate_node_type(candidate)
        ):
            raise AmbiguousCandidateError(
                f"map: candidate {coordinate} type disagrees with map.nodes"
            )
        projected_children = _candidate_children(candidate)
        if node is None:
            continue
        graph_children = {
            child: graph.nodes[child].node_type
            for child in node.children
            if child in graph.nodes
        }
        for child_coordinate, child_type in projected_children:
            graph_type = graph_children.get(child_coordinate)
            if graph_type is not None and _canonical_node_type(graph_type) != _canonical_node_type(
                child_type
            ):
                raise AmbiguousCandidateError(
                    f"map: leads_to coordinate {child_coordinate} type disagrees with map.nodes"
                )


def _validate_reachable_acyclic(
    graph: _VisibleGraph, roots: Sequence[Coordinate]
) -> None:
    """Reject visible cycles instead of assigning path-dependent memo values."""

    visiting: set[Coordinate] = set()
    visited: set[Coordinate] = set()

    def visit(coordinate: Coordinate) -> None:
        if coordinate in visiting:
            raise RoutePlannerError(
                f"map.nodes: reachable cycle detected at coordinate {coordinate}"
            )
        if coordinate in visited:
            return
        node = graph.nodes.get(coordinate)
        if node is None:
            return
        visiting.add(coordinate)
        try:
            for child in sorted(node.children):
                visit(child)
        finally:
            visiting.remove(coordinate)
        visited.add(coordinate)

    for root in sorted(set(roots)):
        visit(root)


def _common_full_depth(
    graph: _VisibleGraph,
    roots: Sequence[Coordinate],
    max_depth: int,
    max_nodes: int,
) -> int:
    """Choose one depth for every candidate under the shared node budget."""

    depth = 0
    for candidate_depth in range(1, max_depth + 1):
        required = len(_reachable_nodes(graph, roots, candidate_depth))
        if required > max_nodes:
            break
        depth = candidate_depth
    return depth


def _common_one_level_depth(
    candidates: Sequence[LiveCandidate], max_depth: int, max_nodes: int
) -> int:
    if max_depth < 1 or max_nodes <= 0:
        return 0
    visible_children: set[Coordinate] = set()
    for candidate in candidates:
        visible_children.update(
            coordinate for coordinate, _ in _candidate_children(candidate)
        )
    return 1 if visible_children and len(visible_children) <= max_nodes else 0


class _FullGraphEvaluator:
    def __init__(
        self,
        graph: _VisibleGraph,
        hp_fraction: float,
        discount: float,
        depth: int,
    ) -> None:
        self.graph = graph
        self.hp_fraction = hp_fraction
        self.discount = discount
        self.depth = depth
        self.memo: dict[tuple[Coordinate, int], _RouteValue] = {}
        self.active: set[Coordinate] = set()
        self.expanded: set[Coordinate] = set()
        self.missing_refs: set[Coordinate] = set()
        self.cycles: set[Coordinate] = set()
        self.unknown_types: set[str] = set()

    def _node_score(self, node: _VisibleNode) -> float:
        canonical = _canonical_node_type(node.node_type)
        if canonical == "unknown" and node.node_type.strip().lower() not in {
            "unknown",
            "?",
            "question",
            "questionmark",
        }:
            self.unknown_types.add(node.node_type)
        return score_node(node.node_type, self.hp_fraction)

    def value(self, coordinate: Coordinate, remaining_depth: int) -> _RouteValue:
        node = self.graph.nodes.get(coordinate)
        if node is None:
            self.missing_refs.add(coordinate)
            return _RouteValue(0.0, ())
        if coordinate in self.active:
            self.cycles.add(coordinate)
            return _RouteValue(0.0, ())
        key = (coordinate, remaining_depth)
        cached = self.memo.get(key)
        if cached is not None:
            return cached

        self.expanded.add(coordinate)
        self.active.add(coordinate)
        try:
            own = self._node_score(node)
            best_child: _RouteValue | None = None
            if remaining_depth > 0:
                for child in sorted(
                    node.children,
                    key=lambda child_coord: _stable_child_key(
                        child_coord,
                        self.graph.nodes[child_coord].node_type
                        if child_coord in self.graph.nodes
                        else "",
                    ),
                ):
                    child_value = self.value(child, remaining_depth - 1)
                    if not child_value.path and child_value.value == 0.0:
                        # Missing/cyclic references have no visible node to
                        # render and must not create an accidental tie bonus.
                        continue
                    if best_child is None or (
                        child_value.value > best_child.value
                        or (
                            child_value.value == best_child.value
                            and child_value.path < best_child.path
                        )
                    ):
                        best_child = child_value
            if best_child is None:
                result = _RouteValue(own, (coordinate,))
            else:
                result = _RouteValue(
                    own + self.discount * best_child.value,
                    (coordinate,) + best_child.path,
                )
            self.memo[key] = result
            return result
        finally:
            self.active.remove(coordinate)


class _OneLevelEvaluator:
    def __init__(self, hp_fraction: float, discount: float) -> None:
        self.hp_fraction = hp_fraction
        self.discount = discount
        self.expanded: set[Coordinate] = set()
        self.unknown_types: set[str] = set()

    def _score(self, node_type: str) -> float:
        if _canonical_node_type(node_type) == "unknown" and node_type.strip().lower() not in {
            "unknown",
            "?",
            "question",
            "questionmark",
        }:
            self.unknown_types.add(node_type)
        return score_node(node_type, self.hp_fraction)

    def value(self, candidate: LiveCandidate, depth: int) -> _CandidateEvaluation:
        coordinate = _candidate_coordinate(candidate)
        node_type = _candidate_node_type(candidate)
        immediate = self._score(node_type)
        if depth < 1:
            return _CandidateEvaluation(immediate, 0.0, (coordinate,), 1)

        raw_children = candidate.features.get("leads_to", [])
        if not raw_children:
            return _CandidateEvaluation(immediate, 0.0, (coordinate,), 1)
        children: list[tuple[Coordinate, str]] = []
        for number, child in enumerate(raw_children):
            child_map = _required_mapping(
                child, f"candidate[{candidate.identity}].leads_to[{number}]"
            )
            child_coordinate = (
                _coordinate(child_map.get("col"), "lookahead.col"),
                _coordinate(child_map.get("row"), "lookahead.row"),
            )
            child_type = _required_string(child_map.get("type"), "lookahead.type")
            children.append((child_coordinate, child_type))
        children.sort(key=lambda item: _stable_child_key(item[0], item[1]))
        best_coordinate, best_type = children[0]
        self.expanded.add(best_coordinate)
        best_score = self._score(best_type)
        for child_coordinate, child_type in children[1:]:
            self.expanded.add(child_coordinate)
            child_score = self._score(child_type)
            if child_score > best_score or (
                child_score == best_score
                and _stable_child_key(child_coordinate, child_type)
                < _stable_child_key(best_coordinate, best_type)
            ):
                best_coordinate, best_type, best_score = (
                    child_coordinate,
                    child_type,
                    child_score,
                )
        return _CandidateEvaluation(
            immediate,
            self.discount * best_score,
            (coordinate, best_coordinate),
            2,
        )


class RoutePlannerPolicy:
    """A map-only, bounded route lookahead policy.

    ``max_depth`` counts visible future node layers after the selected
    immediate node.  ``max_nodes`` is a shared budget: the planner first
    finds one depth that all candidates can support, then evaluates every
    candidate at that same depth.  This avoids ranking one option with a deep
    search against another option with a truncated search.
    """

    model_id = ROUTE_MODEL_ID

    def __init__(
        self,
        *,
        max_nodes: int = DEFAULT_MAX_NODES,
        max_depth: int = DEFAULT_MAX_DEPTH,
        future_discount: float = DEFAULT_FUTURE_DISCOUNT,
    ) -> None:
        if isinstance(max_nodes, bool) or not isinstance(max_nodes, int) or max_nodes < 0:
            raise ValueError("max_nodes must be a non-negative integer")
        if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 0:
            raise ValueError("max_depth must be a non-negative integer")
        if max_depth > MAX_DEPTH_LIMIT:
            raise ValueError(f"max_depth must be <= {MAX_DEPTH_LIMIT}")
        if isinstance(future_discount, bool) or not isinstance(
            future_discount, (int, float)
        ):
            raise ValueError("future_discount must be a finite number in [0, 1]")
        if not math.isfinite(float(future_discount)) or not 0.0 <= float(future_discount) <= 1.0:
            raise ValueError("future_discount must be a finite number in [0, 1]")
        self.max_nodes = max_nodes
        self.max_depth = max_depth
        self.future_discount = float(future_discount)

    @staticmethod
    def candidates(state: Mapping[str, Any]) -> LiveCandidateSet:
        """Return the exact legal map candidates without selecting one."""

        candidate_set = extract_live_candidates(state)
        if candidate_set.family != "map":
            raise UnsupportedScreenError("route planner only supports map states")
        return candidate_set

    live_candidates = candidates

    @staticmethod
    def wire_action_for(
        state: Mapping[str, Any], action: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Resolve an internal map action to an exact current wire action."""

        candidate_set = RoutePlannerPolicy.candidates(state)
        if not isinstance(action, Mapping) or action.get("type") != "map_choose_node":
            raise IllegalCandidateError("route action must be map_choose_node")
        if "index" not in action:
            raise IllegalCandidateError("route action is missing index")
        index = _nonnegative_int(action["index"], "route action.index")
        wire = {"action": "choose_map_node", "index": index}
        candidate_set.identity_for(wire)
        return wire

    def _full_graph_plan(
        self,
        candidates: Sequence[LiveCandidate],
        graph: _VisibleGraph,
        hp_fraction: float,
    ) -> tuple[list[_CandidateEvaluation], _FullGraphEvaluator, int, bool]:
        roots = [_candidate_coordinate(candidate) for candidate in candidates]
        depth = _common_full_depth(
            graph, roots, self.max_depth, self.max_nodes
        )
        evaluator = _FullGraphEvaluator(
            graph, hp_fraction, self.future_discount, depth
        )
        evaluations: list[_CandidateEvaluation] = []
        for candidate in candidates:
            coordinate = _candidate_coordinate(candidate)
            node_type = _candidate_node_type(candidate)
            canonical = _canonical_node_type(node_type)
            if canonical == "unknown" and node_type.strip().lower() not in {
                "unknown",
                "?",
                "question",
                "questionmark",
            }:
                evaluator.unknown_types.add(node_type)
            immediate = score_node(node_type, hp_fraction)
            future = 0.0
            path = (coordinate,)
            if depth > 0:
                root = graph.nodes.get(coordinate)
                if root is None:
                    evaluator.missing_refs.add(coordinate)
                else:
                    # The selected candidate is an implicit root of the
                    # future search.  Mark it active while expanding children
                    # so a malformed cycle back to this root is cut in the
                    # same way as a cycle discovered below a child.
                    evaluator.active.add(coordinate)
                    try:
                        best_child: _RouteValue | None = None
                        for child in sorted(
                            root.children,
                            key=lambda child_coord: _stable_child_key(
                                child_coord,
                                graph.nodes[child_coord].node_type
                                if child_coord in graph.nodes
                                else "",
                            ),
                        ):
                            child_value = evaluator.value(child, depth - 1)
                            if not child_value.path and child_value.value == 0.0:
                                continue
                            if best_child is None or (
                                child_value.value > best_child.value
                                or (
                                    child_value.value == best_child.value
                                    and child_value.path < best_child.path
                                )
                            ):
                                best_child = child_value
                    finally:
                        evaluator.active.remove(coordinate)
                    if best_child is not None:
                        future = self.future_discount * best_child.value
                        path = (coordinate,) + best_child.path
            evaluations.append(
                _CandidateEvaluation(
                    immediate,
                    future,
                    path,
                    len(path),
                )
            )
        return evaluations, evaluator, depth, True

    def _one_level_plan(
        self,
        candidates: Sequence[LiveCandidate],
        hp_fraction: float,
    ) -> tuple[list[_CandidateEvaluation], _OneLevelEvaluator, int, bool]:
        depth = _common_one_level_depth(candidates, self.max_depth, self.max_nodes)
        evaluator = _OneLevelEvaluator(hp_fraction, self.future_discount)
        evaluations = [evaluator.value(candidate, depth) for candidate in candidates]
        return evaluations, evaluator, depth, False

    @staticmethod
    def _path_text(path: tuple[Coordinate, ...], types: Mapping[Coordinate, str]) -> str:
        labels = []
        for coordinate in path:
            node_type = types.get(coordinate, "未知")
            labels.append(f"{node_type}@({coordinate[0]},{coordinate[1]})")
        return " → ".join(labels)

    def recommend(self, state: Mapping[str, Any]) -> Recommendation:
        original = state
        state_view = _state_view(state)
        state_type = str(state_view.get("state_type") or "unknown").strip().lower()
        if state_type != "map":
            raise UnsupportedScreenError(
                f"route planner only supports state_type='map', got {state_type!r}"
            )

        # Candidate extraction precedes graph parsing.  It proves the current
        # actions and their source indices before any heuristic is run.
        candidate_set = self.candidates(state)
        candidates = tuple(candidate_set)
        map_state = _required_mapping(state_view.get("map"), "state.map")
        hp_fraction, hp, max_hp, hp_warning = _hp_context(state_view)
        graph = _parse_graph(map_state)
        graph_was_empty = "nodes" in map_state and graph is None

        if graph is None:
            _validate_one_level_consistency(candidates)
            evaluations, evaluator, depth, full = self._one_level_plan(
                candidates, hp_fraction
            )
        else:
            _validate_full_graph_roots(candidates, graph)
            _validate_reachable_acyclic(
                graph, [_candidate_coordinate(candidate) for candidate in candidates]
            )
            evaluations, evaluator, depth, full = self._full_graph_plan(
                candidates, graph, hp_fraction
            )

        # Build a visible type map only from the graph/candidate projections.
        # It is used for explanation text, never for adding candidate actions.
        type_by_coordinate: dict[Coordinate, str] = {}
        for candidate in candidates:
            type_by_coordinate[_candidate_coordinate(candidate)] = _candidate_node_type(
                candidate
            )
        if graph is not None:
            type_by_coordinate.update(
                {coordinate: node.node_type for coordinate, node in graph.nodes.items()}
            )
        if not full:
            for candidate in candidates:
                raw_children = candidate.features.get("leads_to", [])
                if isinstance(raw_children, list):
                    for child in raw_children:
                        if isinstance(child, Mapping):
                            child_coordinate = (
                                child.get("col"),
                                child.get("row"),
                            )
                            if all(isinstance(value, int) and not isinstance(value, bool) for value in child_coordinate):
                                child_type = child.get("type")
                                if isinstance(child_type, str) and child_type.strip():
                                    type_by_coordinate[child_coordinate] = child_type.strip()

        ranked_indices = sorted(
            range(len(candidates)),
            key=lambda index: (
                -evaluations[index].total,
                candidates[index].features.get("index", 0),
                _candidate_coordinate(candidates[index]),
                _canonical_node_type(_candidate_node_type(candidates[index])),
            ),
        )

        mode = "完整 map.nodes 图" if full else "next_options.leads_to 一层"
        base_warnings = [
            f"路线前瞻证据：{mode}；所有候选共用前瞻深度 {depth}。",
            "分数是节点类型启发式，不是战斗生存率、通关率或全局最优证明。",
            "未来分只使用当前可见节点；未模拟战斗掉血、卡组变化、商店收益或未来 RNG。",
        ]
        if not full:
            if graph_was_empty:
                base_warnings.append(
                    "map.nodes 为空，未把图缺失当作安全；仅使用已公开的 leads_to 一层。"
                )
            elif any("leads_to" not in candidate.features for candidate in candidates):
                base_warnings.append(
                    "完整 map.nodes 不可见且部分候选没有 leads_to；缺失后继的未来分按 0 计。"
                )
            else:
                base_warnings.append(
                    "完整 map.nodes 不可见，仅按每个候选已公开的 leads_to 一层前瞻。"
                )
        budget_limited = False
        if full and graph is not None and depth < self.max_depth:
            roots = [_candidate_coordinate(candidate) for candidate in candidates]
            budget_limited = (
                len(_reachable_nodes(graph, roots, self.max_depth)) > self.max_nodes
            )
        elif not full and depth < 1:
            budget_limited = (
                len(
                    {
                        child
                        for candidate in candidates
                        for child in _candidate_children(candidate)
                    }
                )
                > self.max_nodes
            )
        if budget_limited:
            base_warnings.append(
                f"搜索预算 max_nodes={self.max_nodes} 只允许统一深度 {depth}，"
                "未把不同深度的截断分数混在一起比较。"
            )
        elif not full and self.max_depth > 1:
            base_warnings.append(
                "当前 live codec 只公开 leads_to 一层；未从一层投影推断更深路线。"
            )
        if graph is not None and graph.duplicate_count:
            base_warnings.append(
                f"完整图中去除了 {graph.duplicate_count} 个完全相同的重复坐标节点。"
            )
        if hp_warning:
            base_warnings.append(hp_warning)
        if isinstance(evaluator, _FullGraphEvaluator):
            if evaluator.missing_refs:
                refs = ", ".join(
                    f"({col},{row})" for col, row in sorted(evaluator.missing_refs)
                )
                base_warnings.append(
                    f"可见图引用了缺失坐标 {refs}；这些边未计入未来分，也未视为安全。"
                )
            if evaluator.cycles:
                base_warnings.append(
                    "检测到地图循环；循环边停止展开，不重复计分。"
                )
            if evaluator.unknown_types:
                base_warnings.append(
                    "存在未识别的节点类型，按现有 Unknown 分档并保留启发式警告。"
                )
            descendant_owners: Counter[Coordinate] = Counter()
            roots = [_candidate_coordinate(candidate) for candidate in candidates]
            for root in roots:
                for coordinate in _reachable_nodes(graph, (root,), depth):
                    if coordinate != root:
                        descendant_owners[coordinate] += 1
            if any(count > 1 for count in descendant_owners.values()):
                base_warnings.append(
                    "多条候选路线在可见图中合流；每条实际路线按合流节点计一次。"
                )
        elif evaluator.unknown_types:
            base_warnings.append(
                "存在未识别的后继节点类型，按现有 Unknown 分档并保留启发式警告。"
            )

        def build_candidate(index: int) -> Candidate:
            source = candidates[index]
            evaluation = evaluations[index]
            coordinate = _candidate_coordinate(source)
            node_type = _candidate_node_type(source)
            internal_action = {
                "type": "map_choose_node",
                "index": source.features["index"],
            }
            # Rebind against the exact current wire set.  This deliberately
            # preserves non-contiguous/source indices and rejects stale actions.
            wire = {"action": "choose_map_node", "index": source.features["index"]}
            candidate_set.identity_for(wire)
            if hp is None or max_hp is None:
                hp_fact = "生命未知"
            else:
                hp_fact = f"生命 {hp}/{max_hp}（{hp_fraction:.0%}）"
            facts = (
                f"即时节点分 {evaluation.immediate:.2f}（{node_type}；{hp_fact}）",
                f"可见未来分 {evaluation.future:.2f}（折扣后，前瞻深度 {depth}）",
                f"路线总分 {evaluation.total:.2f} = 即时 {evaluation.immediate:.2f} + 未来 {evaluation.future:.2f}",
                "前瞻路径（可见）："
                + self._path_text(evaluation.path, type_by_coordinate),
            )
            return Candidate(
                action=internal_action,
                label=f"路线 → {node_type}（{coordinate[0]},{coordinate[1]}）",
                score=evaluation.total,
                confidence=0.0,
                facts=facts,
                metrics={
                    "immediate_score": float(evaluation.immediate),
                    "future_score": float(evaluation.future),
                    "total_score": float(evaluation.total),
                    "lookahead_depth": float(depth),
                    "known_path_nodes": float(evaluation.known_path_nodes),
                },
            )

        ranked = [build_candidate(index) for index in ranked_indices]
        search_nodes = len(evaluator.expanded)
        return Recommendation(
            phase="map",
            primary=ranked[0],
            alternatives=tuple(ranked[1:3]),
            model_id=self.model_id,
            game_build=_game_build(original, state_view),
            search_nodes=search_nodes,
            warnings=tuple(base_warnings),
        )


__all__ = [
    "BASELINE_NODE_SCORES",
    "DEFAULT_FUTURE_DISCOUNT",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_NODES",
    "REST_HEAL_THRESHOLD",
    "ROUTE_MODEL_ID",
    "RoutePlannerError",
    "RoutePlannerPolicy",
    "score_node",
    "score_visible_node",
]
