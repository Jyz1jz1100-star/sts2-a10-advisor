from __future__ import annotations

from typing import Any


def _card_label(card: dict[str, Any], target: dict[str, Any] | None = None) -> str:
    name = card.get("name") or card.get("id") or "未知卡牌"
    if target:
        target_name = target.get("name") or target.get("id") or "目标"
        return f"打出{name} → {target_name}"
    return f"打出{name}"


def combat_actions(state: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    """Enumerate only actions visible and legal to a human player.

    The function accepts the STS2MCP JSON shape. It intentionally never reads
    future RNG fields or hidden draw order.
    """

    player = state.get("player") or {}
    enemies = [
        e for e in ((state.get("battle") or {}).get("enemies") or state.get("enemies") or [])
        if e.get("hp", e.get("current_hp", 1)) > 0 and not e.get("is_dead")
    ]
    actions: list[tuple[dict[str, Any], str]] = []

    for index, card in enumerate(player.get("hand") or []):
        if card.get("can_play") is False or card.get("is_playable") is False:
            continue
        target_type = str(card.get("target_type") or "").lower()
        needs_enemy = target_type in {"anyenemy", "enemy", "single_enemy"}
        if needs_enemy:
            valid = set(card.get("valid_targets") or card.get("valid_target_ids") or [])
            for enemy in enemies:
                enemy_id = enemy.get("entity_id") or enemy.get("id")
                if valid and enemy_id not in valid:
                    continue
                action = {"type": "combat_play_card", "card": index, "target": enemy_id}
                actions.append((action, _card_label(card, enemy)))
        else:
            actions.append(({"type": "combat_play_card", "card": index}, _card_label(card)))

    for fallback_slot, potion in enumerate(player.get("potions") or []):
        if (not potion or potion.get("can_use") is False
                or potion.get("can_use_in_combat") is False):
            continue
        slot = potion.get("slot", fallback_slot)
        name = potion.get("name") or potion.get("id") or f"药水{slot + 1}"
        target_type = str(potion.get("target_type") or "").lower()
        if target_type in {"anyenemy", "enemy", "single_enemy"}:
            for enemy in enemies:
                enemy_id = enemy.get("entity_id") or enemy.get("id")
                actions.append(
                    ({"type": "use_potion", "slot": slot, "target": enemy_id},
                     f"使用{name} → {enemy.get('name') or enemy_id}")
                )
        else:
            actions.append(({"type": "use_potion", "slot": slot}, f"使用{name}"))

    actions.append(({"type": "combat_end_turn"}, "结束回合"))
    return actions


def decision_actions(state: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    state_type = state.get("state_type") or "unknown"
    if state_type in {"monster", "elite", "boss"}:
        return combat_actions(state)
    if state_type == "hand_select":
        hand_select = state.get("hand_select") or {}
        out = []
        for fallback_index, card in enumerate(hand_select.get("cards") or []):
            index = card.get("index", fallback_index)
            label = card.get("name") or card.get("id") or f"手牌 {index + 1}"
            out.append(({"type": "combat_select_card", "card": index}, f"选择{label}"))
        if hand_select.get("can_confirm"):
            out.append(({"type": "combat_confirm_selection"}, "确认选牌"))
        return out

    mappings = {
        "card_reward": ("card_reward", "cards", "rewards_pick_card"),
        "card_select": ("card_select", "cards", "card_select_choose"),
        "relic_select": ("relic_select", "relics", "relic_select_choose"),
        "event": ("event", "options", "event_choose_option"),
        "rest_site": ("rest_site", "options", "rest_choose_option"),
        "shop": ("shop", "items", "shop_buy"),
        "map": ("map", "next_options", "map_choose_node"),
    }
    mapping = mappings.get(state_type)
    if not mapping:
        return []
    container, key, action_type = mapping
    options = (state.get(container) or {}).get(key) or []
    out: list[tuple[dict[str, Any], str]] = []
    for fallback_index, option in enumerate(options):
        if option.get("is_locked") or option.get("is_enabled") is False:
            continue
        index = option.get("index", fallback_index)
        label = (option.get("name") or option.get("title") or option.get("type")
                 or option.get("id") or f"选项 {index + 1}")
        out.append(({"type": action_type, "index": index}, str(label)))
    if state_type == "card_reward" and (state.get("card_reward") or {}).get("can_skip", True):
        out.append(({"type": "rewards_skip_card"}, "跳过卡牌奖励"))
    if state_type == "shop":
        out.append(({"type": "shop_leave"}, "离开商店"))
    return out
