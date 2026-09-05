"""Infer actually-executed combat actions from read-only state deltas.

The comparison harness never POSTs to the game, so "what was played" must be
reconstructed from consecutive player-turn states polled over STS2MCP. The
inference is deliberately conservative: when the delta does not determine a
unique action sequence, the turn is flagged ``ambiguous`` and excluded from
route-deviation statistics instead of being guessed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from combat_solver.snapshot import RouteAction


@dataclass(frozen=True)
class ExecutedTurn:
    turn: int
    actions: tuple[RouteAction, ...]
    ambiguous: bool
    notes: tuple[str, ...] = ()
    source: str = "inferred"  # "inferred" from state deltas | "deploy_log"

    def to_json(self) -> dict[str, Any]:
        return {
            "turn": self.turn,
            "actions": [a.to_json() for a in self.actions],
            "ambiguous": self.ambiguous,
            "notes": list(self.notes),
            "source": self.source,
        }


@dataclass
class _Inference:
    actions: list[RouteAction] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    ambiguous: bool = False


def _card_key(card: dict[str, Any]) -> tuple[Any, ...]:
    return (card.get("id"), bool(card.get("is_upgraded")))


def _hand_multiset(state: dict[str, Any]) -> dict[tuple[Any, ...], list[dict[str, Any]]]:
    hand = state.get("player", {}).get("hand", []) or []
    multiset: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for card in hand:
        multiset.setdefault(_card_key(card), []).append(card)
    return multiset


def _enemy_signature(enemy: dict[str, Any]) -> tuple[Any, ...]:
    statuses = sorted(
        (s.get("id") or s.get("name"), s.get("amount"))
        for s in (enemy.get("status") or [])
        if isinstance(s, dict)
    )
    return (enemy.get("hp"), enemy.get("block"), tuple(statuses))


def _cost(card: dict[str, Any]) -> int | None:
    raw = card.get("cost")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def infer_executed_turn(prev: dict[str, Any], nxt: dict[str, Any], turn: int) -> ExecutedTurn:
    """Reconstruct the action sequence between two player-turn monster states.

    ``prev`` and ``next`` are raw STS2MCP singleplayer states captured while
    ``state_type == "monster"``. ``turn`` labels the player turn that just
    finished (the round observed in ``prev``).
    """
    inf = _Inference()
    prev_battle = prev.get("battle") or {}
    next_battle = nxt.get("battle") or {}

    prev_hand = _hand_multiset(prev)
    next_hand = _hand_multiset(nxt)

    played: list[dict[str, Any]] = []
    for key, prev_cards in prev_hand.items():
        next_cards = next_hand.get(key, [])
        if len(next_cards) < len(prev_cards):
            played.extend(prev_cards[: len(prev_cards) - len(next_cards)])

    # Energy accounting: every played card costs energy unless cost is X/None.
    costs = [_cost(card) for card in played]
    known_costs = [c for c in costs if c is not None]
    prev_energy = prev.get("player", {}).get("energy")
    next_energy = nxt.get("player", {}).get("energy")
    if (
        isinstance(prev_energy, int)
        and isinstance(next_energy, int)
        and known_costs
        and prev_energy - sum(known_costs) != next_energy
    ):
        # X-cost cards, energy relics or retention effects can explain a
        # mismatch; keep the sequence but mark it unusable for strict gates.
        inf.ambiguous = True
        inf.notes.append(
            f"energy mismatch: {prev_energy} -> {next_energy} "
            f"with played costs {known_costs}"
        )

    # Potion usage.
    prev_potions = prev.get("player", {}).get("potions") or []
    next_potions = nxt.get("player", {}).get("potions") or []
    if len(next_potions) < len(prev_potions):
        missing = [p for p in prev_potions if p not in next_potions]
        for potion in missing[: len(prev_potions) - len(next_potions)]:
            inf.actions.append(
                RouteAction(kind="potion", card_id=potion.get("id") or potion.get("name"))
            )

    # Card plays, in hand order (the order they left the hand is not exposed;
    # hand order is the stablest available proxy).
    ordered = sorted(
        played, key=lambda c: c.get("index") if isinstance(c.get("index"), int) else 0
    )
    for card in ordered:
        target_index = _infer_target(prev_battle, next_battle, card)
        inf.actions.append(
            RouteAction(kind="play", card_id=card.get("id"), target_index=target_index)
        )

    # End of turn: the round advanced between the two player-turn states.
    prev_round = prev_battle.get("round")
    next_round = next_battle.get("round")
    if isinstance(prev_round, int) and isinstance(next_round, int) and next_round > prev_round:
        inf.actions.append(RouteAction(kind="end_turn"))
    elif not inf.actions:
        inf.notes.append("no state delta observed between player turns")

    return ExecutedTurn(
        turn=turn,
        actions=tuple(inf.actions),
        ambiguous=inf.ambiguous,
        notes=tuple(inf.notes),
    )


def _infer_target(
    prev_battle: dict[str, Any], next_battle: dict[str, Any], card: dict[str, Any]
) -> int | None:
    """Attribute a single-target play to an enemy via its state delta.

    Returns a 0-based enemy index, or None when the card cannot target an
    enemy or the delta does not identify a unique recipient (the caller keeps
    the turn unambiguous only if the route expected a concrete target).
    """
    if card.get("target_type") not in ("AnyEnemy", "Enemy"):
        return None
    prev_enemies = prev_battle.get("enemies") or []
    next_enemies = next_battle.get("enemies") or []
    next_by_id = {
        e.get("entity_id"): e for e in next_enemies if isinstance(e, dict)
    }
    changed: list[int] = []
    for idx, enemy in enumerate(prev_enemies):
        if not isinstance(enemy, dict):
            continue
        counterpart = next_by_id.get(enemy.get("entity_id"))
        if counterpart is None or _enemy_signature(counterpart) != _enemy_signature(enemy):
            changed.append(idx)
    if len(changed) == 1:
        return changed[0]
    return None
