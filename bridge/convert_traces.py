"""Convert raw trace_controller JSONL into contract-valid decision traces.

The recorder (``bridge/trace_controller.py``) writes untrimmed event streams:
``session``, ``health``, ``state``, ``action``, ``result``, ``compendium``,
``run_identity`` ... This module turns every policy-relevant POST that can be
paired with its pre-action state into one ``training.trace_contract`` record:

  * ``visible_state`` passes a deterministic visible-information filter. The
    ordered ``player.draw_pile`` (future draw order is hidden to a human) is
    replaced by the public count plus a sorted composition multiset. The same
    treatment is applied to the discard/exhaust piles so nothing order-bearing
    survives anywhere.
  * ``legal_actions`` are enumerated for the exact state snapshot that was
    visible when the action was sent. Action ids mirror the STS2MCP wire
    payloads one-for-one (``play:<idx>:<entity>``, ``map:<idx>`` ...).
  * ``result`` is attributed from the POST response plus the first later state
    with a different decision id (that state's id becomes
    ``result.next_decision_id``; ``game_over`` maps to status ``terminal``).

Menu bootstrapping, set_ascension, health and probe events are navigation, not
strategy, and are skipped by default (``--include-menu`` keeps menu records).
Sessions whose recorded mod whitelist does not match the lock file are treated
as contaminated and skipped unless ``--allow-contaminated`` is passed.

Usage:

    python -m bridge.convert_traces runs/live_traces/clean-*.jsonl \
        --split train --out data/local/train.jsonl [--report report.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from bridge.trace_controller import decision_id as recorder_decision_id
from training.trace_contract import (
    CURRENT_PUBLIC_BETA_BUILD,
    DEFAULT_ASCENSION,
    DEFAULT_CHARACTER,
    TRACE_VERSION,
    find_seed_leaks,
    validate_record,
)

COMBAT_STATES = ("monster", "elite", "boss")
UNSUPPRESSED_STATES = {"unknown", "overlay"}


# --------------------------------------------------------------------------
# raw event loading
# --------------------------------------------------------------------------


@dataclass
class RawSession:
    path: Path
    session_id: str = ""
    mode: str = ""
    observed_mods: tuple[str, ...] = ()
    observed_game: dict[str, Any] = field(default_factory=dict)
    allowed_mods: frozenset[str] = frozenset({"STS2_MCP"})
    run_id: str | None = None
    seed: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    parse_errors: list[str] = field(default_factory=list)

    @property
    def build_string(self) -> str:
        version = str(self.observed_game.get("version", "")).strip()
        branch = str(self.observed_game.get("branch", "")).strip()
        if not version:
            return ""
        return f"{branch}-{version}" if branch else version

    @property
    def contaminated(self) -> bool:
        return set(self.observed_mods) != set(self.allowed_mods)


def load_raw_session(path: Path) -> RawSession:
    session = RawSession(path=path)
    with path.open("r", encoding="utf-8-sig") as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                session.parse_errors.append(f"{path.name}:{number}: {exc.msg}")
                continue
            if not isinstance(event, dict):
                session.parse_errors.append(f"{path.name}:{number}: not an object")
                continue
            session.events.append(event)
            kind = event.get("event_type")
            raw = event.get("raw")
            if kind == "session" and isinstance(raw, dict):
                session.session_id = str(event.get("session_id", ""))
                session.mode = str(raw.get("mode", ""))
                mods = raw.get("observed_mods")
                if isinstance(mods, list):
                    session.observed_mods = tuple(sorted(str(m) for m in mods))
                game = raw.get("observed_game")
                if isinstance(game, dict):
                    session.observed_game = game
                lock = raw.get("version_lock") or {}
                env = lock.get("evaluation_environment") or {}
                allowed = env.get("allowed_mod_ids")
                if isinstance(allowed, list) and allowed:
                    session.allowed_mods = frozenset(str(m) for m in allowed)
            elif kind == "run_identity" and isinstance(raw, dict):
                if raw.get("run_id"):
                    session.run_id = str(raw["run_id"])
                if raw.get("seed") not in (None, ""):
                    session.seed = str(raw["seed"])
    return session


# --------------------------------------------------------------------------
# visible-information filter
# --------------------------------------------------------------------------


def _card_key(card: Any) -> str:
    if isinstance(card, dict):
        base = str(card.get("id") or card.get("name") or "?")
        if card.get("is_upgraded"):
            base += "+"
        return base
    return str(card)


def _public_pile(pile: Any, count: Any) -> dict[str, Any]:
    if isinstance(pile, list):
        return {"count": len(pile), "composition": sorted(_card_key(c) for c in pile)}
    return {
        "count": count if isinstance(count, int) else 0,
        "composition": [],
    }


def visible_view(state: dict[str, Any]) -> dict[str, Any]:
    """Strip hidden-order pile data and non-visible noise from a raw state."""

    visible = {key: value for key, value in state.items() if key != "message"}
    player = visible.get("player")
    if isinstance(player, dict):
        player = dict(player)
        if "draw_pile" in player or "draw_pile_count" in player:
            player["draw_pile"] = _public_pile(
                player.get("draw_pile"), player.get("draw_pile_count")
            )
        if "discard_pile" in player or "discard_pile_count" in player:
            player["discard_pile"] = _public_pile(
                player.get("discard_pile"), player.get("discard_pile_count")
            )
        if "exhaust_pile" in player or "exhaust_pile_count" in player:
            player["exhaust_pile"] = _public_pile(
                player.get("exhaust_pile"), player.get("exhaust_pile_count")
            )
        player.pop("draw_pile_count", None)
        player.pop("discard_pile_count", None)
        player.pop("exhaust_pile_count", None)
        visible["player"] = player
    return visible


# --------------------------------------------------------------------------
# legal action enumeration (ids mirror the STS2MCP wire payloads exactly)
# --------------------------------------------------------------------------


def _action(action_id: str, action_type: str, *, label: str = "", **payload: Any):
    entry: dict[str, Any] = {"action_id": action_id, "action_type": action_type}
    if payload:
        entry["payload"] = payload
    if label:
        entry["label"] = label
    return entry


def _alive_enemies(state: dict[str, Any]) -> list[dict[str, Any]]:
    battle = state.get("battle") or {}
    enemies = battle.get("enemies") or state.get("enemies") or []
    return [
        enemy
        for enemy in enemies
        if isinstance(enemy, dict)
        and not enemy.get("is_dead")
        and enemy.get("hp", enemy.get("current_hp", 1)) > 0
    ]


def _needs_target(card: dict[str, Any]) -> bool:
    return str(card.get("target_type") or "").lower() in {"anyenemy", "enemy", "single_enemy"}


def _combat_actions(state: dict[str, Any]) -> list[dict[str, Any]]:
    player = state.get("player") or {}
    enemies = _alive_enemies(state)
    actions: list[dict[str, Any]] = []
    for index, card in enumerate(player.get("hand") or []):
        if not isinstance(card, dict) or card.get("can_play") is False:
            continue
        if _needs_target(card):
            valid = set(card.get("valid_targets") or card.get("valid_target_ids") or [])
            for enemy in enemies:
                entity = enemy.get("entity_id") or enemy.get("id")
                if entity is None or (valid and entity not in valid):
                    continue
                actions.append(
                    _action(
                        f"play:{index}:{entity}",
                        "play_card",
                        card_index=index,
                        target=entity,
                    )
                )
        else:
            actions.append(_action(f"play:{index}", "play_card", card_index=index))
    for fallback_slot, potion in enumerate(player.get("potions") or []):
        if not isinstance(potion, dict) or potion.get("can_use") is False:
            continue
        slot = potion.get("slot", fallback_slot)
        if _needs_target(potion):
            for enemy in enemies:
                entity = enemy.get("entity_id") or enemy.get("id")
                if entity is None:
                    continue
                actions.append(
                    _action(
                        f"potion:{slot}:{entity}",
                        "use_potion",
                        slot=slot,
                        target=entity,
                    )
                )
        else:
            actions.append(_action(f"potion:{slot}", "use_potion", slot=slot))
    actions.append(_action("end_turn", "end_turn"))
    return actions


def _indexed_options(container: dict[str, Any], key: str) -> Iterable[tuple[int, dict[str, Any]]]:
    for fallback_index, option in enumerate(container.get(key) or []):
        if not isinstance(option, dict):
            continue
        index = option.get("index", fallback_index)
        yield int(index), option


def _not_locked(option: dict[str, Any]) -> bool:
    if option.get("is_locked"):
        return False
    if option.get("is_enabled") is False:
        return False
    if option.get("is_stocked") is False:
        return False
    return True


IRONCLAD_TITLES = {"铁甲战士", "THE IRONCLAD", "IRONCLAD"}


def is_verified_ironclad_a10(state: dict[str, Any]) -> bool:
    """Refuse to label records unless the visible state proves Ironclad + A10.

    Pre-run menu screens carry no run block; menu records are separately gated
    by --include-menu and are excluded from acceptance corpora regardless.
    """

    run = state.get("run") or {}
    player = state.get("player") or {}
    if run.get("ascension") != DEFAULT_ASCENSION:
        return False
    character_id = str(player.get("character_id") or run.get("character_id") or "").upper()
    character = str(player.get("character") or run.get("character") or "").upper()
    return character_id == DEFAULT_CHARACTER or character in {t.upper() for t in IRONCLAD_TITLES}


def enumerate_legal_actions(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Return contract-shaped legal actions for one raw STS2MCP state.

    Ids are exactly the ids that ``payload_to_action_id`` derives from the
    matching wire payloads, so chosen actions always line up with the list.
    """

    state_type = str(state.get("state_type") or "unknown")
    if state_type in COMBAT_STATES:
        return _combat_actions(state)
    if state_type == "hand_select":
        container = state.get("hand_select") or {}
        actions = [
            _action(f"hand:card:{index}", "combat_select_card", card_index=index)
            for index, option in _indexed_options(container, "cards")
        ]
        if container.get("can_confirm"):
            actions.append(_action("hand:confirm", "combat_confirm_selection"))
        return actions
    if state_type == "map":
        container = state.get("map") or {}
        return [
            _action(f"map:{index}", "choose_map_node", index=index)
            for index, option in _indexed_options(container, "next_options")
        ]
    if state_type == "event":
        container = state.get("event") or {}
        if container.get("in_dialogue"):
            return [_action("event:advance", "advance_dialogue")]
        return [
            _action(f"event:{index}", "choose_event_option", index=index)
            for index, option in _indexed_options(container, "options")
            if _not_locked(option)
        ]
    if state_type == "rewards":
        container = state.get("rewards") or {}
        actions = [
            _action(f"reward:{index}", "claim_reward", index=index)
            for index, option in _indexed_options(container, "items")
        ]
        if container.get("can_proceed"):
            actions.append(_action("proceed", "proceed"))
        return actions
    if state_type == "card_reward":
        container = state.get("card_reward") or {}
        actions = [
            _action(f"reward:card:{index}", "select_card_reward", card_index=index)
            for index, option in _indexed_options(container, "cards")
        ]
        if container.get("can_skip", True):
            actions.append(_action("reward:skip", "skip_card_reward"))
        return actions
    if state_type == "rest_site":
        container = state.get("rest_site") or {}
        actions = [
            _action(f"rest:{index}", "choose_rest_option", index=index)
            for index, option in _indexed_options(container, "options")
            if _not_locked(option)
        ]
        if container.get("can_proceed"):
            actions.append(_action("proceed", "proceed"))
        return actions
    if state_type in {"shop", "fake_merchant"}:
        if state_type == "shop":
            container = state.get("shop") or {}
        else:
            container = (state.get("fake_merchant") or {}).get("shop") or {}
        actions = [
            _action(f"shop:{index}", "shop_purchase", index=index)
            for index, option in _indexed_options(container, "items")
            if _not_locked(option)
        ]
        if container.get("can_proceed"):
            actions.append(_action("proceed", "proceed"))
        return actions
    if state_type == "treasure":
        container = state.get("treasure") or {}
        actions = [
            _action(f"chest:{index}", "claim_treasure_relic", index=index)
            for index, option in _indexed_options(container, "relics")
        ]
        if container.get("can_proceed"):
            actions.append(_action("proceed", "proceed"))
        return actions
    if state_type == "card_select":
        container = state.get("card_select") or {}
        actions = [
            _action(f"grid:card:{index}", "select_card", index=index)
            for index, option in _indexed_options(container, "cards")
        ]
        if container.get("can_confirm"):
            actions.append(_action("grid:confirm", "confirm_selection"))
        if container.get("can_cancel"):
            actions.append(_action("grid:cancel", "cancel_selection"))
        return actions
    if state_type == "bundle_select":
        container = state.get("bundle_select") or {}
        actions = [
            _action(f"bundle:{index}", "select_bundle", index=index)
            for index, option in _indexed_options(container, "bundles")
        ]
        if container.get("can_confirm"):
            actions.append(_action("bundle:confirm", "confirm_bundle_selection"))
        if container.get("can_cancel"):
            actions.append(_action("bundle:cancel", "cancel_bundle_selection"))
        return actions
    if state_type == "relic_select":
        container = state.get("relic_select") or {}
        actions = [
            _action(f"relic:{index}", "select_relic", index=index)
            for index, option in _indexed_options(container, "relics")
        ]
        if container.get("can_skip", True):
            actions.append(_action("relic:skip", "skip_relic_selection"))
        return actions
    if state_type == "crystal_sphere":
        container = state.get("crystal_sphere") or {}
        actions: list[dict[str, Any]] = []
        if container.get("can_use_big_tool"):
            actions.append(_action("sphere:tool:big", "crystal_sphere_set_tool", tool="big"))
        if container.get("can_use_small_tool"):
            actions.append(_action("sphere:tool:small", "crystal_sphere_set_tool", tool="small"))
        for cell in container.get("clickable_cells") or []:
            if isinstance(cell, dict):
                actions.append(
                    _action(
                        f"sphere:cell:{cell.get('x')}:{cell.get('y')}",
                        "crystal_sphere_click_cell",
                        x=cell.get("x"),
                        y=cell.get("y"),
                    )
                )
        if container.get("can_proceed"):
            actions.append(_action("sphere:proceed", "crystal_sphere_proceed"))
        return actions
    if state_type == "menu":
        options = state.get("options") or []
        actions = []
        for option in options:
            if isinstance(option, str):
                actions.append(_action(f"menu:{option}", "menu_select", option=option))
            elif isinstance(option, dict) and option.get("name"):
                if option.get("enabled", True) is False:
                    continue
                name = str(option["name"])
                actions.append(_action(f"menu:{name}", "menu_select", option=name))
        return actions
    return []


def payload_to_action_id(payload: dict[str, Any], state_type: str) -> str | None:
    action = payload.get("action")
    if not isinstance(action, str):
        return None
    if action == "play_card":
        index = payload.get("card_index")
        target = payload.get("target")
        if not isinstance(index, int):
            return None
        return f"play:{index}:{target}" if target else f"play:{index}"
    if action == "end_turn":
        return "end_turn"
    if action in {"use_potion", "discard_potion"}:
        slot = payload.get("slot")
        if slot is None:
            return None
        target = payload.get("target")
        prefix = "potion" if action == "use_potion" else "discard"
        return f"{prefix}:{slot}:{target}" if target else f"{prefix}:{slot}"
    if action == "advance_dialogue":
        return "event:advance"
    if action == "proceed":
        return "proceed" if state_type != "crystal_sphere" else "sphere:proceed"
    if action == "confirm_selection":
        return "grid:confirm"
    if action == "cancel_selection":
        return "grid:cancel"
    if action == "combat_confirm_selection":
        return "hand:confirm"
    if action == "confirm_bundle_selection":
        return "bundle:confirm"
    if action == "cancel_bundle_selection":
        return "bundle:cancel"
    if action == "skip_card_reward":
        return "reward:skip"
    if action == "skip_relic_selection":
        return "relic:skip"
    if action in {"crystal_sphere_set_tool", "set_ascension"}:
        value = payload.get("tool", payload.get("level"))
        if value is None:
            return None
        prefix = "sphere:tool" if action == "crystal_sphere_set_tool" else "ascension"
        return f"{prefix}:{value}"
    indexed = {
        "choose_map_node": ("map", "index"),
        "choose_event_option": ("event", "index"),
        "choose_rest_option": ("rest", "index"),
        "shop_purchase": ("shop", "index"),
        "claim_reward": ("reward", "index"),
        "select_card_reward": ("reward:card", "card_index"),
        "select_card": ("grid:card", "index"),
        "combat_select_card": ("hand:card", "card_index"),
        "select_bundle": ("bundle", "index"),
        "select_relic": ("relic", "index"),
        "claim_treasure_relic": ("chest", "index"),
    }
    if action in indexed:
        prefix, key = indexed[action]
        value = payload.get(key)
        if not isinstance(value, int):
            return None
        return f"{prefix}:{value}"
    if action == "crystal_sphere_click_cell":
        x, y = payload.get("x"), payload.get("y")
        if x is None or y is None:
            return None
        return f"sphere:cell:{x}:{y}"
    if action == "menu_select":
        option = payload.get("option")
        if not isinstance(option, str) or not option:
            return None
        return f"menu:{option}"
    return None


# --------------------------------------------------------------------------
# conversion
# --------------------------------------------------------------------------


def canonical_hash(state: dict[str, Any]) -> str:
    """Same canonicalization as recorder decision ids (server id wins, else hash)."""

    return recorder_decision_id(state)


@dataclass
class ConversionReport:
    records: list[dict[str, Any]] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)

    def bump(self, name: str) -> None:
        self.counters[name] = self.counters.get(name, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": len(self.records),
            "counters": dict(sorted(self.counters.items())),
            "issues": self.issues,
        }


def _result_from(
    response: dict[str, Any] | None,
    next_state: dict[str, Any] | None,
    before_state: dict[str, Any],
    next_decision_id: str | None = None,
) -> dict[str, Any]:
    status = "applied"
    observed = next_state is not None
    if response is None:
        status = "error"
    elif response.get("status") != "ok":
        status = "error"
    elif next_state is not None and next_state.get("state_type") == "game_over":
        status = "terminal"
    result: dict[str, Any] = {
        "status": status,
        "observed": bool(observed),
        "bridge_message": (response or {}).get("message"),
    }
    if next_state is not None:
        result["next_decision_id"] = next_decision_id or canonical_hash(next_state)
        result["next_state_type"] = next_state.get("state_type")
        before_player = before_state.get("player") or {}
        after_player = next_state.get("player") or {}
        if isinstance(before_player.get("hp"), int) and isinstance(after_player.get("hp"), int):
            result["hp_delta"] = after_player["hp"] - before_player["hp"]
        if isinstance(before_player.get("gold"), int) and isinstance(after_player.get("gold"), int):
            result["gold_delta"] = after_player["gold"] - before_player["gold"]
        before_run = before_state.get("run") or {}
        after_run = next_state.get("run") or {}
        if isinstance(before_run.get("floor"), int) and isinstance(after_run.get("floor"), int):
            result["floor_delta"] = after_run["floor"] - before_run["floor"]
    return result


def convert_session(
    session: RawSession,
    *,
    split: str,
    expected_build: str = CURRENT_PUBLIC_BETA_BUILD,
    include_menu: bool = False,
    allow_contaminated: bool = False,
    report: ConversionReport | None = None,
) -> list[dict[str, Any]]:
    """Convert one recorder session into contract records."""

    report = report if report is not None else ConversionReport()

    if session.parse_errors:
        report.issues.extend(f"parse: {item}" for item in session.parse_errors)
    if session.contaminated:
        message = (
            f"{session.path.name}: mods {list(session.observed_mods)} != allowed "
            f"{sorted(session.allowed_mods)} -> contaminated, excluded"
        )
        if not allow_contaminated:
            report.bump("sessions_skipped_contaminated")
            report.issues.append(message)
            return []
        report.issues.append("allow-contaminated: " + message)
    build = session.build_string or expected_build
    if build != expected_build:
        report.issues.append(
            f"{session.path.name}: observed build {build!r} != {expected_build!r}; "
            "records keep the observed build and the validator will reject mixing"
        )

    run_id = session.run_id or f"session:{session.session_id or session.path.stem}"
    seed = session.seed or f"RUNID:{run_id}"

    steps: dict[str, int] = {}
    states_by_id: dict[str, dict[str, Any]] = {}
    ordered: list[tuple[int, dict[str, Any]]] = []
    session_records: list[dict[str, Any]] = []
    for index, event in enumerate(session.events):
        ordered.append((index, event))
        if event.get("event_type") == "state":
            raw = event.get("raw")
            identifier = event.get("decision_id")
            if isinstance(raw, dict) and isinstance(identifier, str):
                # Last occurrence wins; polling repeats the same snapshot.
                states_by_id[identifier] = raw

    for index, event in ordered:
        if event.get("event_type") != "action":
            continue
        payload = event.get("raw")
        decision = event.get("decision_id")
        if not isinstance(payload, dict) or not isinstance(decision, str):
            report.bump("actions_skipped_malformed")
            continue
        state = states_by_id.get(decision)
        if state is None:
            report.bump("actions_skipped_missing_state")
            report.issues.append(
                f"{session.path.name}#{event.get('sequence')}: no state snapshot for {decision[:24]}..."
            )
            continue
        state_type = str(state.get("state_type") or "unknown")
        if state_type in UNSUPPRESSED_STATES:
            report.bump(f"actions_skipped_state_{state_type}")
            continue
        if not is_verified_ironclad_a10(state):
            report.bump("actions_skipped_not_ironclad_a10")
            continue
        if state_type == "menu" and not include_menu:
            report.bump("actions_skipped_menu")
            continue
        if state_type == "menu" and payload.get("action") == "set_ascension":
            report.bump("actions_skipped_bootstrap")
            continue
        chosen_id = payload_to_action_id(payload, state_type)
        if chosen_id is None:
            report.bump("actions_skipped_unmapped")
            report.issues.append(
                f"{session.path.name}#{event.get('sequence')}: unmappable payload {json.dumps(payload, ensure_ascii=False)[:120]}"
            )
            continue

        legal = enumerate_legal_actions(state)
        legal_ids = {entry["action_id"] for entry in legal}
        if chosen_id not in legal_ids:
            report.bump("actions_skipped_illegal")
            report.issues.append(
                f"{session.path.name}#{event.get('sequence')}: chosen {chosen_id!r} not in "
                f"{len(legal)} enumerated actions for {state_type}"
            )
            continue
        chosen_type = next(
            entry["action_type"] for entry in legal if entry["action_id"] == chosen_id
        )

        response: dict[str, Any] | None = None
        next_state: dict[str, Any] | None = None
        next_id: str | None = None
        for _, later in ordered[index + 1 :]:
            kind = later.get("event_type")
            if kind == "result" and later.get("decision_id") == decision and response is None:
                if isinstance(later.get("raw"), dict):
                    response = later["raw"]
                continue
            if kind == "result" and response is not None:
                break
            if kind == "action":
                break
            if kind == "state":
                raw = later.get("raw")
                if isinstance(raw, dict) and canonical_hash(raw) != decision:
                    next_state = raw
                    recorded_id = later.get("decision_id")
                    next_id = recorded_id if isinstance(recorded_id, str) else None
                    break
        result = _result_from(response, next_state, state, next_id)

        step = steps.get(run_id, 0)
        steps[run_id] = step + 1
        record = {
            "trace_version": TRACE_VERSION,
            "run_id": run_id,
            "decision_id": decision,
            "step": step,
            "split": split,
            "build": build,
            "seed": seed,
            "character": DEFAULT_CHARACTER,
            "ascension": DEFAULT_ASCENSION,
            "save_load_used": False,
            "visible_state": visible_view(state),
            "legal_actions": legal,
            "chosen_action": {"action_id": chosen_id, "action_type": chosen_type},
            "result": result,
        }
        issues = validate_record(record, line=event.get("sequence", 0), expected_build=build or None)
        if issues:
            report.bump("records_rejected_by_contract")
            report.issues.extend(issue.render(session.path.name) for issue in issues)
            continue
        session_records.append(record)
        report.bump(f"records_{state_type}")
        report.bump(f"result_{result['status']}")
    report.records.extend(session_records)
    return session_records


def iter_input_files(paths: Iterable[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(path.glob("*.jsonl")))
        else:
            files.append(path)
    return files


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(records, key=lambda r: (str(r["run_id"]), int(r["step"])))
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in ordered:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert raw recorder JSONL into contract-valid decision traces"
    )
    parser.add_argument("inputs", nargs="+", type=Path, help="raw trace files or directories")
    parser.add_argument("--split", required=True, choices=("train", "validation", "test"))
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--expected-build", default=CURRENT_PUBLIC_BETA_BUILD)
    parser.add_argument(
        "--allow-contaminated",
        action="store_true",
        help="convert sessions even when the recorded mod list deviates from the lock",
    )
    parser.add_argument("--include-menu", action="store_true", help="keep menu navigation records")
    parser.add_argument(
        "--allow-seed-less",
        action="store_true",
        help="accept sessions without a compendium run_identity seed (uses RUNID:<run> surrogate)",
    )
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = ConversionReport()
    all_records: list[dict[str, Any]] = []
    surrogate_runs: list[str] = []
    for path in iter_input_files(args.inputs):
        session = load_raw_session(path)
        records = convert_session(
            session,
            split=args.split,
            expected_build=args.expected_build,
            include_menu=args.include_menu,
            allow_contaminated=args.allow_contaminated,
            report=report,
        )
        report.records.clear()
        all_records.extend(records)
        if session.seed is None and records and session.run_id is None:
            surrogate_runs.append(session.run_id or path.name)
    if surrogate_runs and not args.allow_seed_less:
        print(
            "ERROR: sessions without a compendium run_identity produced RUNID-surrogate seeds: "
            + ", ".join(surrogate_runs)
            + "\nRe-capture with start-ironclad-a10 (it records run_identity) or pass --allow-seed-less "
            "for pipeline smoke data only (never for acceptance corpora).",
            file=sys.stderr,
        )
        return 1

    seed_issues = find_seed_leaks(
        (number, record) for number, record in enumerate(all_records, start=1)
    )
    for issue in seed_issues:
        report.issues.append(f"seed-leak: {issue.render()}")
    report.counters["seed_leak_issues"] = len(seed_issues)

    write_jsonl(args.out, all_records)
    summary = {
        "output": str(args.out),
        "split": args.split,
        "records": len(all_records),
        "counters": report.counters,
        "issues": report.issues,
    }
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(
            f"wrote {len(all_records)} records to {args.out}; "
            f"counters={report.counters}; issues={len(report.issues)}"
        )
        for line in report.issues[:20]:
            print(f"  issue: {line}")
    return 1 if seed_issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
