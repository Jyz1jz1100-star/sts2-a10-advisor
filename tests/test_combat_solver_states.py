"""Shared synthetic STS2MCP-shaped states for Combat Solver tests.

The shape mirrors tests/fixtures/screens/monster.json (live v0.4.0 protocol)
with only the fields the session/executed layers consume.
"""
from __future__ import annotations

from typing import Any


def enemy(entity_id: str, hp: int, block: int = 0, status: list | None = None) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "combat_id": entity_id,
        "hp": hp,
        "max_hp": max(hp, 1),
        "block": block,
        "status": status or [],
        "intents": [],
    }


def play_card(
    card_id: str,
    index: int,
    cost: int = 1,
    target: str = "AnyEnemy",
    upgraded: bool = False,
) -> dict[str, Any]:
    return {
        "id": card_id,
        "index": index,
        "cost": str(cost),
        "is_upgraded": upgraded,
        "target_type": target,
        "can_play": True,
    }


def monster_state(
    round_no: int,
    hp: int = 60,
    energy: int = 3,
    hand: list | None = None,
    enemies: list | None = None,
    potions: list | None = None,
    turn: str = "player",
    is_play_phase: bool = True,
    run: dict | None = None,
) -> dict[str, Any]:
    return {
        "state_type": "monster",
        "battle": {
            "round": round_no,
            "turn": turn,
            "is_play_phase": is_play_phase,
            "enemies": enemies if enemies is not None else [enemy("CULTIST_0", hp=48)],
        },
        "player": {
            "hp": hp,
            "energy": energy,
            "hand": hand or [],
            "potions": potions or [],
        },
        "run": run or {"act": 1, "floor": 2, "ascension": 10, "seed": 1_600_000_000},
    }


def non_combat_state(state_type: str = "rewards", hp: int = 52) -> dict[str, Any]:
    return {
        "state_type": state_type,
        "player": {"hp": hp},
        "run": {"act": 1, "floor": 2, "ascension": 10, "seed": 1_600_000_000},
    }
