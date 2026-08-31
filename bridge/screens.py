"""Classify the STS2MCP `state_type` into owned decision screens vs no-ops.

Owned decision screens get advice. Combat and transitions are no-ops: the
advisor stays silent while the human plays the fight.
"""
from __future__ import annotations

# state_type values observed in STS2MCP docs/raw-full.md:
#   menu, monster, elite, boss, hand_select, rewards, card_reward, map, event,
#   rest_site, shop, card_select, treasure, relic_select, game_over, unknown

# Map raw state_type -> our normalized screen kind (used to pick a playbook).
_DECISION_KINDS = {
    "card_reward": "card_reward",
    "map": "map",
    "event": "event",
    "rest_site": "rest_site",
    "shop": "shop",
    "card_select": "card_select",
    "treasure": "treasure",
    "relic_select": "relic_select",
}

# Everything here is a no-op: combat, in-combat selections, menus, summaries.
_NOOP_KINDS = {
    "menu", "monster", "elite", "boss", "hand_select", "rewards",
    "game_over", "unknown", "loading", "transition",
}

# Screen kinds that exist (for config validation / routing docs).
SCREEN_KINDS = sorted(set(_DECISION_KINDS.values()) | {"boss_card_select"})

# A card_select grid this size or larger is treated as a high-stakes
# boss/transform grid for model-routing purposes.
_BOSS_GRID_THRESHOLD = 8


def classify(state: dict) -> tuple[str | None, bool]:
    """Return (kind, is_decision).

    kind is the normalized screen kind (None if unknown/no-op).
    is_decision is True only for owned decision screens.
    """
    st = (state or {}).get("state_type", "unknown")
    if st in _DECISION_KINDS:
        return _DECISION_KINDS[st], True
    return None, False


def _position_words(n: int) -> list[str]:
    """Natural left-to-right labels matching what the player sees as map forks."""
    presets = {
        1: ["the only"],
        2: ["left", "right"],
        3: ["left", "middle", "right"],
        4: ["far left", "middle-left", "middle-right", "far right"],
        5: ["far left", "middle-left", "center", "middle-right", "far right"],
        6: ["far left", "left", "middle-left", "middle-right", "right", "far right"],
    }
    if n in presets:
        return presets[n]
    ordinals = ["1st", "2nd", "3rd", "4th", "5th", "6th", "7th", "8th", "9th"]
    return [f"{ordinals[i]} from left" if i < len(ordinals) else f"#{i+1} from left"
            for i in range(n)]


def extract_options(state: dict, kind: str) -> list[dict]:
    """Pull the choosable options off the current screen into uniform dicts.

    Single source of truth used by both signature.py and grounding.py.
    Each option dict has at least: index, otype, and a label. Cards/relics also
    carry id/name/description/is_upgraded so grounding can join + display them.
    Field paths follow STS2MCP docs/raw-full.md; tweak here if the live mod
    differs (this is the only place that reads the per-screen layout).
    """
    s = state or {}
    if kind == "card_reward":
        return [_card(c) for c in (s.get("card_reward") or {}).get("cards", [])]
    if kind == "card_select":
        return [_card(c) for c in (s.get("card_select") or {}).get("cards", [])]
    if kind == "relic_select":
        return [_relic(r) for r in (s.get("relic_select") or {}).get("relics", [])]
    if kind == "shop":
        return [_shop_item(it) for it in (s.get("shop") or {}).get("items", [])]
    if kind == "map":
        opts = list((s.get("map") or {}).get("next_options", []))
        # The player sees these as forks left-to-right; label them by visible
        # position, not the game's internal col (which the player can't see).
        order = sorted(range(len(opts)), key=lambda i: (opts[i].get("col") is None,
                                                        opts[i].get("col"), i))
        words = _position_words(len(opts))
        pos_of = {oi: words[rank] for rank, oi in enumerate(order)}
        out = []
        for i, n in enumerate(opts):
            pos = pos_of[i]
            out.append({
                "index": n.get("index"), "otype": "node",
                "id": n.get("type"), "name": n.get("type"),
                "label": f"{pos} path -> {n.get('type')}",
                "pos_word": pos, "col": n.get("col"), "row": n.get("row"),
                "description": "", "is_upgraded": False,
            })
        return out
    if kind == "rest_site":
        out = []
        for o in (s.get("rest_site") or {}).get("options", []):
            out.append({
                "index": o.get("index"), "otype": "rest_option",
                "id": o.get("id"), "name": o.get("name"),
                "label": o.get("name") or o.get("id"),
                "description": "(disabled)" if o.get("is_enabled") is False else "",
                "is_upgraded": False, "enabled": o.get("is_enabled", True),
            })
        return out
    if kind == "event":
        out = []
        for o in (s.get("event") or {}).get("options", []):
            out.append({
                "index": o.get("index"), "otype": "event_option",
                "id": o.get("title"), "name": o.get("title"),
                "label": o.get("title"), "description": o.get("description", ""),
                "is_upgraded": False,
                # Rich fields the live API provides; not surfacing these was a bug.
                "is_locked": bool(o.get("is_locked")),   # option cannot be chosen
                "is_proceed": bool(o.get("is_proceed")),
                "relic_name": o.get("relic_name"),
                "relic_description": o.get("relic_description"),
                "keywords": o.get("keywords") or [],
            })
        return out
    if kind == "treasure":
        treasure = s.get("treasure") or {}
        relics = treasure.get("relics") or ([treasure["relic"]] if treasure.get("relic") else [])
        return [_relic(r) for r in relics]
    return []


def _card(c: dict) -> dict:
    # Accept both plain (card_reward/card_select) and card_-prefixed (shop) shapes.
    return {
        "index": c.get("index"), "otype": "card",
        "id": c.get("id") or c.get("card_id"),
        "name": c.get("name") or c.get("card_name"),
        "label": c.get("name") or c.get("card_name") or c.get("id") or c.get("card_id"),
        "description": c.get("description") or c.get("card_description", ""),
        "is_upgraded": bool(c.get("is_upgraded")),
        "cost": c.get("cost") or c.get("card_cost"),
        "ctype": c.get("type") or c.get("card_type"),
        "rarity": c.get("rarity") or c.get("card_rarity"),
        "keywords": c.get("keywords") or [],
    }


def _relic(r: dict) -> dict:
    return {
        "index": r.get("index"), "otype": "relic",
        "id": r.get("id") or r.get("relic_id"),
        "name": r.get("name") or r.get("relic_name"),
        "label": r.get("name") or r.get("relic_name") or r.get("id") or r.get("relic_id"),
        "description": r.get("description") or r.get("relic_description", ""),
        "is_upgraded": False, "counter": r.get("counter"),
        "rarity": r.get("rarity") or r.get("relic_rarity"),
        "keywords": r.get("keywords") or [],
    }


def _shop_item(it: dict) -> dict:
    """Normalize a shop item. Shop entries use category + prefixed fields
    (card_*/relic_*/potion_*) plus price/can_afford/on_sale flags."""
    cat = it.get("category")
    if cat == "relic":
        opt = _relic(it)
    elif cat == "potion":
        opt = {"index": it.get("index"), "otype": "potion",
               "id": it.get("potion_id") or it.get("id"),
               "name": it.get("potion_name") or it.get("name"),
               "label": it.get("potion_name") or it.get("name") or it.get("potion_id"),
               "description": it.get("potion_description") or it.get("description", ""),
               "is_upgraded": False, "keywords": it.get("keywords") or []}
    elif cat == "card_removal":
        opt = {"index": it.get("index"), "otype": "card_removal",
               "id": "card_removal", "name": "Card Removal",
               "label": "Remove a card", "is_upgraded": False,
               "description": "Remove a card from your deck.", "keywords": []}
    else:  # card
        opt = _card(it)
        opt["otype"] = "card"
    opt["price"] = it.get("price")
    opt["can_afford"] = it.get("can_afford")
    opt["on_sale"] = it.get("on_sale")
    return opt


def player_relics(state: dict) -> list[dict]:
    """Owned relics as uniform option dicts (so grounding can inject full text)."""
    return [_relic(r) for r in ((state.get("player") or {}).get("relics") or [])]


def known_keywords(state: dict, kind: str) -> list[dict]:
    """Collect keyword definition objects from on-screen options + owned relics.

    STS2MCP attaches a `keywords` list (e.g. {name, description}) to cards/relics/
    options. Injecting these as a glossary means the model never has to guess what
    Sly / Pierce / Doom / etc. do.
    """
    seen: dict[str, dict] = {}
    pools = extract_options(state, kind) + player_relics(state)
    for opt in pools:
        for kw in opt.get("keywords") or []:
            if isinstance(kw, dict):
                name = kw.get("name") or kw.get("keyword") or kw.get("id")
                desc = kw.get("description") or kw.get("text") or ""
            else:
                name, desc = str(kw), ""
            if name and name not in seen:
                seen[name] = {"name": name, "description": desc}
    return list(seen.values())


def model_kind(state: dict, kind: str) -> str:
    """The key used for per-screen model routing.

    Most kinds route by their own name. A large card_select grid (typically a
    boss reward / transform-the-deck moment) routes as `boss_card_select`.
    """
    if kind == "card_select":
        cards = (state.get("card_select") or {}).get("cards") or []
        if len(cards) >= _BOSS_GRID_THRESHOLD or str(state.get("state_type", "")).lower() == "boss":
            return "boss_card_select"
    if kind == "event" and is_ancient_event(state):
        return "ancient"   # run-long blessing — route to a stronger model
    return kind


def is_ancient_event(state: dict) -> bool:
    """Ancient encounters (run-long Blessings) arrive as events with is_ancient."""
    return bool((state.get("event") or {}).get("is_ancient"))
