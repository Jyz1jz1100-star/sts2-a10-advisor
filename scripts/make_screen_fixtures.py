"""Generate screen-schema fixtures for the bridge and the converter.

Fixtures under ``tests/fixtures/screens/`` are the canonical state shapes the
STS2MCP v0.4.0 protocol exposes per screen. They serve two gates:

1. ``tests/test_screen_fixtures.py`` runs the converter's legal-action
   enumerator and visible-state filter over every fixture, pinning the
   protocol shape and the action-id surface for each reachable screen.
2. Real-game re-captures can be diffed against them after a game patch to
   detect bridge schema drift.

Captured fixtures come from version-locked ``runs/live_traces/*.jsonl`` (build
v0.111.0 / assembly 222455745 / STS2MCP 0.4.0). Synthetic fixtures encode the
documented protocol of screens not yet reached by a clean capture
(https://github.com/Gennadiyev/STS2MCP docs/raw-full.md) and are marked
``"captured": false`` until a real trace replaces them.

Regenerate/capture:

    python scripts/make_screen_fixtures.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "screens"
LIVE_TRACES = PROJECT_ROOT / "runs" / "live_traces"

BUILD = "public-beta-v0.111.0"

# Captured: (source trace file, state_type) -> fixture name
CAPTURED_SOURCES: dict[str, tuple[str, str]] = {
    "monster.json": ("clean-a10-combat-bash-heuristic.jsonl", "monster"),
    "map.json": ("clean-a10-path-left-heuristic.jsonl", "map"),
    "event.json": ("clean-a10-neow-scissors-heuristic.jsonl", "event"),
    "card_select.json": ("clean-a10-remove-strike-heuristic.jsonl", "card_select"),
    "menu_main.json": ("clean-ironclad-a10-bootstrap.jsonl", "menu"),
}


def _load_first_state(trace: Path, state_type: str) -> dict[str, Any]:
    with trace.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            event = json.loads(line)
            if event.get("event_type") != "state":
                continue
            raw = event.get("raw")
            if isinstance(raw, dict) and raw.get("state_type") == state_type:
                return raw
    raise LookupError(f"no {state_type} state in {trace}")


def captured_fixtures() -> dict[str, dict[str, Any]]:
    fixtures: dict[str, dict[str, Any]] = {}
    for name, (trace_name, state_type) in CAPTURED_SOURCES.items():
        trace = LIVE_TRACES / trace_name
        if not trace.is_file():
            print(f"skip {name}: missing {trace}")
            continue
        state = _load_first_state(trace, state_type)
        screen = state_type
        if name == "menu_main.json":
            screen = "menu_main"
        fixtures[name] = {
            "screen": screen,
            "captured": True,
            "build": BUILD,
            "source": f"runs/live_traces/{trace_name}",
            "state": state,
        }
    return fixtures


def _player(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "block": 0,
        "character": "铁甲战士",
        "character_id": "IRONCLAD",
        "discard_pile": [],
        "discard_pile_count": 0,
        "draw_pile": [],
        "draw_pile_count": 4,
        "energy": 3,
        "exhaust_pile": [],
        "exhaust_pile_count": 0,
        "gold": 75,
        "hand": [],
        "hp": 72,
        "max_energy": 3,
        "max_hp": 80,
        "max_potion_slots": 3,
        "potions": [],
        "relics": [
            {
                "counter": None,
                "description": "在战斗结束时获得6点防御。",
                "id": "BURNING_BLOOD",
                "keywords": [],
                "name": "燃烧之血",
            }
        ],
        "status": [],
    }
    base.update(overrides)
    return base


def _run(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"act": 1, "ascension": 10, "floor": 5}
    base.update(overrides)
    return base


def synthetic_fixtures() -> dict[str, dict[str, Any]]:
    """Documented protocol shapes for screens without a clean capture yet."""

    card = {
        "index": 0,
        "id": "STRIKE_IRONCLAD",
        "name": "打击",
        "type": "Attack",
        "cost": "1",
        "star_cost": None,
        "description": "造成6点伤害。",
        "is_upgraded": False,
        "keywords": [],
    }
    card2 = {**card, "index": 1, "id": "DEFEND_IRONCLAD", "name": "防御", "type": "Skill",
             "description": "获得5点格挡。"}
    hand_card = {**card, "can_play": True, "target_type": "AnyEnemy", "rarity": "Basic",
                 "unplayable_reason": None}

    fixtures: dict[str, dict[str, Any]] = {}

    def add(name: str, screen: str, state: dict[str, Any]) -> None:
        fixtures[name] = {
            "screen": screen,
            "captured": False,
            "build": BUILD,
            "source": "docs/raw-full.md (STS2MCP v0.4.0 protocol)",
            "state": state,
        }

    add(
        "elite.json",
        "elite",
        {
            "state_type": "elite",
            "battle": {
                "round": 2,
                "turn": "player",
                "is_play_phase": True,
                "enemies": [
                    {
                        "entity_id": "CULTIST_0",
                        "combat_id": 1,
                        "name": "拜教者",
                        "hp": 44,
                        "max_hp": 48,
                        "block": 0,
                        "status": [],
                        "intents": [
                            {"type": "Attack", "label": "12", "title": "攻击",
                             "description": "造成12点伤害。"}
                        ],
                    }
                ],
            },
            "run": _run(floor=8),
            "player": _player(
                energy=2,
                hand=[hand_card, {**hand_card, "index": 1, "id": "DEFEND_IRONCLAD",
                                  "name": "防御", "target_type": "Self"}],
                draw_pile=[card, card2],
                draw_pile_count=2,
            ),
        },
    )
    add(
        "boss.json",
        "boss",
        {
            "state_type": "boss",
            "battle": {
                "round": 3,
                "turn": "player",
                "is_play_phase": True,
                "enemies": [
                    {
                        "entity_id": "BOSS_0",
                        "combat_id": 1,
                        "name": "首脑",
                        "hp": 120,
                        "max_hp": 250,
                        "block": 8,
                        "status": [{"id": "STRENGTH", "name": "力量", "stacks": 2}],
                        "intents": [
                            {"type": "Attack", "label": "2x13", "title": "攻击",
                             "description": "造成2次13点伤害。"}
                        ],
                    }
                ],
            },
            "run": _run(floor=16, act=1),
            "player": _player(
                hp=41,
                hand=[hand_card],
                potions=[{"slot": 0, "id": "FIRE_POTION", "name": "火焰药水",
                          "target_type": "AnyEnemy", "can_use": True}],
            ),
        },
    )
    add(
        "hand_select.json",
        "hand_select",
        {
            "state_type": "hand_select",
            "hand_select": {
                "mode": "simple_select",
                "prompt": "选择一张牌消耗。",
                "cards": [card, card2],
                "can_confirm": True,
            },
            "battle": {
                "round": 1,
                "turn": "player",
                "is_play_phase": True,
                "enemies": [],
            },
            "run": _run(),
            "player": _player(hand=[hand_card, {**hand_card, "index": 1,
                                                "id": "DEFEND_IRONCLAD"}]),
        },
    )
    add(
        "rewards.json",
        "rewards",
        {
            "state_type": "rewards",
            "rewards": {
                "items": [
                    {"index": 0, "type": "gold", "description": "获得25金币。",
                     "gold_amount": 25},
                    {"index": 1, "type": "potion", "description": "获得一瓶药水。",
                     "potion_id": "SWIFT_POTION", "potion_name": "迅捷药水"},
                    {"index": 2, "type": "card", "description": "将一张牌加入牌组。"},
                ],
                "can_proceed": False,
            },
            "run": _run(),
            "player": _player(),
        },
    )
    add(
        "card_reward.json",
        "card_reward",
        {
            "state_type": "card_reward",
            "card_reward": {
                "cards": [
                    {**card, "index": 0, "id": "UPPERCUT", "name": "上勾拳",
                     "rarity": "Uncommon",
                     "description": "造成13点伤害。施加1虚弱。施加1易伤。"},
                    {**card, "index": 1, "id": "IRON_WIND", "name": "铁风",
                     "rarity": "Rare"},
                ],
                "can_skip": True,
            },
            "run": _run(),
            "player": _player(),
        },
    )
    add(
        "rest_site.json",
        "rest_site",
        {
            "state_type": "rest_site",
            "rest_site": {
                "options": [
                    {"index": 0, "id": "rest", "name": "休息",
                     "description": "恢复30%最大生命值。", "is_enabled": True},
                    {"index": 1, "id": "smith", "name": "锻造",
                     "description": "升级一张牌。", "is_enabled": True},
                ],
                "can_proceed": False,
            },
            "run": _run(floor=14),
            "player": _player(hp=39),
        },
    )
    add(
        "shop.json",
        "shop",
        {
            "state_type": "shop",
            "shop": {
                "items": [
                    {"index": 0, "category": "card", "price": 75, "is_stocked": True,
                     "can_afford": True, "on_sale": False, "card_id": "OFFERING",
                     "card_name": "献祭", "card_type": "Skill", "card_cost": "1",
                     "card_rarity": "Rare"},
                    {"index": 5, "category": "relic", "price": 150, "is_stocked": True,
                     "can_afford": False, "relic_id": "VAJRA", "relic_name": "金刚杵"},
                    {"index": 8, "category": "potion", "price": 50, "is_stocked": True,
                     "can_afford": True, "potion_id": "FIRE_POTION",
                     "potion_name": "火焰药水"},
                    {"index": 10, "category": "card_removal", "price": 75,
                     "is_stocked": False, "can_afford": True},
                ],
                "can_proceed": True,
            },
            "run": _run(),
            "player": _player(gold=120),
        },
    )
    add(
        "fake_merchant.json",
        "fake_merchant",
        {
            "state_type": "fake_merchant",
            "fake_merchant": {
                "event_id": "FAKE_MERCHANT",
                "event_name": "假商人",
                "started_fight": False,
                "shop": {
                    "items": [
                        {"index": 0, "category": "relic", "cost": 150,
                         "is_stocked": True, "can_afford": True,
                         "relic_id": "VAJRA", "relic_name": "金刚杵"},
                    ],
                    "can_proceed": True,
                },
            },
            "run": _run(),
            "player": _player(),
        },
    )
    add(
        "treasure.json",
        "treasure",
        {
            "state_type": "treasure",
            "treasure": {
                "relics": [
                    {"index": 0, "id": "LANTERN", "name": "提灯",
                     "description": "每场战斗的第一回合获得1点能量。",
                     "rarity": "Uncommon", "keywords": []},
                ],
                "can_proceed": True,
            },
            "run": _run(floor=6),
            "player": _player(),
        },
    )
    add(
        "bundle_select.json",
        "bundle_select",
        {
            "state_type": "bundle_select",
            "bundle_select": {
                "screen_type": "bundle",
                "prompt": "选择一个卡组。",
                "bundles": [
                    {"index": 0, "card_count": 3, "cards": [card]},
                    {"index": 1, "card_count": 2, "cards": [card2]},
                ],
                "preview_showing": False,
                "can_cancel": False,
                "can_confirm": False,
            },
            "run": _run(),
            "player": _player(),
        },
    )
    add(
        "relic_select.json",
        "relic_select",
        {
            "state_type": "relic_select",
            "relic_select": {
                "prompt": "选择一个遗物。",
                "relics": [
                    {"index": 0, "id": "BLACK_STAR", "name": "黑星",
                     "description": "精英敌人额外掉落一个遗物。", "rarity": "Rare",
                     "keywords": []},
                    {"index": 1, "id": "BREWING", "name": "酿造",
                     "description": "战斗结束时获得一瓶药水。", "rarity": "Rare",
                     "keywords": []},
                ],
                "can_skip": True,
            },
            "run": _run(floor=16),
            "player": _player(),
        },
    )
    add(
        "crystal_sphere.json",
        "crystal_sphere",
        {
            "state_type": "crystal_sphere",
            "crystal_sphere": {
                "instructions_title": "水晶球",
                "grid_width": 9,
                "grid_height": 11,
                "cells": [{"x": 0, "y": 0, "is_hidden": True, "is_clickable": True,
                           "is_highlighted": False, "is_hovered": False}],
                "clickable_cells": [{"x": 4, "y": 7}, {"x": 5, "y": 7}],
                "revealed_items": [],
                "tool": "none",
                "can_use_big_tool": True,
                "can_use_small_tool": True,
                "divinations_left_text": "还剩3次占卜",
                "can_proceed": False,
            },
            "run": _run(),
            "player": _player(),
        },
    )
    add(
        "game_over.json",
        "game_over",
        {
            "state_type": "game_over",
            "game_over": {"message": "Run ended.", "options": ["main_menu"]},
            "run": _run(),
            "player": _player(hp=0),
        },
    )
    add(
        "overlay.json",
        "overlay",
        {
            "state_type": "overlay",
            "overlay": {
                "screen_type": "NSomeUnknownScreen",
                "message": "An overlay is active. It may require manual interaction.",
            },
            "run": _run(),
            "player": _player(),
        },
    )
    return fixtures


def main() -> int:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    fixtures = {**synthetic_fixtures(), **captured_fixtures()}
    for name, fixture in sorted(fixtures.items()):
        (FIXTURE_DIR / name).write_text(
            json.dumps(fixture, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"wrote tests/fixtures/screens/{name} ({'captured' if fixture['captured'] else 'synthetic'})")
    print(f"{len(fixtures)} fixtures under {FIXTURE_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
