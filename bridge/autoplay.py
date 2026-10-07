"""Autonomous out-of-combat run driver for the Combat Solver comparison track.

Combat is played by the Combat Solver mod itself (full-auto mode, toggled in
its panel). Everything around the fight — map pathing, card rewards, shop,
campfire, events, treasure, and starting fresh runs after a death — is driven
here via STS2MCP POST actions, using the live heuristic advisor's decisions.

Wire contract (verified against third_party/STS2MCP-main McpMod.Actions.cs):
advisor-internal action types map to the real dispatch names, e.g.
map_choose_node -> choose_map_node, rewards_pick_card -> select_card_reward
(with card_index), shop_buy -> shop_purchase, rest_choose_option ->
choose_rest_option (option list index), shop_leave -> proceed.

Safety model:
- POSTs are impossible without the explicit ``--allow-actions`` flag; the
  controller additionally refuses any non-loopback URL;
- every POST and its resulting state are written to a trace JSONL;
- combat screens are never touched (the mod owns them);
- stale-screen POSTs are rejected by the controller's decision-id guard;
- hard caps: ``--max-runs`` started runs and a total action budget.

In fixed mode this process is the only run starter.  It reserves and starts
seeds strictly in allocation order, commits a seed only after authoritative
identity readback, and stops at exhaustion.  A durable ledger must be
batch-local (``--seed-ledger`` or ``--batch-dir``); no allocation-sidecar
default is provided because that would couple independent batches.

The comparison harness (scripts/run_solver_comparison.py) stays read-only and
runs alongside this driver; the two never share POST responsibility.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

multiset_type = Counter

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from advisor_core.live_choice_policy import POLICY_VERSION as LIVE_CHOICE_POLICY_VERSION  # noqa: E402
from advisor_core.live_candidate_codec import EmptyCandidateError  # noqa: E402
from advisor_core.live_choice_policy import choose  # noqa: E402
from advisor_core.live_choice_policy import has_out_of_combat_use  # noqa: E402
from advisor_core.live_choice_policy import potion_to_discard_for  # noqa: E402
from advisor_core.live_choice_policy import potion_to_sip_now  # noqa: E402
from advisor_core.policy_live import LiveHeuristicPolicy  # noqa: E402

from combat_solver.snapshot import RouteAction  # noqa: E402

from bridge.trace_controller import (  # noqa: E402
    ActionPermissionError,
    BridgeConnectionError,
    BridgeProtocolError,
    merge_verified_run_identity,
    STS2MCPController,
    TraceRecorder,
    VersionLock,
)
from bridge.seed_allocation import (  # noqa: E402
    SeedAllocation,
    SeedAllocationError,
    SeedAllocationExhausted,
    SeedEntry,
    SeedLedger,
    load_seed_allocation,
)
from bridge.run_progress import RunCoverage  # noqa: E402


def _sha256_file(path: Path | None) -> str | None:
    """Hash an artifact for the acceptance trace, without fabricating it."""

    if path is None or not path.is_file():
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest().upper()


def _git_head() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _read_provenance(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"cannot read provenance file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("--provenance must contain a JSON object")
    return dict(payload)


class RunIdentityError(BridgeProtocolError):
    """A saved/continued run failed the automated identity contract."""


# Exit codes for ``python -m bridge.autoplay``: 0 is a clean quota or
# allocation end; EXIT_CLASSIFIED_STOP ends the batch on purpose for a
# recorded reason (stale state, bridge unavailable) instead of crashing.
# scripts/supervise_solver_batch.py mirrors this constant.
EXIT_CLASSIFIED_STOP = 3

#: How long a client-owned fight may leave the state byte-identical before the batch
#: stops and says so. Derived from the live traces, not chosen: 13,539 identical-state
#: runs inside combat screens recorded by supervised batches have a median of 0.5 s (one
#: poll interval) and a 99th percentile of 14 s, and every observation beyond two minutes
#: in that set was a stall that ended only when the process was stopped by hand. 180 s is
#: about thirteen times the observed p99, so a slow fight is not mistaken for a wedge while
#: a wedge is never waited on silently -- which is what the batch did at Act 2 floor 33 on
#: 2026-10-07, polling a paused solver for over fifteen minutes with no reason recorded.
COMBAT_PROGRESS_TIMEOUT_SECONDS = 180.0


class AutoplayClassifiedStop(RuntimeError):
    """A bounded, classified reason to end the autoplay batch.

    Raised only after retries that re-read fresh state were exhausted, and
    never to mask an exception: ``reason`` names the failure class
    (``stale_state``, ``bridge_unavailable``, ``repeated_failures``) and
    ``detail`` carries the last concrete error for the batch record.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def hand_index_for(card_id: str, nth: int, hand: list[dict[str, Any]]) -> int | None:
    """Index of the ``nth`` copy of ``card_id`` in the current hand."""
    candidates = [
        i for i, card in enumerate(hand)
        if card.get("id") == card_id and card.get("can_play") is not False
    ]
    if len(candidates) <= nth:
        return None
    return candidates[nth]


def target_entity_id(
    enemies: list[dict[str, Any]], target_index: int | None
) -> str | None:
    """Entity id for the route's plan-time enemy index (None = no target)."""
    if target_index is None or target_index < 0 or target_index >= len(enemies):
        return None
    enemy = enemies[target_index]
    return enemy.get("entity_id") or enemy.get("id")


def potion_slot_for(potion_id: str | None, potions: list[dict[str, Any]]) -> int | None:
    for position, potion in enumerate(potions):
        pid = potion.get("id") or potion.get("potion_id")
        if pid == potion_id:
            return int(potion.get("slot", position))
    return None


def select_route_action(
    actions: Sequence[Any],
    hand_ids: multiset_type,
    failed: set[int],
) -> tuple[int, Any] | None:
    """Pick the next route action to execute, driven by the CURRENT hand.

    Scans the route in order and executes the first play whose card is still
    in hand — this self-heals: cards already played drop out of the hand, so
    a stale route is skipped naturally, and a transient unplayable card is
    retried until it exhausts its attempts (``failed`` holds dead indices).
    Returns (index, action) or None when only end-turn remains (the caller
    ends the turn) or nothing is executable yet.
    """
    hand_available = +hand_ids  # copy with counts
    first_end_turn: int | None = None
    for index, action in enumerate(actions):
        if index in failed:
            continue
        if action.kind == "end_turn":
            if first_end_turn is None:
                first_end_turn = index
            continue
        card_id = action.card_id
        if card_id and hand_available[card_id] > 0:
            hand_available[card_id] -= 1
            return index, action
    if first_end_turn is not None:
        return first_end_turn, RouteAction(kind="end_turn")
    return None


def hand_multiset(hand: list[dict[str, Any]]) -> multiset_type:
    return Counter(
        card.get("id") for card in hand
        if isinstance(card, dict) and card.get("id")
    )

_COMBAT_SCREEN_TYPES = {"monster", "elite", "boss", "hand_select"}
# Screens the bridge reports *instead of* the fight it interrupts: the `battle` block is absent
# while one is up, so their owner can only be established from the frame before them.
_TRANSIENT_OVERLAY_SCREENS = {"card_select", "bundle_select", "overlay"}
# Actions whose acknowledgement means this screen is finished with. Deliberately one entry:
# `confirm_selection` was here first and cost a run -- a shop floor legitimately shows two different
# card choosers, and no observed refusal ever came from a second confirm. `menu_select` is added at
# the call site, because it ends a menu surface only in that it navigates away from it.
_TERMINAL_SCREEN_ACTIONS = {"proceed"}
#: Accepted posts allowed against one unchanged state read before the screen is called stuck.
#: Measured, not chosen: across the winning batch's 745 decision ids the healthy ceiling was 4 posts
#: on a gameplay screen, while a menu surface legitimately navigates several times on one read (it
#: reached 20). The value this exists to catch is 335 -- an enchant modal that answered `ok` to
#: every post and never advanced, which neither existing guard could see: the repeat detector keys on
#: the payload (it varied as different cards were tried) and the stale-attempt counter only moves on
#: a refusal.
MAX_POSTS_PER_STATE = 12
MAX_POSTS_PER_STATE_ON_MENU = 24
_HEURISTIC_SCREENS = {"card_reward", "shop", "rest_site", "map"}
# card_select is not a combat screen type: it doubles as the out-of-combat grid
# screens and the in-combat selection (battle key present).  The build's own
# auto-play registry, MegaCrit.Sts2.Core.AutoSlay.Handlers.Screens, drives every
# card-selection screen except one, so that exception is the only name the driver
# abstains on -- and it needs the class name because its state carries no
# ``battle`` key.  The registry's card screens: NDeckCardSelectScreen,
# NSimpleCardSelectScreen, NDeckTransformSelectScreen, NDeckUpgradeSelectScreen,
# NDeckEnchantSelectScreen, NChooseACardSelectionScreen,
# NChooseABundleSelectionScreen.  ``BuildCardSelectState`` spells the first four
# with a logical name and the rest with the class name.
#
# Evidence that NCombatPileCardSelectScreen belongs to the combat layer: in the
# recorded trace it disappeared within two polls with no action of ours, and the
# one ``select_card`` posted against it returned ``No card selection screen is
# open``.
_COMBAT_OWNED_CARD_SELECT_SCREENS = frozenset({"NCombatPileCardSelectScreen"})


def _screen_key(state: dict[str, Any]) -> tuple:
    """Which screen instance a decision belongs to, for the accepted-exit latch.

    The floor is not enough. A shop floor legitimately presents several screens of the same
    ``state_type`` -- remove one card, then enchant three -- and measuring that collision cost a run
    that reached act 3 floor 39: confirming the first chooser latched the second, and the client sat
    on 615 identical frames while we held. So the screen's own identity (its surface name, its
    prompt, how many things it offers) belongs in the key.
    """
    screen = state.get("run") or {}
    state_type = str(state.get("state_type") or "unknown")
    body = state.get(state_type)
    if state_type == "menu":
        return (state_type, str(state.get("menu_screen") or ""))
    if isinstance(body, dict):
        detail = (str(body.get("screen_type") or ""), str(body.get("prompt") or ""),
                  max((len(v) for v in body.values() if isinstance(v, list)), default=0))
    else:
        detail = ()
    return (state_type, screen.get("act"), screen.get("floor")) + detail


class WaitForTransition:
    """The driver is holding off on purpose: the client is mid-transition.

    Not a refused post and not a skipped screen, so it must not travel through
    the same path as either -- the caller keeps polling, the bounded stall
    watchdog still applies, and the run records how long it held.
    """

    __slots__ = ("reason",)

    def __init__(self, reason: str) -> None:
        self.reason = reason


def _map_position(state: dict[str, Any]) -> str:
    """The map node the client currently stands on, as a comparable value."""
    return json.dumps(
        (state.get("map") or {}).get("current_position"), sort_keys=True
    )


def _first_present(*values: Any) -> Any:
    """Return the first value that is present, including falsy scalars."""
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _canonical_character(state: dict[str, Any]) -> str:
    """Return the machine-readable character id from a live state."""
    player = state.get("player") or {}
    run = state.get("run") or {}
    value = _first_present(
        player.get("character_id"),
        player.get("character"),
        run.get("character_id"),
        run.get("character"),
    )
    normalized = str(value or "").strip().upper()
    if normalized in {"IRONCLAD", "THE IRONCLAD", "铁甲战士"}:
        return "IRONCLAD"
    return normalized.split(".")[-1]


def _character_is_readable(state: dict[str, Any]) -> bool:
    """False only when the frame carries no character field at all.

    The first state after a ``continue`` is often a transition frame with no
    ``player`` block.  Treating that as "not Ironclad" refused legitimate A10
    resumes outright, so absence is kept distinct from a real conflict.
    """
    return bool(_canonical_character(state))


def _run_identity_from_state(state: dict[str, Any]) -> dict[str, Any]:
    """Extract only identity fields needed by the automated run guard."""
    player = state.get("player") or {}
    run = state.get("run") or {}
    return {
        "character_id": _first_present(player.get("character_id"), run.get("character_id")),
        "character": _first_present(player.get("character"), run.get("character")),
        "ascension": _first_present(run.get("ascension"), player.get("ascension")),
        "game_mode": _first_present(
            run.get("game_mode"),
            run.get("mode"),
            state.get("game_mode"),
            state.get("mode"),
        ),
        "run_id": _first_present(run.get("run_id"), state.get("run_id")),
        "seed": run.get("seed"),
    }


def _run_identity_from_compendium(compendium: dict[str, Any]) -> dict[str, Any]:
    """Extract active or disk-backed saved-run identity from compendium JSON.

    STS2MCP intentionally keeps ``current_run`` active-only.  On the main
    menu, a visible ``continue`` can therefore coexist with
    ``current_run: null``; the additive ``saved_run`` block is the only
    acceptable pre-continue identity source in that state.
    """
    current = compendium.get("current_run") if isinstance(compendium, dict) else None
    saved = compendium.get("saved_run") if isinstance(compendium, dict) else None
    current_is_active = (
        isinstance(current, dict) and current.get("is_in_progress") is True
    )
    saved_is_present = isinstance(saved, dict) and saved.get("is_saved") is True
    if current_is_active and saved_is_present:
        raise RunIdentityError(
            "Refusing continue: compendium exposes active current_run and saved_run "
            "simultaneously"
        )
    use_saved = not current_is_active
    if use_saved and isinstance(saved, dict) and saved.get("is_saved") is True:
        current = saved
    elif use_saved:
        current = None
    if not isinstance(current, dict):
        return {}
    identity = {
        "ascension": current.get("ascension"),
        "game_mode": _first_present(current.get("game_mode"), current.get("mode")),
        "run_id": current.get("run_id"),
        "seed": current.get("seed"),
        "save_scope": current.get("save_scope"),
        "is_in_progress": current.get("is_in_progress"),
        # The compendium's selected run block is served by the singleplayer
        # profile endpoint.  Keep explicit negative multiplayer markers
        # visible as evidence if a bridge version ever starts returning them.
        "is_multiplayer": _first_present(
            current.get("is_multiplayer"), current.get("multiplayer")
        ),
    }
    if current.get("parse_error"):
        # Do not turn a partially parsed disk block into an identity.  The
        # source preserves the presence marker for diagnostics, while this
        # consumer rejects it before any Continue POST.
        identity["parse_error"] = current.get("parse_error")
    if use_saved:
        identity["is_saved"] = current.get("is_saved")
    return identity


def _require_saved_run_identity(compendium: dict[str, Any]) -> dict[str, Any]:
    """Fail closed before ``continue`` unless the saved run is standard A10."""
    identity = _run_identity_from_compendium(compendium)
    if (
        identity.get("is_in_progress") is not True
        and identity.get("is_saved") is not True
    ):
        raise RunIdentityError(
            "Refusing continue: compendium does not confirm an active or saved run"
        )
    if identity.get("parse_error"):
        raise RunIdentityError(
            "Refusing continue: selected run identity could not be parsed or read"
        )
    if identity.get("is_saved") is True:
        missing = [
            field
            for field in ("run_id", "seed")
            if _first_present(identity.get(field)) is None
        ]
        if missing:
            raise RunIdentityError(
                "Refusing continue: saved run identity is missing "
                + ", ".join(missing)
            )
    mode = str(identity.get("game_mode") or "").strip().upper()
    if mode != "STANDARD":
        raise RunIdentityError(
            "Refusing continue: saved run mode is not machine-verifiable standard "
            f"(observed {identity.get('game_mode')!r})"
        )
    if identity.get("is_multiplayer") is True:
        raise RunIdentityError(
            "Refusing continue: saved run is marked multiplayer"
        )
    if identity.get("ascension") != 10:
        raise RunIdentityError(
            "Refusing continue: saved run ascension is not A10 "
            f"(observed {identity.get('ascension')!r})"
        )
    identity = dict(identity)
    identity["singleplayer_verified"] = True
    return identity


def _identity_value_key(field: str, value: Any) -> Any:
    """Canonical comparison key for duplicated identity fields."""
    if field == "game_mode":
        return str(value).strip().upper()
    if field == "ascension":
        if isinstance(value, bool):
            return value
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    if field == "seed":
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.isascii() and stripped.isdecimal():
                try:
                    return int(stripped)
                except ValueError:
                    pass
            return stripped
    if field == "run_id":
        return str(value).strip()
    return value


def _merge_verified_run_identity(
    expected_saved: dict[str, Any], live_identity: dict[str, Any]
) -> dict[str, Any]:
    """Merge live identity without erasing verified saved-run provenance.

    The compendium is the authority for an existing save's mode, run id and
    seed.  A live state can omit those fields on the first frame, but it may
    not contradict them when it does expose them.  This prevents a nullable
    state field from silently replacing a value that was already verified.
    """
    merged = dict(expected_saved)
    for field in ("run_id", "seed", "ascension", "game_mode"):
        saved_value = expected_saved.get(field)
        live_value = live_identity.get(field)
        saved_present = _first_present(saved_value) is not None
        live_present = _first_present(live_value) is not None
        if saved_present and live_present:
            if _identity_value_key(field, saved_value) != _identity_value_key(
                field, live_value
            ):
                raise RunIdentityError(
                    "Refusing automated actions after continue: saved/live "
                    f"{field} values conflict (saved={saved_value!r}, "
                    f"live={live_value!r})"
                )
        elif not saved_present and live_present:
            merged[field] = live_value
    # Character is only machine-readable on the active state in this bridge;
    # preserve it when available, but never let a missing live value erase a
    # verified saved-run field above.
    for field in ("character_id", "character"):
        if _first_present(live_identity.get(field)) is not None:
            merged[field] = live_identity[field]
    return merged


def _require_continued_run_identity(
    state: dict[str, Any],
    expected_saved: dict[str, Any],
    post_compendium: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fail closed after ``continue`` unless the active run is Ironclad A10.

    When ``post_compendium`` is supplied, it is the first post-Continue
    authoritative save read.  The live state alone does not expose stable
    ``run_id``/seed fields on every bridge build, so accepting it without this
    read could bind gameplay to a different run.  A missing/non-active current
    block or any identity conflict is rejected; transport failures remain the
    caller's transient bridge-retry path.
    """
    identity = _run_identity_from_state(state)
    character = _canonical_character(state)
    if character != "IRONCLAD":
        raise RunIdentityError(
            "Refusing automated actions after continue: active run is not "
            f"Ironclad (observed character_id={identity.get('character_id')!r}, "
            f"character={identity.get('character')!r})"
        )
    if state.get("is_multiplayer") is True or state.get("multiplayer") is True:
        raise RunIdentityError(
            "Refusing automated actions after continue: active state is multiplayer"
        )
    players = state.get("players")
    if isinstance(players, list) and len(players) != 1:
        raise RunIdentityError(
            "Refusing automated actions after continue: active state does not "
            f"contain exactly one player (count={len(players)})"
        )
    if identity.get("ascension") != 10:
        raise RunIdentityError(
            "Refusing automated actions after continue: active run is not A10 "
            f"(observed {identity.get('ascension')!r})"
        )
    # The pre-continue compendium is the only authoritative machine-readable
    # mode for a saved run. Preserve it in the trace and require it to have
    # been checked; do not infer "standard" from a missing state field.
    if (
        str(expected_saved.get("game_mode") or "").strip().upper() != "STANDARD"
        or expected_saved.get("singleplayer_verified") is not True
    ):
        raise RunIdentityError("Refusing automated actions: saved-run mode guard was not verified")
    if post_compendium is not None:
        current_run = post_compendium.get("current_run")
        if not isinstance(current_run, dict) or current_run.get("is_in_progress") is not True:
            raise RunIdentityError(
                "Refusing automated actions after continue: compendium has no active current_run"
            )
        saved_run = post_compendium.get("saved_run")
        if isinstance(saved_run, dict) and saved_run.get("is_saved") is True:
            raise RunIdentityError(
                "Refusing automated actions after continue: compendium exposes "
                "active current_run and saved_run simultaneously"
            )
        try:
            authoritative = merge_verified_run_identity(state, post_compendium)
        except BridgeProtocolError as exc:
            raise RunIdentityError(
                "Refusing automated actions after continue: active compendium identity "
                f"is incomplete ({exc})"
            ) from exc
        for field in ("run_id", "seed"):
            expected_value = _first_present(expected_saved.get(field))
            observed_value = _first_present(authoritative.get(field))
            if expected_value is None or observed_value is None:
                raise RunIdentityError(
                    "Refusing automated actions after continue: active compendium "
                    f"identity has no {field}"
                )
            if _identity_value_key(field, expected_value) != _identity_value_key(
                field, observed_value
            ):
                raise RunIdentityError(
                    "Refusing automated actions after continue: saved/active "
                    f"{field} values conflict"
                )
        identity = authoritative
    merged = _merge_verified_run_identity(expected_saved, identity)
    merged["guard"] = "continue_post_state"
    return merged


def _to_payload(action: dict[str, Any]) -> dict[str, Any]:
    """Map an advisor-internal action dict onto the STS2MCP wire format."""
    kind = action["type"]
    if kind == "combat_play_card":
        payload: dict[str, Any] = {"action": "play_card", "card_index": action["card"]}
        if action.get("target") is not None:
            payload["target"] = action["target"]
        return payload
    if kind == "use_potion":
        payload = {"action": "use_potion", "slot": action["slot"]}
        if action.get("target") is not None:
            payload["target"] = action["target"]
        return payload
    if kind == "combat_end_turn":
        return {"action": "end_turn"}
    if kind == "map_choose_node":
        return {"action": "choose_map_node", "index": action["index"]}
    if kind == "event_choose_option":
        return {"action": "choose_event_option", "index": action["index"]}
    if kind == "rest_choose_option":
        return {"action": "choose_rest_option", "index": action["index"]}
    if kind == "shop_buy":
        return {"action": "shop_purchase", "index": action["index"]}
    if kind == "rewards_pick_card":
        return {"action": "select_card_reward", "card_index": action["index"]}
    if kind in {"rewards_skip", "rewards_skip_card"}:
        return {"action": "skip_card_reward"}
    if kind == "shop_leave":
        return {"action": "proceed"}
    raise ValueError(f"no wire mapping for action type {kind!r}")


def _screen_can_proceed(state: dict[str, Any]) -> bool:
    """True when any screen container advertises a continue control."""
    return any(
        isinstance(value, dict) and value.get("can_proceed") is True
        for value in state.values()
    )


#: What every frame carries, named screen or not.
_FRAME_ENVELOPE = frozenset({"state_type", "run", "player", "room_type"})


#: How many cards a selection screen asks for, read off its own prompt: the build
#: formats `CardSelectorPrefs.Prompt` into the screen's bottom label, so "选择3张牌来附魔。"
#: is the game stating its own MinSelect.  An ASCII digit or a Chinese numeral.
_CARDS_REQUESTED_RE = re.compile(
    r"(?:选择|choose|select)\s*(\d+|[一二三四五六七八九十]+)", re.IGNORECASE
)
_CHINESE_NUMERALS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def _cards_requested(selection: dict[str, Any]) -> int:
    """How many cards this selection screen asks for, or 0 when it never says.

    Zero means "no requirement read", and the walk then does what it has always done:
    honour ``can_confirm``. Falling back to a guessed count instead would let an
    unparseable prompt strand the run selecting cards a screen never asked for, which
    is a new way to fail rather than a fix -- the repeat guard still catches it.
    """
    prompt = str(selection.get("prompt") or "")
    match = _CARDS_REQUESTED_RE.search(prompt)
    if not match:
        return 0
    token = match.group(1)
    if token.isdigit():
        return max(1, int(token))
    return _CHINESE_NUMERALS.get(token, 0)


def _menu_option_names(state: dict[str, Any]) -> set[str]:
    """The menu options a click may actually be aimed at.

    The bridge shapes these two different ways depending on the screen: the main menu
    lists bare strings, while the singleplayer mode list returns objects carrying the
    name alongside whether the entry is selectable at all. Reading either with ``set()``
    raises on the second -- the dict is unhashable -- so the shape has to be unwrapped
    before the names are compared, and the enabled flag has to be honoured, because a
    greyed-out mode is not a candidate and clicking it is the same class of defect as
    posting a card that is not in hand.

    A missing ``enabled`` reads as selectable: an older bridge that never reports it must
    keep behaving exactly as it did rather than strand the driver with no options.
    """
    names: set[str] = set()
    for option in state.get("options") or []:
        if isinstance(option, dict):
            name = option.get("name")
            if name is None:
                continue
            if option.get("enabled", True) is False:
                continue
            names.add(str(name))
        elif isinstance(option, str):
            names.add(option)
    return names


class DeferredToCombat:
    """A screen the driver is leaving to the in-game Combat Solver, by name.

    Returning a bare ``None`` for this made an intentional hand-off
    indistinguishable from having no idea what the screen is, so the batch ledger
    booked 18 combat-owned card selections as "a screen skipped without a rule"
    while also booking them as deferred -- the same frame owned by somebody else
    and by nobody.  Abstaining is correct here; being unable to tell it from a gap
    is what the contract item exists to catch.
    """

    __slots__ = ("screen_type",)

    def __init__(self, screen_type: str) -> None:
        self.screen_type = screen_type


def _is_bare_frame(state: dict[str, Any]) -> bool:
    """True when the bridge named no screen and shipped no screen payload.

    Every screen the bridge can name carries a container under its own key -- a
    ``map`` frame has ``map``, a reward frame has ``rewards``.  A poll that lands
    between two rooms gets a frame with the run, the player and nothing else, and
    reports ``state_type: unknown`` because there is genuinely no room to name:
    17 of them in the deepest live run, one or two at most per node, each followed
    by a frame that did name its screen.

    The distinction this exists to keep is between *moment* and *content*.  A bare
    frame is a moment, so waiting on it is the only move that neither posts into a
    screen that may not exist nor reports a gap where none was.  An ``unknown``
    that does carry a payload is unmodelled content, and has to keep failing the
    way unmodelled content fails.
    """
    return state.get("state_type") == "unknown" and not (
        set(state) - _FRAME_ENVELOPE
    )



class AutoPlayer:
    def __init__(
        self,
        controller: STS2MCPController | None,
        *,
        max_runs: int = 20,
        max_actions: int = 2000,
        poll: float = 1.0,
        route_source: Any = None,
        out_of_combat_only: bool = False,
        combat_progress_timeout: float = COMBAT_PROGRESS_TIMEOUT_SECONDS,
        seed_file: Path | str | None = None,
        seed_ledger: Path | str | None = None,
        batch_dir: Path | str | None = None,
        failure_backoff: float = 1.0,
        bridge_backoff: float = 2.0,
        bridge_unavailable_timeout_seconds: float = 180.0,
        max_consecutive_failures: int = 60,
        max_total_failures: int = 600,
        clock: Any = None,
        travel_settle_seconds: float = 8.0,
        max_travel_reposts: int = 2,
    ):
        self.controller = controller
        self.policy = LiveHeuristicPolicy()
        self.max_runs = max_runs
        self.max_actions = max_actions
        self.poll = poll
        # A travel the bridge acknowledges is not a travel that happened.  The hold
        # below therefore has a deadline: after the settle window the same legal
        # option is re-posted a bounded number of times, and a node that still will
        # not be left ends the batch by name instead of stalling until the watchdog.
        self._clock = clock or time.monotonic
        self._travel_settle_seconds = float(travel_settle_seconds)
        self._max_travel_reposts = int(max_travel_reposts)
        # Retry budgets: every retry re-reads fresh state first; when a budget
        # is exhausted the batch ends as a classified stop instead of a crash.
        self._failure_backoff = failure_backoff
        self._bridge_backoff = bridge_backoff
        self._bridge_unavailable_timeout_seconds = bridge_unavailable_timeout_seconds
        self._max_consecutive_failures = max_consecutive_failures
        self._max_total_failures = max_total_failures
        # combat route executor: the mod (advice mode) auto-searches every
        # player turn and logs the route; we execute it over the bridge
        self._route_source = route_source
        # In the supervised comparison path Combat Solver/full-auto is the
        # sole combat owner.  Keep this guard independent of route_source so
        # a misconfigured caller cannot make autoplay POST during combat.
        self._out_of_combat_only = bool(out_of_combat_only)
        # Progress bound for a fight the *client* owns. Tracked on the whole state
        # payload rather than on the round counter, because a player turn that is
        # churning block, statuses and hand size has not stalled, while a byte-identical
        # state has stopped whatever the round number says.
        self._combat_progress_timeout = float(combat_progress_timeout)
        self._combat_state_digest: str | None = None
        self._combat_state_since: float = 0.0
        self._combat_state_screen: str | None = None
        self._combat_state_moved = 0
        self._combat_round: int | None = None
        self._combat_anchor_mono: float = 0.0
        self._combat_actions: tuple[Any, ...] = ()
        self._combat_failed: set[int] = set()
        self._combat_attempts = 0
        self._combat_note = ""
        self.actions_by_screen: dict[str, int] = {}
        # Which required flow steps this run actually walked through, and the
        # finished runs alongside them.  A batch that never met an act's Ancient
        # or never fought the final act's second boss has to say so.
        self.coverage = RunCoverage()
        self.completed_runs: list[dict[str, Any]] = []
        # A screen left by the generic proceed fallback is a skip, not a
        # decision; count them so a repeated one stops the batch instead of
        # carrying the run past content nobody can account for.
        self._bypass_counts: dict[str, int] = {}
        self._max_bypass_per_screen = 3
        #: frames spent on a screen that has neither a rule nor a continue control
        self._unhandled_counts: dict[str, int] = {}
        #: the identical frame the current unhandled streak started from
        self._unhandled_identity: tuple[str, str] | None = None
        #: map node whose travel the mod already acknowledged; the client keeps
        #: reporting that map while the travel animation plays, and posting a
        #: second choose_map_node into it is a move the state no longer offers
        self._map_travel_committed: str | None = None
        self._map_travel_payload: dict[str, Any] | None = None
        self._travel_acked_at: float | None = None
        self._travel_reposts = 0
        self._max_unhandled_per_screen = 10
        self.runs_started = 0
        # A batch that opens on a leftover run drives a run it never embarked, so counting only
        # embarks makes --max-runs mean two different things depending on what was on the save:
        # it collects the quota, then embarks one extra run it immediately abandons at the menu.
        # That last embark is why every batch used to leave a fresh act-1 run parked on the
        # client -- and a parked run is exactly what a seeded embark probe cannot work around.
        # Counted where a run is actually collected (``_seal_run``), so a batch that never reaches
        # a terminal cannot charge itself runs it did not gather.
        self.runs_adopted = 0
        self._current_run_embarked = False
        self.consecutive_failures = 0
        self._advance_id: str | None = None
        self._advance_posts = 0
        self._last_executed: tuple[str, str] | None = None
        self._repeat_execution_count = 0
        self.stop_reason: str | None = None
        self.seed_allocation: SeedAllocation | None = None
        self._seed_ledger: SeedLedger | None = None
        if seed_file is not None:
            self.seed_allocation = load_seed_allocation(seed_file)
            self._seed_ledger = SeedLedger(
                self.seed_allocation,
                seed_ledger,
                batch_dir=batch_dir,
            )
        elif seed_ledger is not None:
            raise ValueError("--seed-ledger requires --seed-file")
        elif batch_dir is not None:
            raise ValueError("--batch-dir requires --seed-file")
        self._seed_identity_checked = False
        # small per-screen state machines (treasure/bundle/card_select need
        # two steps); reset whenever the screen type changes
        self._last_step: tuple[str, str] | None = None
        self._last_screen: str | None = None
        #: ``select_card`` toggles, so the driver remembers which grid indices it
        #: has already clicked on the current card-selection screen.
        self._accepted_exit: tuple | None = None
        self._combat_screen_floor: tuple[Any, Any] | None = None
        self._card_select_screen: tuple[Any, ...] | None = None
        self._card_select_picks: set[int] = set()
        self._stall_since: float | None = None
        self._stall_screen: str | None = None
        # A menu ``continue`` is never trusted on its own.  The saved run is
        # checked through compendium before the POST and the first resulting
        # live state is checked for Ironclad/A10 before any gameplay action.
        self._continued_run_guard: dict[str, Any] | None = None
        #: Bounded wait for the first post-``continue`` frame that exposes a
        #: character; transition frames legitimately carry none for a moment.
        self._continue_identity_since: float | None = None
        self._continue_identity_frames = 0
        self._continue_identity_timeout = 60.0
        # If a crash left an active reservation and the authoritative save is
        # still sitting behind the menu, require the user-visible Continue
        # path before any fresh-start action.  Reconciliation must never turn
        # an unresolved reservation into an implicit overwrite.
        self._seed_continue_required = False

    # ------------------------------------------------------------- decisions
    def _map_travel_pending(self, state: dict[str, Any]) -> Any:
        """The client still reports the node an acknowledged travel left.

        Either the transition is in flight -- normal for a few hundred ms, and the
        reason this hold exists -- or the layer below accepted the request and never
        performed it, which is what a deadline can tell apart from a wait.  A silent
        no-op is re-posted (the same option is still the legal one) a bounded number of
        times, each re-post recorded; then it stops the batch by name rather than
        burning the 5-minute stall watchdog on it.
        """
        waited = (self._clock() - self._travel_acked_at) if self._travel_acked_at else 0.0
        if waited < self._travel_settle_seconds:
            return WaitForTransition("map travel acknowledged, waiting for the client to leave the node")
        run = state.get("run") or {}
        map_block = state.get("map") or {}
        options = map_block.get("next_options") or []
        payload = self._map_travel_payload or {}
        index = payload.get("index")
        target = next((o for o in options if isinstance(o, dict) and o.get("index") == index), None)
        source = map_block.get("current_position") or {}

        def _coord(node: Any) -> str:
            if not isinstance(node, dict):
                return "?"
            return f"({node.get('col')},{node.get('row')})"

        if self._travel_reposts >= self._max_travel_reposts or not payload:
            self._classified_stop(
                "travel_no_effect",
                f"map travel from {_coord(source)} to {_coord(target)} "
                f"({_coord(source)} is still reported) was acknowledged "
                f"{self._travel_reposts + 1} time(s) and the client never left the node "
                f"after {waited:.1f}s (act {run.get('act')} floor {run.get('floor')})",
            )
        self._travel_reposts += 1
        self.coverage.note_travel_repost(
            run.get("act"), run.get("floor"),
            source=source,
            destination={k: target.get(k) for k in ("col", "row", "type")} if target else {},
            attempt=self._travel_reposts,
            waited_seconds=waited,
        )
        self._travel_acked_at = self._clock()
        print(
            f"[autoplay] travel acknowledged but the client is still on this node; "
            f"re-posting ({self._travel_reposts}/{self._max_travel_reposts}) {payload}",
            flush=True,
        )
        return dict(payload)

    def decide(
        self, state: dict[str, Any]
    ) -> dict[str, Any] | WaitForTransition | None:
        """Return the wire payload for this screen, or None to leave it alone."""
        state_type = str(state.get("state_type") or "unknown")
        fresh = self._last_screen != state_type
        self._last_screen = state_type
        run = state.get("run") or {}
        screen_key = _screen_key(state)
        if state_type in _COMBAT_SCREEN_TYPES:
            # Remember where the fight is: the bridge strips the `battle` block while an
            # in-combat overlay is up, so a chooser that follows a combat frame at the same
            # floor is the solver's, and the only evidence for that is the frame before it.
            self._combat_screen_floor = (run.get("act"), run.get("floor"))
            return None  # the Combat Solver owns every combat screen
        if self._accepted_exit == screen_key:
            # We already posted the action that ends this screen and the client acknowledged
            # it; a frame that still shows the screen is the transition resolving, not work
            # left over. Posting again is what produced "Rewards screen is not open" and the
            # duplicate "Returning to main menu" whose late effect bounced the mode screen.
            return WaitForTransition(
                f"the exit already accepted at {state_type} has not cleared the screen")
        if state_type not in _TRANSIENT_OVERLAY_SCREENS:
            # The fight is over: rewards, map or event at the same floor are ours again, and a
            # chooser after them must not inherit the combat context.
            self._combat_screen_floor = None
        if state_type != "map":
            self._map_travel_committed = None
        event = state.get("event") or {}
        if state_type == "event" and event.get("in_dialogue"):
            # post-choice dialogue: advance until real options return
            return {"action": "advance_dialogue"}
        if state_type == "map":
            position = _map_position(state)
            if position != self._map_travel_committed:
                # The client left the node: the acknowledged travel did land, so
                # neither the lock nor the re-post budget carries over to the next one.
                self._map_travel_committed = None
                self._map_travel_payload = None
                self._travel_acked_at = None
                self._travel_reposts = 0
            else:
                return self._map_travel_pending(state)
        if (
            state_type == "rest_site"
            and (state.get("rest_site") or {}).get("can_choose") is False
        ):
            # The room's options are readable a moment before its UI node exists,
            # and the mod refuses a choice in that window.  Absent the flag -- an
            # older bridge -- behaviour is unchanged, because refusing to act on
            # an unknown would strand every rest site.
            return WaitForTransition("the rest site room cannot take a choice yet")
        rest_site = state.get("rest_site") or {}
        if (
            state_type == "rest_site"
            and not rest_site.get("options")
            and rest_site.get("can_proceed") is False
        ):
            # Spent, and the exit is not up yet: choosing clears `options` under
            # ShouldDisableRemainingRestSiteOptions, then the room awaits HideChoices and the heal
            # VFX before ShowProceedButton (NRestSiteRoom.AfterSelectingOptionAsync). The codec is
            # right to refuse a candidate list this empty -- the frame just is not a decision point,
            # and asking it 21 times read as 21 flow defects. `is False` keeps an older bridge that
            # publishes no `can_proceed` on the refusing path, where a truly stuck room must surface.
            return WaitForTransition("the rest site is spent and shows no exit yet")
        if state_type in ("map", "rest_site"):
            # A potion the state says can be drunk between fights, drunk when the
            # recorded rule says it is worth it.  With the installed bridge the
            # state never says so, and this returns None for every run.
            sip = potion_to_sip_now(state, at_rest_site=state_type == "rest_site")
            if sip is not None:
                return {"action": "use_potion", "slot": int(sip.get("slot", 0))}
        payload: dict[str, Any] | None = None
        if state_type in _HEURISTIC_SCREENS:
            try:
                payload = _to_payload(self.policy.recommend(state).primary.action)
            except ValueError:
                # The live policy validates its recommendation against the
                # candidate codec.  Only a codec-validated, proceed-only
                # transition may use the generic transition fallback.  This
                # prevents malformed/ambiguous/wrong-build states from being
                # turned into a proceed POST merely because can_proceed is
                # present in the raw JSON.
                candidates = self.policy.candidates(state)
                if (
                    len(candidates) == 1
                    and candidates[0].wire_action == {"action": "proceed"}
                ):
                    payload = {"action": "proceed"}
                else:
                    raise
        elif state_type == "treasure":
            if fresh:
                self._last_step = None
            payload = self._treasure_step(state)
        elif state_type == "relic_select":
            # A boss relic is one of the few decisions whose value persists for
            # the whole run, so it gets the recorded rule rather than slot zero.
            payload = {
                "action": "select_relic",
                "index": choose(((state.get("relic_select") or {}).get("relics")) or []),
            }
        elif state_type == "fake_merchant":
            # The fake merchant offer has two exits only: buy, or decline into a
            # fight.  The bridge exposes the decline as the event's proceed button
            # (McpMod.Actions.cs:618-637), which is disabled until the encounter is
            # resolved, so trying proceed is the one action that neither spends gold
            # nor picks a relic for the run.  A refusal is surfaced as a named error
            # rather than an idle, which is what used to happen here.
            payload = {"action": "proceed"}
        elif state_type == "bundle_select":
            if fresh:
                self._last_step = None
            payload = self._bundle_step()
        elif state_type == "rewards":
            payload = self._rewards_step(state)
        elif state_type == "event":
            payload = self._event_choice(state)
        elif state_type == "card_select":
            payload = self._card_select_choice(state)
        elif state_type == "crystal_sphere":
            payload = self._crystal_sphere_step(state)
        if state_type == "shop":
            freed = self._potion_room_to_free(state, payload)
            if freed is not None:
                return freed
        if payload is None and state_type == "event" and not _screen_can_proceed(state):
            # An event room with no candidate and no exit is mid-transition, in one of two
            # directions: the room has not built its options yet, or it is closing after a choice we
            # already made. THE_ARCHITECT did the latter at act 3 floor 49 for 8 frames -- 3.7 s --
            # and the run read that as a missing handler and stopped the deepest live run this
            # project has recorded. Waiting is bounded by the caller's stall watchdog, so an event
            # that really never presents anything is still named.
            return WaitForTransition("the event room offers no candidate and no exit yet")
        if payload is None and _is_bare_frame(state):
            # No screen at all, not a screen with no rule: hold, and let the same
            # watchdog name it if the client never comes back.
            return WaitForTransition("the client reported no screen on this frame")
        if payload is None and state_type != "map" and _screen_can_proceed(state):
            # continue buttons after an applied choice (rest result, shop,
            # claimed rewards, ...): the screen itself says it can advance.
            # On a screen with no rule this is a skip, not a decision, so it is
            # recorded and bounded rather than quietly carrying the run along.
            run = state.get("run") or {}
            self._bypass_counts[state_type] = self._bypass_counts.get(state_type, 0) + 1
            self.coverage.note_bypass(
                state_type, run.get("act"), run.get("floor"),
                reason="generic_proceed_fallback",
            )
            print(
                f"[autoplay] no rule for screen {state_type!r}; "
                f"proceeding past it ({self._bypass_counts[state_type]}x)",
                flush=True,
            )
            if self._bypass_counts[state_type] > self._max_bypass_per_screen:
                raise BridgeProtocolError(
                    f"screen {state_type!r} was skipped "
                    f"{self._bypass_counts[state_type]} times without a rule; "
                    "refusing to keep advancing past unmodelled content"
                )
            return {"action": "proceed"}
        return payload

    @staticmethod
    def _rewards_step(state: dict[str, Any]) -> dict[str, Any] | None:
        """Claim the next live reward, including cards.

        STS2MCP opens ``card_reward`` after a card reward is claimed, so the
        card policy must see that state before it can pick or skip a card.
        Reward indices are read from the latest state because the mod removes
        claimed buttons and re-numbers the remaining enabled rewards.
        """
        rewards = state.get("rewards") or {}
        for item in rewards.get("items") or []:
            if not isinstance(item, dict):
                continue
            if item.get("claimed") or item.get("was_chosen") or item.get("is_claimed"):
                continue
            return {
                "action": "claim_reward",
                "index": int(item.get("index") or 0),
            }
        # A disabled proceed button is a transient or malformed reward state;
        # do not guess by posting an action that the upstream endpoint rejects.
        if rewards.get("can_proceed") is True:
            return {"action": "proceed"}
        # Nothing left to claim and no exit offered yet is the last reward still
        # draining, which is the same shape as a spent campfire or an empty chest,
        # so it holds under the caller's bounded stall watchdog.  Returning None
        # here booked it as "a screen skipped without a rule" -- seen on the run
        # that cleared all three acts, at the act 2 boss and the final double
        # boss, where the run had in fact gone on to win.
        return WaitForTransition("the reward screen has nothing left to claim and no exit yet")

    def _card_select_choice(self, state: dict[str, Any]) -> dict[str, Any] | None:
        """Out-of-combat card selection (Neow/event/shop/removal/enchant): toggling UI.

        ``select_card`` TOGGLES the highlight and ``confirm_selection`` commits it.  The
        exposed state carries no per-card selected flag, so the driver walks distinct
        indices and commits when the screen both says it can confirm *and* has been
        shown the number of cards it asked for.

        The second half is not decoration.  ``can_confirm`` is the confirm button's own
        enabled state, and on an enchant grid that button is enabled before anything is
        picked -- where it is wired to ``PreviewSelection``, not to
        ``ConfirmSelection`` (NDeckEnchantSelectScreen.cs:223-226, :288-297), so
        clicking it commits nothing until the cards are there.  Reading it alone made
        the walk confirm an empty three-card selection four times in a row on live act 2
        floor 30, each answered ``ok`` with nothing moved, until the repeat guard
        stopped the batch.  The build's own auto-player selects first for exactly this
        reason: DeckEnchantScreenHandler.cs clicks cards while no preview is up and the
        confirm is not enabled, up to ``min(count, 5)`` of them.

        In-combat card selections carry a ``battle`` key and belong to the Combat Solver.
        """
        if state.get("battle") is not None:
            return None
        selection = state.get("card_select") or {}
        # ``screen_type`` is the overlay's class name for anything the bridge has
        # no logical name for.  That is NOT evidence of combat ownership: the
        # build's own auto-play registry
        # (MegaCrit.Sts2.Core.AutoSlay.Handlers.Screens) drives
        # NDeckEnchantSelectScreen out of combat, and keying the abstention on
        # "unrecognised name" left that screen with no owner -- it sat through
        # 11 polls, advancing nothing, until the unhandled-screen guard stopped
        # the batch.
        screen_type = str(selection.get("screen_type") or "")
        if screen_type in _COMBAT_OWNED_CARD_SELECT_SCREENS:
            self.coverage.note_deferred_to_combat(screen_type)
            return DeferredToCombat(screen_type)
        cards = [
            c for c in selection.get("cards") or [] if isinstance(c, dict)
        ]
        run = state.get("run") or {}
        # No `battle` block, but the frame before this one was a fight at this same floor: the
        # bridge removes the battle object while an in-combat chooser is up, so this overlay is
        # the solver's. Posting `select_card` into it is what came back as "No card selection
        # screen is open" at 08:21:29, one poll before the same elite round reappeared.
        if self._combat_screen_floor == (run.get("act"), run.get("floor")):
            # The coverage note carries the *why*; the screen type stays what it is, because the
            # log line and the ledger both print it as the client's own name for the overlay.
            self.coverage.note_deferred_to_combat(
                f"{screen_type or 'unknown'} (after combat at this floor)")
            return DeferredToCombat(screen_type or "unknown")
        identity = (screen_type, run.get("act"), run.get("floor"), len(cards))
        if identity != self._card_select_screen:
            self._card_select_screen = identity
            self._card_select_picks = set()
        wanted = min(_cards_requested(selection), len(cards))
        if selection.get("can_confirm") and len(self._card_select_picks) >= wanted:
            self._last_step = None
            self._card_select_screen = None
            self._card_select_picks = set()
            return {"action": "confirm_selection"}
        if not cards:
            return None
        unpicked = [
            c for c in cards if int(c.get("index", -1)) not in self._card_select_picks
        ]
        if not unpicked:
            # Every card offered has been clicked and the screen still cannot
            # confirm.  That is the grid resolving, not a state with no rule: seen
            # live at act 2 floor 33, where the run moved on half a second later.
            # Held like every other settle window -- bounded by the caller's stall
            # watchdog, so a grid that never lights up still stops the batch, now
            # under a reason that names it.
            return WaitForTransition(
                "the card selection has taken every offered pick and cannot confirm yet"
            )
        pick = next(
            (c for c in unpicked if str(c.get("id") or "").startswith("STRIKE")),
            unpicked[0],
        )
        chosen = int(pick.get("index", 0))
        self._card_select_picks.add(chosen)
        return {"action": "select_card", "index": chosen}

    @staticmethod
    def _potion_room_to_free(
        state: dict[str, Any], payload: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Discard a held potion when the shop pick this frame cannot be carried.

        The shop is the one screen where the run demonstrably hits its own capacity:
        5,179 recorded shop frames already had every potion slot filled, 5,119 of
        them with an affordable potion on the shelf.  The discard therefore never
        leads -- it only follows a purchase the policy already chose, and it only
        when the offered potion's own published text beats the weakest held text.
        """
        if payload is None or payload.get("action") != "shop_purchase":
            return None
        shop = state.get("shop") or {}
        player = state.get("player") or {}
        held = [p for p in (player.get("potions") or []) if isinstance(p, dict) and p.get("id")]
        max_slots = player.get("max_potion_slots")
        if not isinstance(max_slots, int) or len(held) < max_slots:
            return None
        items = [i for i in (shop.get("items") or []) if isinstance(i, dict)]
        offered = next(
            (
                i for i in items
                if int(i.get("index", -1)) == int(payload.get("index", -2))
                and i.get("category") == "potion"
            ),
            None,
        )
        if offered is None:
            return None
        weakest = potion_to_discard_for(state, offered)
        if weakest is None:
            return None
        return {"action": "discard_potion", "slot": int(weakest.get("slot", 0))}

    def _crystal_sphere_step(
        self, state: dict[str, Any]
    ) -> dict[str, Any] | WaitForTransition | None:
        """The Crystal Sphere minigame: reveal cells until the exit is offered.

        The screen was reported unhandled at act 3 floor 40 by
        ``ssb-20260921T133240Z-7eb0c916`` -- the deepest live run of the day --
        and the generic ``proceed`` cannot serve it: the mod's own proceed path
        looks for a different button than ``crystal_sphere_proceed`` does.  The
        rule mirrors the build's own handler
        (AutoSlay.Handlers.Screens/CrystalSphereScreenHandler.cs:28-95): take the
        exit once the screen offers one, otherwise reveal a cell it says is
        clickable, in a fixed order rather than the handler's random one.
        """
        sphere = state.get("crystal_sphere") or {}
        if sphere.get("can_proceed"):
            return {"action": "crystal_sphere_proceed"}
        cells = [c for c in (sphere.get("clickable_cells") or []) if isinstance(c, dict)]
        if not cells:
            # Nothing to reveal and no exit yet: the divinations are spent and the
            # reward drain is still resolving. Waiting is bounded by the caller's
            # stall watchdog, so this cannot spin forever.
            return WaitForTransition("the crystal sphere offers no cell and no exit yet")
        cell = min(cells, key=lambda c: (int(c.get("y", 0)), int(c.get("x", 0))))
        return {
            "action": "crystal_sphere_click_cell",
            "x": int(cell.get("x", 0)),
            "y": int(cell.get("y", 0)),
        }

    def _treasure_step(self, state: dict[str, Any]) -> dict[str, Any] | WaitForTransition:
        treasure = state.get("treasure") or {}
        if treasure.get("relics") and self._last_step != ("treasure", "claim"):
            self._last_step = ("treasure", "claim")
            return {"action": "claim_treasure_relic", "index": 0}
        if not treasure.get("can_proceed"):
            # Refused live at act 1 floor 10 of ssb-20260922T170535Z-0e43b211:
            # an empty chest with no exit offered yet is the reward drain still
            # resolving, not a screen to close.  ``proceed`` there is a refusal
            # the client answers, and the retry that follows is the same
            # decision -- so the screen says when it may be left.
            return WaitForTransition("the chest offers no relic and no exit yet")
        return {"action": "proceed"}

    def _bundle_step(self) -> dict[str, Any]:
        if self._last_step != ("bundle", "select"):
            self._last_step = ("bundle", "select")
            return {"action": "select_bundle", "index": 0}
        self._last_step = ("bundle", "confirm")
        return {"action": "confirm_bundle_selection"}

    @staticmethod
    def _event_choice(state: dict[str, Any]) -> dict[str, Any] | None:
        event = state.get("event") or {}
        options = [o for o in (event.get("options") or []) if isinstance(o, dict)]
        choosable = [o for o in options if o.get("is_locked") is not True]
        if not choosable:
            # Nothing on this screen can be picked yet.  An event whose options
            # are still animating looks exactly like this: PUNCH_OFF and
            # SLIPPERY_BRIDGE each exposed zero options for one poll and two the
            # next.  Posting index 0 into it is an action the state does not
            # offer, and the mod refuses it -- so the trace that claims "every
            # action was legal" cannot contain such a post.  Wait instead.
            return None
        # An Ancient's options *are* the run-defining boon, so leaving one is a
        # loss; a plain event's proceed is the offer to walk away.  The old
        # Neow-only "移除" branch is subsumed by the rule -- removal is an
        # upside marker for every act's Ancient, not just the first one's.
        is_ancient = bool(event.get("is_ancient"))
        return {
            "action": "choose_event_option",
            "index": choose(choosable, prefer_proceed=not is_ancient),
        }

    # ------------------------------------------------- combat route executor
    def _note_combat_progress(self, state: dict, screen_type: str) -> None:
        """Bound the wait on a fight the client owns, and say which half stopped moving.

        The signature is the whole state payload, not the round counter: a player turn in
        which block, statuses or hand size churn has not stalled, while a byte-identical
        state has stopped even if a round number ticks. The detail separates enemy from
        player movement because both halves moved in the Act-2 stall and only the
        card-deployment step stopped -- naming that is the difference between "the client
        is slow" and "the solver paused its own choice".
        """
        digest = hashlib.sha256(
            json.dumps(state, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        now = time.monotonic()
        if digest != self._combat_state_digest:
            if self._combat_state_digest is not None:
                self._combat_state_moved += 1
            self._combat_state_digest = digest
            self._combat_state_since = now
            self._combat_state_screen = screen_type
            return
        waited = now - self._combat_state_since
        if waited <= self._combat_progress_timeout:
            return
        battle = state.get("battle") or {}
        run_state = state.get("run") or {}
        player = state.get("player") or {}
        self._classified_stop(
            "combat_no_progress",
            (f"{screen_type} at act {run_state.get('act')} floor {run_state.get('floor')} "
             f"left the state unchanged for {waited:.0f}s (bound "
             f"{self._combat_progress_timeout:.0f}s; the observed p99 of identical-state "
             "combat runs is 14s over 13,539 samples) — "
             f"round {battle.get('round')} turn {battle.get('turn')}, "
             f"player hp {player.get('hp')}, enemies "
             f"{[(e.get('entity_id'), e.get('hp')) for e in battle.get('enemies', [])]}, "
             f"{self._combat_state_moved} state changes since this screen began"),
        )

    def _combat_tick(self, state: dict[str, Any]) -> str:
        """Advance one step of solver-route-driven combat (scan-execute).

        Each tick scans the route for the first action whose card is still in
        the current hand and plays it. Cards already played leave the hand, so
        a stale route is skipped naturally and transient unplayable cards are
        retried instead of being skipped (the old positional index drifted
        whenever the mod replanned, dropping one card per round).
        """
        if self._out_of_combat_only:
            # The mod/full-auto keeper owns combat in a supervised run.  Do
            # not even poll a route source here: this is a hard single-owner
            # boundary, not merely a policy preference.
            time.sleep(self.poll)
            return "combat-owner-mod"
        battle = state.get("battle") or {}
        round_no = battle.get("round")
        if battle.get("turn") != "player" or battle.get("is_play_phase") is not True \
                or not isinstance(round_no, int):
            if self._combat_round is not None:
                self._combat_round = None  # enemy turn / between rounds
            time.sleep(self.poll)
            return "enemy-turn"

        if self._combat_round != round_no:
            self._combat_round = round_no
            self._combat_anchor_mono = time.monotonic()
            self._combat_actions = ()
            self._combat_failed: set[int] = set()
            self._combat_attempts = 0
            self._combat_note = "anchor"

        if self._route_source is not None:
            for event in self._route_source.poll():
                snapshot = event.snapshot
                if snapshot is None or not snapshot.route:
                    continue
                candidates = [
                    a for step in snapshot.route if step.turn == round_no
                    for a in step.actions
                ]
                if candidates and candidates != self._combat_actions:
                    self._combat_actions = tuple(candidates)
                    self._combat_failed = set()
                    self._combat_note = f"route r{round_no}: {len(candidates)} actions"

        hand = (state.get("player") or {}).get("hand") or []
        picked = select_route_action(
            self._combat_actions, hand_multiset(hand), self._combat_failed
        )
        if picked is None:
            waited = time.monotonic() - self._combat_anchor_mono
            if not self._combat_actions and waited <= 330:
                time.sleep(0.5)
                return "waiting-for-route"
            if self._combat_actions and waited <= 20:
                # give the mod's divergence replan a moment to arrive before
                # ending the turn on a route that no longer matches the hand
                time.sleep(0.5)
                return "scanning"
            return self._post({"action": "end_turn"}, state, "end-turn/scan",
                              raises=False)

        index, action = picked
        payload = self._combat_payload(state, action)
        if payload is None:
            self._combat_failed.add(index)
            self._combat_note = f"skip unplayable {action.card_id}"
            return self._combat_tick(state)
        note = f"r{round_no} {payload['action']}"
        ok = self._post(payload, state, note, raises=False)
        if not ok:
            self._combat_attempts += 1
            if self._combat_attempts >= 3:
                self._combat_failed.add(index)
                self._combat_attempts = 0
            time.sleep(1.0)
            return f"post-failed {payload['action']}"
        self._combat_attempts = 0
        return note

    def _combat_payload(
        self, state: dict[str, Any], action: Any
    ) -> dict[str, Any] | None:
        if action.kind == "end_turn":
            return {"action": "end_turn"}
        player = state.get("player") or {}
        enemies = (state.get("battle") or {}).get("enemies") or []
        if action.kind == "play":
            card_id = action.card_id or ""
            hand = player.get("hand") or []
            index = hand_index_for(card_id, 0, hand)
            if index is None:
                return None
            payload: dict[str, Any] = {"action": "play_card", "card_index": index}
            entity = target_entity_id(enemies, action.target_index)
            if entity is not None:
                payload["target"] = entity
            return payload
        if action.kind == "potion":
            slot = potion_slot_for(action.card_id, player.get("potions") or [])
            if slot is None:
                return None
            payload = {"action": "use_potion", "slot": slot}
            entity = target_entity_id(enemies, action.target_index)
            if entity is not None:
                payload["target"] = entity
            return payload
        return None

    def note_post_against_state(self, state_type: str, decision_id: str | None) -> None:
        """Count an accepted post against the state read it was made from, and stop when it is stuck.

        A bridge that answers `ok` while the client never moves is the failure mode this repo has
        been bitten by twice (the 2026-10-07 infinite combat poll, the 2026-09-23 frozen menu), and
        both earlier guards look at *failed* attempts. Silence here would burn a whole batch posting
        into a modal, so the count is per state read, not per payload.
        """
        if not decision_id:
            return
        if decision_id != self._advance_id:
            self._advance_id = decision_id
            self._advance_posts = 0
        self._advance_posts += 1
        limit = (MAX_POSTS_PER_STATE_ON_MENU if state_type == "menu"
                 else MAX_POSTS_PER_STATE)
        if self._advance_posts > limit:
            self._classified_stop(
                "screen_not_advancing",
                f"{state_type} accepted {self._advance_posts} posts against the unchanged state "
                f"{decision_id} and never advanced")

    def note_action_accepted(self, state: dict[str, Any], payload: dict[str, Any]) -> None:
        """Remember that the client acknowledged the action which ends this screen.

        Called only after a POST returns without error. The bridge can keep reporting a screen for
        hundreds of milliseconds after its exit is accepted -- and the decision id does not always
        move when it does -- so an id comparison alone never told us the screen was gone.
        """
        action = str((payload or {}).get("action") or "")
        state_type = str(state.get("state_type") or "unknown")
        terminal = action in _TERMINAL_SCREEN_ACTIONS or (
            action == "menu_select" and state_type in {"menu", "game_over"})
        if terminal:
            self._accepted_exit = _screen_key(state)

    def _post(
        self,
        payload: dict[str, Any],
        state: dict[str, Any],
        note: str,
        raises: bool = True,
    ) -> str | bool:
        from bridge.trace_controller import decision_id

        try:
            self.controller.send_action(
                payload, expected_decision_id=decision_id(state)
            )
        except (BridgeProtocolError, ActionPermissionError, ValueError) as exc:
            if raises:
                raise
            print(f"[autoplay] post failed ({exc})", flush=True)
            return False
        self.actions_by_screen[state.get("state_type") or "combat"] = (
            self.actions_by_screen.get(state.get("state_type") or "combat", 0) + 1
        )
        self.consecutive_failures = 0
        self._combat_note = note
        time.sleep(1.2)  # let the play resolve before the next step
        return True

    # ---------------------------------------------------------- seed ledger
    @staticmethod
    def _identity_character(identity: dict[str, Any]) -> str:
        value = _first_present(identity.get("character_id"), identity.get("character"))
        token = str(value or "").strip().upper()
        if token in {"IRONCLAD", "THE IRONCLAD", "铁甲战士"}:
            return "IRONCLAD"
        return token.split(".")[-1]

    def _verified_current_identity(self, state: dict[str, Any]) -> dict[str, Any]:
        """Merge a live active state with the authoritative current save.

        This path is used only to recover a process which was restarted while
        a fixed-seed run was active.  It deliberately requires the same
        complete identity contract as a fresh seeded start; the current run is
        never inferred from a menu option or from a ledger position.
        """

        assert self.controller is not None
        compendium = self.controller.get_compendium(record=True)
        saved = _require_saved_run_identity(compendium)
        try:
            identity = merge_verified_run_identity(state, compendium)
        except BridgeProtocolError as exc:
            raise RunIdentityError(
                f"Refusing fixed-seed recovery: authoritative identity is incomplete ({exc})"
            ) from exc
        if self._identity_character(identity) != "IRONCLAD":
            raise RunIdentityError(
                "Refusing fixed-seed recovery: active run is not Ironclad"
            )
        if identity.get("ascension") != 10:
            raise RunIdentityError(
                "Refusing fixed-seed recovery: active run is not A10"
            )
        if str(saved.get("game_mode") or "").strip().upper() != "STANDARD":
            raise RunIdentityError(
                "Refusing fixed-seed recovery: saved run is not standard"
            )
        if state.get("is_multiplayer") is True or state.get("multiplayer") is True:
            raise RunIdentityError(
                "Refusing fixed-seed recovery: active state is multiplayer"
            )
        players = state.get("players")
        if isinstance(players, list) and len(players) != 1:
            raise RunIdentityError(
                "Refusing fixed-seed recovery: active state is not singleplayer"
            )
        identity["guard"] = "allocation_recovery"
        return identity

    def _reconcile_seed_state(self, state: dict[str, Any], state_type: str) -> None:
        """Reconcile one active reservation/current run at most once.

        An unresolved reservation is a deliberate hard stop.  In particular,
        a menu with no Continue entry cannot silently advance to the next seed
        after a crash between reservation and the start POST.
        """

        if self._seed_ledger is None or self._seed_identity_checked:
            return
        snapshot = self._seed_ledger.snapshot()
        active = snapshot.get("active")
        if state_type in {"menu", "game_over"}:
            if active is None:
                return
            assert self.controller is not None
            try:
                saved = _require_saved_run_identity(
                    self.controller.get_compendium(record=True)
                )
            except RunIdentityError as exc:
                # The compendium proving there is NO active or saved run means
                # the reserved run is gone (game restarted before the save
                # reached disk).  Roll the reservation back so the same seed
                # is reserved again; anything else stays a hard stop.
                if "does not confirm" in str(exc):
                    self._seed_ledger.abort_active_reservation(
                        "compendium proves the reserved run is gone"
                    )
                    return
                raise
            # The authoritative save is enough to reconcile the pre-POST
            # reservation.  Continue itself remains separately guarded below.
            self._seed_ledger.observe_current_run(saved)
            self._seed_identity_checked = True
            self._seed_continue_required = True
            return
        identity = self._verified_current_identity(state)
        if active is None:
            # A previous batch may have owned this run (already consumed) or
            # have been stopped between its start POST and its own
            # reconciliation (next entry).  observe_current_run validates the
            # consumed case; only an unconsumed next-entry run falls through
            # to adoption, and anything else fails closed.
            try:
                self._seed_ledger.observe_current_run(identity)
            except SeedAllocationError:
                self._seed_ledger.adopt_current_run(identity)
        else:
            self._seed_ledger.observe_current_run(identity)
        self._seed_identity_checked = True

    def _validate_seeded_continue(self, saved: dict[str, Any]) -> None:
        if self._seed_ledger is None:
            return
        # A Continue save is acceptable only when its seed is already consumed
        # (or is the exact unresolved active reservation being reconciled).
        self._seed_ledger.validate_saved_run(saved)
        self._seed_identity_checked = False

    def _started_identity(
        self,
        state: dict[str, Any],
        entry: SeedEntry,
    ) -> dict[str, Any]:
        """Obtain the bridge-verified identity for a just-started run."""

        assert self.controller is not None
        identity = getattr(self.controller, "last_run_identity", None)
        if not isinstance(identity, dict):
            # Compatibility fallback for a test/different controller that has
            # the same GET contract but predates ``last_run_identity``.
            try:
                identity = merge_verified_run_identity(
                    state,
                    self.controller.get_compendium(record=True),
                    requested_seed=entry.raw_seed,
                    canonical_seed=entry.canonical_seed,
                )
            except BridgeProtocolError as exc:
                raise RunIdentityError(
                    f"Seeded start did not expose a complete identity: {exc}"
                ) from exc
        else:
            identity = dict(identity)
        if self._identity_character(identity) != "IRONCLAD":
            raise RunIdentityError("Seeded start identity is not Ironclad")
        if identity.get("ascension") != 10:
            raise RunIdentityError("Seeded start identity is not A10")
        if str(identity.get("game_mode") or "").strip().upper() != "STANDARD":
            raise RunIdentityError("Seeded start identity is not standard")
        return identity

    # ------------------------------------------------------------- run
    def _write_session_end(self, detail: str | None = None) -> None:
        """Append the final summary to the trace so batches end auditable."""

        recorder = getattr(self.controller, "recorder", None)
        if recorder is None:
            return
        recorder.write(
            "session_end",
            {
                "summary": self.summary(),
                "detail": detail,
            },
        )

    def _classified_stop(self, reason: str, detail: str) -> None:
        """End the batch for a recorded reason; never swallow the cause."""

        self.stop_reason = reason
        self._write_session_end(detail)
        raise AutoplayClassifiedStop(reason, detail)

    def run(self) -> dict[str, Any]:
        assert self.controller is not None
        failures = 0
        last_fail_id: str | None = None
        # A protocol failure on the very first read must still be recordable, and
        # recording it reaches for the screen it happened on.
        state: dict[str, Any] = {}
        state_type = ""
        decision_id: str | None = None
        bridge_unavailable_since: float | None = None
        while self._actions_total() < self.max_actions and not self._batch_is_done():
            try:
                state, decision_id = self.controller.get_state()
            except BridgeConnectionError as exc:
                # Transport failure: no state can be read, so the only honest
                # retry is bounded waiting for the endpoint to come back.  The
                # supervisor's own probe normally ends the batch sooner when
                # the game is gone; this bound covers standalone runs.  Deep
                # Combat Solver searches can block the listener for a while,
                # so the default budget is generous (>= the 120s identity
                # window) and every retry re-attempts a fresh GET.
                now = time.monotonic()
                bridge_unavailable_since = bridge_unavailable_since or now
                print(f"bridge unavailable ({exc}); retrying", flush=True)
                if (
                    now - bridge_unavailable_since
                    >= self._bridge_unavailable_timeout_seconds
                ):
                    self._classified_stop(
                        "bridge_unavailable",
                        f"no readable state for "
                        f"{now - bridge_unavailable_since:.1f}s; last error: {exc}",
                    )
                time.sleep(self._bridge_backoff)
                continue
            except BridgeProtocolError as exc:
                # Protocol-level state read failure (HTTP error status or a
                # non-JSON body): bounded like any other failure, never the
                # old unbounded retry, and each retry re-reads fresh state.
                failures += 1
                print(f"state read failed ({exc}); retrying", flush=True)
                if failures > self._max_total_failures:
                    self._classified_stop(
                        "repeated_state_failures", f"last error: {exc}"
                    )
                time.sleep(self._failure_backoff)
                continue
            # A fresh readable state resets the bridge-unavailable window.
            bridge_unavailable_since = None
            state_type = str(state.get("state_type") or "unknown")
            self.coverage.observe(state)
            self.coverage.note_unknown_screen(state_type)
            try:
                self._reconcile_seed_state(state, state_type)
                if self._continued_run_guard is not None and state_type in {
                    "menu",
                    "game_over",
                }:
                    raise RunIdentityError(
                        "Refusing automated actions after continue: no active run state appeared"
                    )
                if self._continued_run_guard is not None and state_type not in {
                    "menu",
                    "game_over",
                }:
                    if not _character_is_readable(state):
                        # Unreadable is not the same as wrong.  Poll again under
                        # a deadline; never wait without one, and never act.
                        self._continue_identity_since = (
                            self._continue_identity_since or time.monotonic()
                        )
                        self._continue_identity_frames += 1
                        waited = time.monotonic() - self._continue_identity_since
                        if waited > self._continue_identity_timeout:
                            raise RunIdentityError(
                                "Refusing automated actions after continue: no state "
                                f"exposed a character within {waited:.0f}s across "
                                f"{self._continue_identity_frames} frames "
                                f"(last screen {state_type!r})"
                            )
                        time.sleep(self.poll)
                        continue
                    identity = _require_continued_run_identity(
                        state,
                        self._continued_run_guard,
                        self.controller.get_compendium(record=True),
                    )
                    if self.controller.recorder:
                        self.controller.recorder.write(
                            "run_identity", identity,
                            decision_id=decision_id,
                        )
                    self._continued_run_guard = None
                    self._continue_identity_since = None
                if state_type in {"menu", "game_over"}:
                    # leave the death screen; the next menu visit starts a run
                    if state_type == "game_over":
                        self.controller.send_action(
                            {"action": "menu_select", "option": "main_menu"},
                            expected_decision_id=decision_id,
                        )
                        self._seal_run("game_over")
                    else:
                        screen = str(state.get("menu_screen") or "")
                        if screen not in ("main", "singleplayer"):
                            # settings/compendium/etc: walk back toward the main menu
                            # -- but only along a door the menu itself lists.  This
                            # refused twice live in ssb-20260922T170535Z-0e43b211
                            # ("Back button not available") because the assumption
                            # "not a screen I recognise, therefore it has a back
                            # button" is not the same statement; the option list is
                            # the only evidence about the current screen.
                            if "back" in _menu_option_names(state):
                                self.controller.send_action(
                                    {"action": "menu_select", "option": "back"},
                                    expected_decision_id=decision_id,
                                )
                            else:
                                time.sleep(self.poll)
                        else:
                            options = _menu_option_names(state)
                            saved = None
                            if "continue" in options:
                                # A saved run may belong to another character,
                                # mode, or ascension.  Verify it before the
                                # POST, then verify the first resulting live
                                # state before permitting any actions.  Never
                                # abandon or overwrite a user save here.
                                try:
                                    saved = _require_saved_run_identity(
                                        self.controller.get_compendium(record=True)
                                    )
                                except RunIdentityError as exc:
                                    # Only a provably absent save makes the
                                    # continue option stale: the mod's option
                                    # list can still carry it (fresh install,
                                    # discarded save) and the click would be
                                    # unverifiable.  A REAL save with a bad
                                    # identity must keep failing closed.
                                    if (
                                        self._seed_continue_required
                                        or "does not confirm" not in str(exc)
                                    ):
                                        raise
                                    options = options - {"continue"}
                            if "continue" in options:
                                self._validate_seeded_continue(saved)
                                self.controller.send_action(
                                    {"action": "menu_select", "option": "continue"},
                                    expected_decision_id=decision_id,
                                )
                                self._continued_run_guard = saved
                                self._seed_continue_required = False
                            else:
                                if self._seed_continue_required:
                                    raise RunIdentityError(
                                        "Refusing fresh start: reconciled seed reservation "
                                        "requires the matching Continue action"
                                    )
                                if not self._room_left_to_start():
                                    # The quota is met, or a run is already owed.  Stop here rather
                                    # than embarking one more than this batch can drive -- that is
                                    # the parked run, and it blocks the seeded probe.
                                    break
                                state, decision_id = self._start_run(state_type)
                                continue
                else:
                    if state_type in _COMBAT_SCREEN_TYPES:
                        # combat: execute the solver's logged route over the
                        # bridge (the mod stays in advice mode; no UI toggles)
                        if self._out_of_combat_only or self._route_source is None:
                            # The client owns this fight, so there is nothing to post --
                            # but a wait without a bound is how a paused solver became an
                            # infinite poll on 2026-10-07.
                            self._note_combat_progress(state, state_type)
                            time.sleep(self.poll)
                            continue
                        self._combat_tick(state)
                        continue
                    if state_type != self._stall_screen:
                        self._stall_since = None
                        self._stall_screen = state_type
                    payload = self.decide(state)
                    if isinstance(payload, WaitForTransition):
                        # A held frame is neither a refused post nor a skipped
                        # screen: the client still reports the map it was asked
                        # to leave, and the only legal move is to wait for it.
                        # Bounded, because a hold that never resolves is exactly
                        # the hang the unhandled budget exists to catch.
                        run_state = state.get("run") or {}
                        self.coverage.note_wait_for_transition(payload.reason)
                        print(
                            f"[autoplay] holding: {payload.reason} "
                            f"at act {run_state.get('act')} floor {run_state.get('floor')}",
                            flush=True,
                        )
                        self._stall_since = self._stall_since or time.monotonic()
                        if time.monotonic() - self._stall_since > 300:
                            raise BridgeProtocolError(
                                f"stalled: {payload.reason} for over 5 minutes "
                                "— manual action required"
                            )
                        time.sleep(self.poll)
                        continue
                    if isinstance(payload, DeferredToCombat):
                        # Owned by the in-game solver, so there is nothing to post
                        # and no gap to report -- but still bounded.  Being booked
                        # as unhandled was the only thing that ever stopped a
                        # deferral that failed to resolve, and taking that label
                        # away without a budget would trade a false alarm for a
                        # silent spin.
                        run_state = state.get("run") or {}
                        print(
                            f"[autoplay] deferring {payload.screen_type} to the "
                            f"Combat Solver at act {run_state.get('act')} "
                            f"floor {run_state.get('floor')}",
                            flush=True,
                        )
                        self._stall_since = self._stall_since or time.monotonic()
                        if time.monotonic() - self._stall_since > 300:
                            raise BridgeProtocolError(
                                f"stalled: {payload.screen_type} left with the "
                                "Combat Solver for over 5 minutes — manual action "
                                "required"
                            )
                        time.sleep(self.poll)
                        continue
                    if payload is None:
                        # non-combat screens must always be decidable; an
                        # unsupported one would stall the batch silently
                        run_state = state.get("run") or {}
                        self.coverage.note_unhandled(state_type, run_state.get("act"),
                                                     run_state.get("floor"))
                        # "Stuck" means the same screen keeps coming back with
                        # nothing to do, so the count is of *identical* frames:
                        # an event whose options appear one poll later is not a
                        # missing handler, and a lifetime-per-screen tally would
                        # stop a long run on a transient frame.
                        identity = (state_type, decision_id)
                        if identity == self._unhandled_identity:
                            self._unhandled_counts[state_type] = (
                                self._unhandled_counts.get(state_type, 0) + 1
                            )
                        else:
                            self._unhandled_counts[state_type] = 1
                            self._unhandled_identity = identity
                        count = self._unhandled_counts[state_type]
                        if count > self._max_unhandled_per_screen:
                            # A screen with no rule and no continue control is a
                            # missing handler, not a slow moment.  Say so by name
                            # where the run is standing rather than idling on it.
                            self._classified_stop(
                                "unhandled_screen",
                                f"no rule and no continue control for screen "
                                f"{state_type!r} at act {run_state.get('act')} "
                                f"floor {run_state.get('floor')} after {count} frames",
                            )
                        self._stall_since = self._stall_since or time.monotonic()
                        if time.monotonic() - self._stall_since > 300:
                            raise BridgeProtocolError(
                                f"stalled: no rule for screen {state_type!r} "
                                "for over 5 minutes — manual action required"
                            )
                        time.sleep(self.poll)
                        continue
                    self._stall_since = None
                    # A mod-side silent no-op (e.g. a purchase the game
                    # refuses without an error response) otherwise loops the
                    # same POST forever: after three identical executions on
                    # the same decision id, fall back to the generic proceed
                    # exit instead of buying infinitely.
                    execution_key = (decision_id, json.dumps(payload, sort_keys=True))
                    if execution_key == self._last_executed:
                        self._repeat_execution_count += 1
                    else:
                        self._repeat_execution_count = 0
                    self._last_executed = execution_key
                    if self._repeat_execution_count >= 3:
                        if payload.get("action") != "proceed":
                            run = state.get("run") or {}
                            self.coverage.note_bypass(
                                state_type, run.get("act"), run.get("floor"),
                                reason="repeat_without_state_change",
                            )
                            print(
                                "[autoplay] repeating identical action without "
                                f"state change; falling back to proceed ({payload})",
                                flush=True,
                            )
                            payload = {"action": "proceed"}
                        if self._repeat_execution_count >= 6:
                            # The proceed escape is itself not advancing, so the
                            # screen is unmodelled rather than merely stubborn.
                            self._classified_stop(
                                "unmodelled_screen",
                                f"screen {state_type!r} repeated the same action "
                                f"{self._repeat_execution_count + 1} times and the "
                                "proceed escape did not change state",
                            )
                    self.controller.send_action(payload, expected_decision_id=decision_id)
                    self.note_action_accepted(state, payload)
                    self.note_post_against_state(state_type, decision_id)
                    if payload.get("action") == "choose_map_node":
                        # Only an acknowledged travel counts as committed: a
                        # refused one leaves the map genuinely open, and the
                        # next poll must be free to try again.
                        self._map_travel_committed = _map_position(state)
                        self._map_travel_payload = dict(payload)
                        self._travel_acked_at = self._clock()
                        self._travel_reposts = 0
                        self._stall_since = None
            except SeedAllocationExhausted:
                # Exhaustion is a clean, auditable stop.  Never fall through
                # to an unseeded/random start once the allocation is spent.
                self.stop_reason = "seed_allocation_exhausted"
                print("[autoplay] fixed seed allocation exhausted; stopping", flush=True)
                break
            except (SeedAllocationError, RunIdentityError):
                # Identity mismatch is a hard safety stop, not a transient
                # state/HTTP race.  Never retry it and never abandon the save.
                raise
            except (BridgeProtocolError, ActionPermissionError, ValueError) as exc:
                # game-boot and screen-transition races produce transient
                # failures; only a STREAK on the same state is fatal.  Every
                # retry re-reads fresh state at the loop head, and an
                # exhausted budget ends the batch as a classified stop that
                # names the failure class instead of a bare crash.
                if decision_id != last_fail_id:
                    self.consecutive_failures = 0
                last_fail_id = decision_id
                failures += 1
                self.consecutive_failures += 1
                if isinstance(exc, EmptyCandidateError):
                    # A screen the contract found nothing actionable on is a gap to
                    # read later, not a moment that passed: the retry that follows can
                    # well succeed, and without this the trace shows only the success.
                    run_state = state.get("run") or {}
                    self.coverage.note_empty_candidates(
                        state_type or "unknown",
                        run_state.get("act"),
                        run_state.get("floor"),
                        error=str(exc),
                    )
                print(f"action failed ({exc}); retrying", flush=True)
                if self.consecutive_failures > self._max_consecutive_failures:
                    self._classified_stop(
                        "stale_state",
                        f"no state change across {self.consecutive_failures} "
                        f"attempts on decision {last_fail_id!r}; last error: {exc}",
                    )
                if failures > self._max_total_failures:
                    self._classified_stop(
                        "repeated_failures", f"last error: {exc}"
                    )
                time.sleep(self._failure_backoff)
                continue
            self.actions_by_screen[state_type] = self.actions_by_screen.get(state_type, 0) + 1
            self.consecutive_failures = 0
            time.sleep(self.poll)
        if self.stop_reason is None and self._batch_is_done():
            # Named rather than left null: "the loop ended" and "the loop ended because the run
            # quota was collected at a free menu" are different statements, and only the second one
            # is the reason the parked-run bug was invisible for so long.
            self.stop_reason = "max_runs_collected"
        if self._seed_ledger is not None and self.stop_reason is None:
            # The loop can end exactly when max_runs/max_actions is reached,
            # without making one extra reserve call.  Preserve the explicit
            # fixed-allocation exhaustion reason in that boundary case.
            if self._seed_ledger.snapshot().get("exhausted") is True:
                self.stop_reason = "seed_allocation_exhausted"
        self._write_session_end()
        return self.summary()

    def _start_run(self, state_type: str) -> tuple[dict[str, Any], str]:
        assert self.controller is not None
        # A fresh start after a run that never reached its terminal screen means
        # that run ended by abandonment or crash, not by clear.
        self._seal_run("superseded_by_new_run")
        entry: SeedEntry | None = None
        if self._seed_ledger is not None:
            entry = self._seed_ledger.reserve_next()
            print(
                "[autoplay] starting fresh Ironclad A10 run "
                f"({state_type}) seed[{entry.index}]={entry.raw_seed!r}",
                flush=True,
            )
        else:
            print(f"[autoplay] starting a fresh Ironclad A10 run ({state_type})", flush=True)
        # Keep the reservation if any start/verification step fails.  A later
        # process must reconcile it against the actual current run rather than
        # retrying the same seed blindly.
        state, decision_id = self.controller.start_ironclad_a10(
            seed=entry.raw_seed if entry is not None else None,
            # A background-throttled game (30 fps when unfocused) transitions
            # far slower than the 10s per-wait default.
            timeout=30.0,
        )
        if entry is not None:
            identity = self._started_identity(state, entry)
            self._seed_ledger.finalize_started(entry, identity)
            self._seed_identity_checked = True
        self.runs_started += 1
        self._current_run_embarked = True
        self.consecutive_failures = 0
        time.sleep(self.poll)
        return state, decision_id

    def _actions_total(self) -> int:
        return sum(self.actions_by_screen.values())

    def _batch_is_done(self) -> bool:
        """True when the quota of *collected* runs is met and nothing is on screen.

        This is the overshoot fix. ``runs_started`` is charged the instant the client is told to
        begin, so gating the loop on it ended every batch one frame after its last embark -- a fresh
        act-1 run parked on screen with no automated actor left. Three batches on 2026-10-07 did
        exactly that, and a parked run is what a seeded embark cannot work around, because the probe
        needs the menu. So the quota is read against runs the batch actually collected, and a run
        still on screen (``coverage.started`` -- a frame that is a run, not the menu's act-1-with-
        nothing-behind-it) or merely just requested is always finished first.
        """
        return len(self.completed_runs) >= self.max_runs and not self._run_owed()

    def _run_owed(self) -> bool:
        """A run is on screen, or was just asked for and has not shown up yet."""
        return self.coverage.started or self._current_run_embarked

    def _room_left_to_start(self) -> bool:
        """Whether the batch may put another run on screen.

        Bounded two ways on purpose. Collected + in flight is the honest quota, but a client that
        answers an embark with the same menu would otherwise be re-embarked forever, so the number
        of embarks is capped at the quota as well -- the same bound the old loop had, except it is
        now read at the menu instead of at the top of the loop, where it cut a run off mid-flight.
        """
        if self.runs_started >= self.max_runs:
            return False
        return len(self.completed_runs) + (1 if self._run_owed() else 0) < self.max_runs

    def _seal_run(self, reason: str) -> None:
        """Close off the run being observed and begin accounting for the next."""

        coverage = self.coverage.coverage()
        if coverage["acts_seen"]:
            coverage["sealed_by"] = reason
            self.completed_runs.append(coverage)
            if not self._current_run_embarked:
                # A run the driver never started still ended up collected, so it uses the quota
                # exactly like an embark would.  This is the whole overshoot fix: without it the
                # batch embarks one run past what it can drive and leaves it parked on the client.
                self.runs_adopted += 1
                print(
                    "[autoplay] collected a run it never embarked; it uses one of the "
                    f"{self.max_runs} --max-runs",
                    flush=True,
                )
        self._current_run_embarked = False
        self.coverage = RunCoverage()
        self._bypass_counts.clear()
        self._unhandled_counts.clear()
        self._unhandled_identity = None
        self._card_select_screen = None
        self._card_select_picks = set()

    def summary(self) -> dict[str, Any]:
        result = {
            "runs_started": self.runs_started,
            # Named separately rather than folded into runs_started: a reader has to be able to see
            # that one of the collected runs came off the save rather than from an embark.
            "runs_adopted": self.runs_adopted,
            "actions_total": self._actions_total(),
            "actions_by_screen": dict(self.actions_by_screen),
            # The in-flight run is reported only once it is actually a run: the
            # menu reports act 1 with nothing behind it.
            "runs": [
                *self.completed_runs,
                *([self.coverage.coverage()] if self.coverage.started else []),
            ],
        }
        runs = result["runs"]
        # Kept separate on purpose: a batch can hold a run that cleared three
        # acts without holding one the bridge could certify as a win, and
        # collapsing the two would invent victory evidence nobody observed.
        result["runs_with_certified_clear"] = sum(
            1 for row in runs if row["run_complete"]
        )
        result["victory_evidence_available"] = any(
            row["outcome_source"] == "bridge_is_victory_flag" and row["outcome"] is True
            for row in runs
        )
        if self.stop_reason is not None:
            result["stop_reason"] = self.stop_reason
        if self._seed_ledger is not None:
            result["seed_allocation"] = self._seed_ledger.snapshot()
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:15526")
    parser.add_argument("--max-runs", type=int, default=30)
    parser.add_argument("--max-actions", type=int, default=3000)
    parser.add_argument("--poll", type=float, default=1.0)
    parser.add_argument(
        "--allow-actions",
        action="store_true",
        help="grant POST permission (without it the driver only prints decisions)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="poll and print decisions without any POST",
    )
    parser.add_argument("--trace", type=Path, default=None)
    parser.add_argument(
        "--lock-file",
        type=Path,
        default=PROJECT_ROOT / "config" / "live_version.lock.json",
        help="version lock used for the trace's installed-game/mod verification",
    )
    parser.add_argument(
        "--cohort",
        choices=("assisted", "no-sl"),
        default="assisted",
        help="acceptance cohort recorded in the trace session metadata",
    )
    parser.add_argument(
        "--seed-mode",
        choices=("observational", "fixed"),
        default="observational",
        help="seed evidence mode recorded in the trace session metadata",
    )
    parser.add_argument(
        "--seed-file",
        type=Path,
        default=None,
        help=(
            "pre-registered ordered seed allocation; required in fixed mode "
            "and never accepted in observational mode"
        ),
    )
    parser.add_argument(
        "--seed-ledger",
        type=Path,
        default=None,
        help=(
            "crash-safe allocation ledger; fixed mode requires this or "
            "--batch-dir for live actions"
        ),
    )
    parser.add_argument(
        "--batch-dir",
        type=Path,
        default=None,
        help=(
            "batch-local directory for the default fixed-seed ledger "
            "(seed_allocation.ledger.json)"
        ),
    )
    parser.add_argument(
        "--execution-owner",
        choices=("http_route_executor", "combat_solver_full_auto", "manual_player"),
        default=None,
        help=(
            "single action owner; defaults to http_route_executor, or "
            "combat_solver_full_auto with --out-of-combat-only"
        ),
    )
    parser.add_argument(
        "--fullauto-keeper-active",
        action="store_true",
        help="record the keeper as a watchdog (never as a second action owner)",
    )
    parser.add_argument(
        "--provenance",
        type=Path,
        default=None,
        help="optional supervisor JSON provenance object to copy into the trace",
    )
    parser.add_argument("--model-id", default=None)
    parser.add_argument("--checkpoint", type=Path, default=None)
    parser.add_argument("--data-manifest", type=Path, default=None)
    parser.add_argument("--emulator", type=Path, default=None)
    parser.add_argument(
        "--log-dir", default=None,
        help="game log dir for combat route execution (default: "
        "%%APPDATA%%\\SlayTheSpire2\\logs; pass '' to disable combat play)",
    )
    parser.add_argument(
        "--out-of-combat-only",
        action="store_true",
        help="never execute combat actions; Combat Solver/full-auto owns combat",
    )
    parser.add_argument(
        "--combat-progress-timeout",
        type=float,
        default=COMBAT_PROGRESS_TIMEOUT_SECONDS,
        help=(
            "seconds a client-owned fight may leave the state byte-identical before the "
            f"batch stops with combat_no_progress (default {COMBAT_PROGRESS_TIMEOUT_SECONDS}, "
            "derived from the observed 99th percentile of live identical-state combat runs)"
        ),
    )
    args = parser.parse_args(argv)

    allocation: SeedAllocation | None = None
    if args.seed_file is not None:
        if args.seed_mode != "fixed":
            raise ValueError("--seed-file is only valid with --seed-mode fixed")
        allocation = load_seed_allocation(args.seed_file)
    elif args.seed_mode == "fixed":
        raise ValueError("fixed seed mode requires --seed-file")
    if args.seed_ledger is not None and args.seed_file is None:
        raise ValueError("--seed-ledger requires --seed-file")
    if args.batch_dir is not None and args.seed_file is None:
        raise ValueError("--batch-dir requires --seed-file")
    batch_dir = (
        Path(args.batch_dir).expanduser().resolve()
        if args.batch_dir is not None
        else None
    )
    seed_ledger_path = (
        Path(args.seed_ledger).expanduser().resolve()
        if args.seed_ledger is not None
        else (
            batch_dir / "seed_allocation.ledger.json"
            if batch_dir is not None
            else None
        )
    )
    if (
        allocation is not None
        and args.allow_actions
        and not args.dry_run
        and seed_ledger_path is None
    ):
        raise ValueError(
            "fixed live autoplay requires --seed-ledger or --batch-dir; "
            "a shared allocation-sidecar ledger is unsafe"
        )

    lock = VersionLock.load(args.lock_file)
    observed_game = lock.verify_installed_game()
    observed_mods = lock.verify_live_mods()
    supplied_provenance = _read_provenance(args.provenance)
    execution_owner = args.execution_owner or (
        "combat_solver_full_auto"
        if args.out_of_combat_only
        else "http_route_executor"
    )
    if args.out_of_combat_only and execution_owner != "combat_solver_full_auto":
        raise ValueError(
            "--out-of-combat-only requires combat_solver_full_auto as the execution owner"
        )
    if not args.out_of_combat_only and execution_owner == "combat_solver_full_auto":
        raise ValueError(
            "combat_solver_full_auto requires --out-of-combat-only so HTTP combat "
            "actions cannot run beside the Mod"
        )
    checkpoint = args.checkpoint
    if checkpoint is None and isinstance(supplied_provenance.get("checkpoint"), str):
        checkpoint = Path(supplied_provenance["checkpoint"])
    data_manifest = args.data_manifest
    if data_manifest is None and isinstance(supplied_provenance.get("data_manifest"), str):
        data_manifest = Path(supplied_provenance["data_manifest"])
    emulator = args.emulator
    if emulator is None and isinstance(supplied_provenance.get("emulator"), str):
        emulator = Path(supplied_provenance["emulator"])
    model_id = args.model_id or supplied_provenance.get("model_id")
    model_metadata = dict(
        supplied_provenance.get("model_metadata")
        if isinstance(supplied_provenance.get("model_metadata"), dict)
        else {}
    )
    model_metadata.update(
        {
            "model_id": model_id,
            "checkpoint": str(checkpoint) if checkpoint else supplied_provenance.get("checkpoint"),
            "checkpoint_sha256": (
                _sha256_file(checkpoint)
                if checkpoint is not None
                else supplied_provenance.get("checkpoint_sha256")
            ),
            "data_manifest": str(data_manifest) if data_manifest else supplied_provenance.get("data_manifest"),
            "data_sha256": (
                _sha256_file(data_manifest)
                if data_manifest is not None
                else supplied_provenance.get("data_sha256")
            ),
            "emulator": str(emulator) if emulator else supplied_provenance.get("emulator"),
            "emulator_sha256": (
                _sha256_file(emulator)
                if emulator is not None
                else supplied_provenance.get("emulator_sha256")
            ),
            "git_head": supplied_provenance.get("git_head") or _git_head(),
            "lock_path": str(args.lock_file),
        }
    )

    recorder = None
    if args.trace:
        args.trace.parent.mkdir(parents=True, exist_ok=True)
        recorder = TraceRecorder(
            args.trace,
            metadata={
                "component": "autoplay",
                "cohort": args.cohort,
                "seed_mode": args.seed_mode,
                # Which out-of-combat pick rule produced this run's relic/event/
                # Ancient choices.  Without it a survival difference cannot be
                # attributed to a policy change instead of to luck.
                "live_choice_policy_version": LIVE_CHOICE_POLICY_VERSION,
                "seed_allocation": allocation.describe() if allocation else None,
                "seed_ledger": str(seed_ledger_path) if seed_ledger_path else None,
                "execution_owner": execution_owner,
                "execution_watchdogs": (
                    ["fullauto_keeper"] if args.fullauto_keeper_active else []
                ),
                "version_lock": lock.raw,
                "lock_path": str(args.lock_file),
                "observed_game": observed_game,
                "observed_mods": observed_mods,
                **model_metadata,
            },
        )

    controller = STS2MCPController(
        args.base_url,
        allow_actions=args.allow_actions and not args.dry_run,
        recorder=recorder,
    )
    health = controller.health()
    lock.verify_bridge_health(health)
    if recorder:
        recorder.write(
            "provenance",
            {
                "lock_path": str(args.lock_file),
                "game_observed": observed_game,
                "bridge_observed": health,
                "allowed_mods_observed": observed_mods,
                "execution_owner": execution_owner,
            },
        )
    route_source = None
    log_dir = args.log_dir if args.log_dir is not None else (
        str(Path.home() / "AppData" / "Roaming" / "SlayTheSpire2" / "logs")
    )
    if log_dir and not args.out_of_combat_only:
        from combat_solver.logformat import LogTailSource

        route_source = LogTailSource(
            Path(os.path.expandvars(log_dir)), mod_version=None
        )
    player = AutoPlayer(
        controller,
        max_runs=args.max_runs,
        max_actions=args.max_actions,
        poll=args.poll,
        route_source=route_source,
        out_of_combat_only=args.out_of_combat_only,
        combat_progress_timeout=args.combat_progress_timeout,
        # A preview is observational by definition: do not create or advance
        # the durable fixed-seed ledger until POST authority is granted.
        seed_file=(allocation.path if allocation is not None and args.allow_actions and not args.dry_run else None),
        seed_ledger=(seed_ledger_path if allocation is not None and args.allow_actions and not args.dry_run else None),
        batch_dir=(batch_dir if allocation is not None and args.allow_actions and not args.dry_run else None),
    )

    if args.dry_run or not args.allow_actions:
        # decision preview: poll a few screens, print what would be posted
        for _ in range(10):
            state, _ = controller.get_state()
            payload = player.decide(state)
            print(json.dumps({
                "state_type": state.get("state_type"),
                "would_post": payload,
            }, ensure_ascii=False))
            time.sleep(1.0)
        return 0

    print("[autoplay] driving out-of-combat screens; combat stays with the mod",
          flush=True)
    try:
        summary = player.run()
    except AutoplayClassifiedStop as exc:
        # A classified stop already wrote its session_end trace event and set
        # the summary stop_reason; end with a distinct exit code so a
        # supervisor can separate a deliberate bounded stop from a crash.
        print(f"[autoplay] stopping: {exc.reason} ({exc.detail})", flush=True)
        print(json.dumps(player.summary(), ensure_ascii=False))
        return EXIT_CLASSIFIED_STOP
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
