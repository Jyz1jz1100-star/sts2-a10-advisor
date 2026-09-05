"""A10 out-of-combat heuristic advisor for live STS2MCP states.

This is the first deliverable of the out-of-combat decision track: a
deterministic, explainable rule policy for the screens the Combat Solver mod
cannot touch (card reward, shop, rest site, map pathing). Combat screens are
deliberately NOT covered — in-combat advice is the Combat Solver's job (and,
after Phase D, the distilled policy), so this policy raises on them and the
live loop keeps the overlay quiet there.

Scope honesty:
- rules encode explicit A10 conventions (deck thinning, rest/smith threshold,
  buy priority, path preference by HP band) — every recommendation carries its
  facts in Chinese;
- `event`/Neow are intentionally not covered by the heuristic: the codec can
  expose their visible candidates, but this policy has no event-specific value
  model and therefore refuses to invent a choice. A trained replacement (BC
  on live features) supersedes this policy later.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .contracts import Candidate, Recommendation
from .live_candidate_codec import LiveCandidateSet, extract_live_candidates
from .policy import SmokeBaselinePolicy

_MODEL_ID = "heuristic-out-of-combat-v1"

_RARITY_VALUE = {"Rare": 3.0, "Uncommon": 2.0, "Common": 1.0, "Special": 2.0}
_BASIC_CARD_IDS = {
    "STRIKE_IRONCLAD", "DEFEND_IRONCLAD", "STRIKE_SILENT", "DEFEND_SILENT",
    "STRIKE", "DEFEND", "BASH", "ASCENDERS_BANE", "NEUTRALIZE", "SURVIVOR",
}
_NODE_SCORES = {
    "campfire": (5.0, 3.2),    # (low HP, high HP)
    "rest": (5.0, 3.2),
    "shop": (3.4, 3.4),
    "treasure": (3.8, 3.8),
    "event": (2.6, 2.6),
    "monster": (2.0, 2.0),
    "elite": (0.8, 4.2),
    "unknown": (2.5, 2.5),
}

_REST_HEAL_THRESHOLD = 0.75


def _indexed_wire_action(
    action: Mapping[str, Any], *, wire_name: str, wire_key: str,
    internal_key: str | None = None,
) -> dict[str, Any]:
    """Translate one indexed internal action without inventing an index."""

    source_key = internal_key or wire_key
    if source_key not in action:
        raise ValueError(f"internal action is missing {source_key!r}")
    return {"action": wire_name, wire_key: action[source_key]}


def _wire_action_for_internal(action: Mapping[str, Any]) -> dict[str, Any]:
    """Map policy actions to the exact STS2MCP body used by the codec.

    The policy still returns the established internal action vocabulary so the
    autoplay bridge remains the owner of POSTs.  This adapter is deliberately
    local to the policy layer: importing bridge/autoplay here would couple a
    read-only advisor to the action executor.
    """

    if not isinstance(action, Mapping):
        raise ValueError("internal action must be an object")
    kind = action.get("type")
    if kind == "map_choose_node":
        return _indexed_wire_action(
            action, wire_name="choose_map_node", wire_key="index"
        )
    if kind == "rewards_pick_card":
        return _indexed_wire_action(
            action,
            wire_name="select_card_reward",
            wire_key="card_index",
            internal_key="index",
        )
    if kind in {"rewards_skip_card", "rewards_skip"}:
        return {"action": "skip_card_reward"}
    if kind == "shop_buy":
        return _indexed_wire_action(action, wire_name="shop_purchase", wire_key="index")
    if kind == "shop_leave":
        return {"action": "proceed"}
    if kind == "rest_choose_option":
        return _indexed_wire_action(
            action, wire_name="choose_rest_option", wire_key="index"
        )
    if kind == "event_choose_option":
        return _indexed_wire_action(
            action, wire_name="choose_event_option", wire_key="index"
        )
    raise ValueError(f"no live codec mapping for internal action {kind!r}")


def validated_wire_action(
    state: Mapping[str, Any], action: Mapping[str, Any]
) -> dict[str, Any]:
    """Return an exact wire body only when it is legal in ``state``.

    This is useful to callers that want the codec's identity/action check while
    keeping action execution elsewhere.  It performs no I/O.
    """

    candidate_set = extract_live_candidates(state)
    wire_action = _wire_action_for_internal(action)
    candidate_set.identity_for(wire_action)
    return wire_action


def _hp_fraction(state: dict[str, Any]) -> tuple[float, int, int]:
    player = state.get("player") or {}
    hp = int(player.get("hp") or 0)
    max_hp = int(player.get("max_hp") or 0) or 1
    return hp / max_hp, hp, max_hp


def _card_value(card: dict[str, Any]) -> float | None:
    """None = never pick (basic starter cards / curses)."""
    card_id = str(card.get("id") or "")
    rarity = str(card.get("rarity") or "")
    if rarity.lower() == "curse" or card_id in _BASIC_CARD_IDS:
        return None
    value = _RARITY_VALUE.get(rarity, 1.0)
    if str(card.get("type") or "").lower() in {"skill", "power"}:
        value += 0.5
    return value


class LiveHeuristicPolicy:
    """Rule-based out-of-combat advisor; combat screens are out of scope.

    The heuristic remains the policy of record.  Before and after applying its
    rules, the live candidate codec validates the visible decision window and
    the exact corresponding STS2MCP action.  It does not turn this baseline
    into a trained policy or perform any I/O.
    """

    model_id = _MODEL_ID

    def __init__(self) -> None:
        self._fallback = SmokeBaselinePolicy()

    @staticmethod
    def candidates(state: Mapping[str, Any]) -> LiveCandidateSet:
        """Return the current legal candidate set without choosing one."""

        return extract_live_candidates(state)

    # Explicit alias for callers whose code uses the codec vocabulary.
    live_candidates = candidates

    @staticmethod
    def wire_action_for(
        state: Mapping[str, Any], action: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Return ``action``'s exact legal wire body, or fail closed."""

        return validated_wire_action(state, action)

    @staticmethod
    def _validate_recommendation(
        recommendation: Recommendation, candidate_set: LiveCandidateSet
    ) -> Recommendation:
        """Ensure every surfaced action is present in this live decision set."""

        surfaced = (recommendation.primary, *recommendation.alternatives)
        for candidate in surfaced:
            wire_action = _wire_action_for_internal(candidate.action)
            candidate_set.identity_for(wire_action)
        return recommendation

    def recommend(self, state: dict[str, Any]) -> Recommendation:
        state_type = str(state.get("state_type") or "unknown").strip().lower()
        handler = {
            "card_reward": self._card_reward,
            "shop": self._shop,
            "rest_site": self._rest_site,
            "map": self._map,
        }.get(state_type)
        if handler is None:
            if state_type in {"monster", "elite", "boss"}:
                raise ValueError(
                    "combat screens are delegated to the Combat Solver overlay"
                )
            raise ValueError(f"no heuristic for {state_type!r}")
        # Extraction is intentionally before the heuristic.  Malformed or
        # transitional states therefore fail closed instead of being converted
        # into a guessed action by a permissive rule default.
        candidate_set = self.candidates(state)
        recommendation = handler(state)
        return self._validate_recommendation(recommendation, candidate_set)

    # ------------------------------------------------------------ card_reward
    def _card_reward(self, state: dict[str, Any]) -> Recommendation:
        reward = state.get("card_reward") or {}
        cards = [c for c in (reward.get("cards") or []) if isinstance(c, dict)]
        scored = sorted(
            ((_card_value(c), c) for c in cards),
            key=lambda item: (-1 if item[0] is None else -item[0],
                              item[1].get("index", 0)),
        )
        _, hp, max_hp = _hp_fraction(state)
        facts_common = (
            f"生命 {hp}/{max_hp}",
            "启发式规则：有可用牌时优先选牌，仅在无可用牌时跳过",
        )
        candidates: list[Candidate] = []
        for value, card in scored:
            if value is None:
                continue
            candidates.append(
                Candidate(
                    action={"type": "rewards_pick_card", "index": card.get("index", 0)},
                    label=f"选取{card.get('name') or card.get('id')}"
                    + ("（升级）" if card.get("is_upgraded") else ""),
                    score=value,
                    confidence=0.0,
                    facts=(
                        f"{card.get('name')}：{card.get('rarity')} {card.get('type')}",
                    )
                    + facts_common,
                )
            )
        can_skip = reward.get("can_skip", True)
        best = candidates[0] if candidates else None
        # Pick the best candidate whenever ANY non-basic, non-curse card is
        # offered (threshold 1.0); skip only when the reward is literally
        # empty of usable cards.
        if can_skip and best is None:
            primary = Candidate(
                action={"type": "rewards_skip"},
                label="跳过本次卡牌奖励",
                score=2.1,
                confidence=0.0,
                facts=("奖励中只有基础牌或诅咒",) + facts_common,
            )
        elif best is not None:
            primary = best
        else:
            # Do not let Recommendation.primary become None when the live
            # screen requires a pick.  The codec has already proved that the
            # screen is structurally actionable, but this heuristic has no
            # safe preference among only basic/curse cards.
            raise ValueError(
                "card_reward has no heuristic pick and skipping is unavailable"
            )
        alternatives = tuple(c for c in candidates if c is not primary)[:2]
        return Recommendation(
            phase="card_reward",
            primary=primary,
            alternatives=alternatives,
            model_id=self.model_id,
            game_build=str((state.get("game") or {}).get("build") or "unknown"),
            warnings=("启发式规则 v1：只陈述规则与事实，不保证全局最优。",),
        )

    # ----------------------------------------------------------------- shop
    def _shop(self, state: dict[str, Any]) -> Recommendation:
        shop = state.get("shop") or {}
        items = [i for i in (shop.get("items") or []) if isinstance(i, dict)]
        player = state.get("player") or {}
        gold = int(player.get("gold") or 0)
        fraction, hp, max_hp = _hp_fraction(state)
        hp_fact = f"生命 {hp}/{max_hp}" if (state.get("player") or {}).get("max_hp") else None
        affordable = [
            i for i in items
            if i.get("is_stocked") and i.get("can_afford") is not False
            and int(i.get("price") or 0) <= gold
        ]

        def _label(item: dict[str, Any]) -> str:
            category = item.get("category")
            if category == "card_removal":
                return "删卡"
            if category == "relic":
                return f"购买遗物 {item.get('relic_name') or item.get('relic_id')}"
            if category == "potion":
                return f"购买药水 {item.get('potion_name') or item.get('potion_id')}"
            return f"购买卡牌 {item.get('card_name') or item.get('card_id')}"

        def _score(item: dict[str, Any]) -> float:
            category = item.get("category")
            if category == "card_removal":
                return 5.0
            if category == "relic":
                return 4.0
            if category == "card":
                rarity = str(item.get("card_rarity") or "")
                return _RARITY_VALUE.get(rarity, 1.0)
            if category == "potion":
                return 1.2
            return 0.5

        ranked = sorted(
            affordable, key=lambda i: (-_score(i), int(i.get("price") or 0))
        )
        candidates = [
            Candidate(
                action={"type": "shop_buy", "index": item.get("index", 0)},
                label=_label(item),
                score=_score(item),
                confidence=0.0,
                facts=(f"价格 {item.get('price')}（金币 {gold}）",),
            )
            for item in ranked
        ]
        can_proceed = shop.get("can_proceed") is True
        stocked_any = any(
            i.get("is_stocked") for i in items if isinstance(i, dict)
        )
        # The mod opens the merchant inventory on entry and reports
        # can_proceed=false while it is open; the proceed wire action still
        # closes it and leaves.  Without stocked items the state is unknown
        # and the driver must not guess an exit.
        leave = Candidate(
            action={"type": "shop_leave"},
            label="离开商店",
            score=0.4,
            confidence=0.0,
            facts=(
                "无可负担的高优先级商品",
                f"金币 {gold}",
                *(() if can_proceed else ("库存界面打开中，proceed 会先关闭库存",)),
            ),
        ) if (can_proceed or stocked_any) else None
        if candidates:
            primary = candidates[0]
        elif leave is not None:
            primary = leave
        else:
            raise ValueError("shop has no affordable item and cannot proceed")
        alternatives = tuple(candidates[1:3]) if candidates else ()
        return Recommendation(
            phase="shop",
            primary=primary,
            alternatives=alternatives,
            model_id=self.model_id,
            game_build=str((state.get("game") or {}).get("build") or "unknown"),
            warnings=(
                "优先级：删卡 > 遗物 > 高稀有卡 > 药水。",
                *([hp_fact] if hp_fact else []),
            ),
        )

    # ------------------------------------------------------------ rest_site
    def _rest_site(self, state: dict[str, Any]) -> Recommendation:
        site = state.get("rest_site") or {}
        options = [o for o in (site.get("options") or []) if isinstance(o, dict)]
        fraction, hp, max_hp = _hp_fraction(state)
        want_rest = fraction < _REST_HEAL_THRESHOLD
        preferred_id = "rest" if want_rest else "smith"
        chosen = next(
            (o for o in options if o.get("id") == preferred_id and o.get("is_enabled") is not False),
            None,
        )
        if chosen is None:
            chosen = next(
                (o for o in options if o.get("is_enabled") is not False), None
            )
        if chosen is None:
            raise ValueError("rest_site has no enabled option")
        if chosen.get("id") == "rest":
            reason = (
                f"生命 {hp}/{max_hp}（{fraction:.0%}）低于 {_REST_HEAL_THRESHOLD:.0%}，先回血"
                if want_rest
                else f"生命 {hp}/{max_hp}（{fraction:.0%}）健康，仍可选择休息"
            )
        elif chosen.get("id") == "smith":
            reason = (
                f"生命 {hp}/{max_hp}（{fraction:.0%}）健康，升级核心牌"
                if not want_rest
                else f"生命 {hp}/{max_hp}（{fraction:.0%}），休息不可用，选择锻造"
            )
        else:
            reason = (
                f"目标选项 {preferred_id} 不可用，选择 {chosen.get('id')}"
            )
        primary = Candidate(
            action={"type": "rest_choose_option",
                    "index": int(chosen.get("index", options.index(chosen)))},
            label=chosen.get("name") or str(chosen.get("id")),
            score=5.0 if chosen.get("id") == preferred_id else 1.0,
            confidence=0.0,
            facts=(reason,
                   chosen.get("description") or "",),
        )
        alternatives = tuple(
            Candidate(
                action={
                    "type": "rest_choose_option",
                    "index": int(o.get("index", options.index(o))),
                },
                label=o.get("name") or str(o.get("id")),
                score=1.0,
                confidence=0.0,
                facts=(o.get("description") or "",),
            )
            for o in options
            if o is not chosen and o.get("is_enabled") is not False
        )[:2]
        return Recommendation(
            phase="rest_site",
            primary=primary,
            alternatives=alternatives,
            model_id=self.model_id,
            game_build=str((state.get("game") or {}).get("build") or "unknown"),
            warnings=("阈值规则：生命 < 75% 选休息，否则锻造。",),
        )

    # ----------------------------------------------------------------- map
    def _map(self, state: dict[str, Any]) -> Recommendation:
        map_state = state.get("map") or {}
        options = [
            o for o in (map_state.get("next_options") or []) if isinstance(o, dict)
        ]
        if not options:
            raise ValueError("map has no next_options")
        fraction, hp, max_hp = _hp_fraction(state)

        def _score(option: dict[str, Any]) -> float:
            node_type = str(option.get("type") or "unknown").lower()
            band = _NODE_SCORES.get(node_type)
            if band is None:
                return 2.5
            return band[0] if fraction < _REST_HEAL_THRESHOLD else band[1]

        ranked = sorted(
            options, key=lambda o: (-_score(o), int(o.get("index") or 0))
        )
        candidates = [
            Candidate(
                action={"type": "map_choose_node", "index": o.get("index", 0)},
                label=f"路线 → {o.get('type')}（{o.get('col')},{o.get('row')}）",
                score=_score(o),
                confidence=0.0,
                facts=(
                    f"节点类型 {o.get('type')}",
                    f"生命 {hp}/{max_hp}（{fraction:.0%}）",
                ),
            )
            for o in ranked
        ]
        return Recommendation(
            phase="map",
            primary=candidates[0],
            alternatives=tuple(candidates[1:3]),
            model_id=self.model_id,
            game_build=str((state.get("game") or {}).get("build") or "unknown"),
            warnings=(
                "路径规则：低血优先火堆/宝箱，高血且健康才考虑精英。",
            ),
        )
