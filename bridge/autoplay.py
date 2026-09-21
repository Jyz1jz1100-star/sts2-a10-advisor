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
from advisor_core.live_choice_policy import choose  # noqa: E402
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
# card_select is NOT here: it doubles as the out-of-combat removal screen
# (no "battle" key) and the in-combat selection (battle key present), and
# _card_select_choice discriminates by exactly that.
_HEURISTIC_SCREENS = {"card_reward", "shop", "rest_site", "map"}


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
        seed_file: Path | str | None = None,
        seed_ledger: Path | str | None = None,
        batch_dir: Path | str | None = None,
        failure_backoff: float = 1.0,
        bridge_backoff: float = 2.0,
        bridge_unavailable_timeout_seconds: float = 180.0,
        max_consecutive_failures: int = 60,
        max_total_failures: int = 600,
    ):
        self.controller = controller
        self.policy = LiveHeuristicPolicy()
        self.max_runs = max_runs
        self.max_actions = max_actions
        self.poll = poll
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
        self._max_unhandled_per_screen = 10
        self.runs_started = 0
        self.consecutive_failures = 0
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
    def decide(self, state: dict[str, Any]) -> dict[str, Any] | None:
        """Return the wire payload for this screen, or None to leave it alone."""
        state_type = str(state.get("state_type") or "unknown")
        fresh = self._last_screen != state_type
        self._last_screen = state_type
        if state_type in _COMBAT_SCREEN_TYPES:
            return None  # the Combat Solver owns every combat screen
        event = state.get("event") or {}
        if state_type == "event" and event.get("in_dialogue"):
            # post-choice dialogue: advance until real options return
            return {"action": "advance_dialogue"}
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
        return None

    def _card_select_choice(self, state: dict[str, Any]) -> dict[str, Any] | None:
        """Out-of-combat card selection (Neow/event/shop/removal): toggling UI.

        ``select_card`` TOGGLES the highlight, then ``confirm_selection``
        commits it — ``can_confirm`` in the state is the truth signal.  Some
        events require SEVERAL picks ("选择2张普通牌...") before ``can_confirm``
        lights up, and the exposed state carries no per-card selected flag, so
        the driver walks a deterministic pick sequence instead of re-toggling
        the same card forever.  In-combat card selections carry a ``battle``
        key and belong to the Combat Solver.
        """
        if state.get("battle") is not None:
            return None
        selection = state.get("card_select") or {}
        # ``screen_type`` is the only honest discriminator here.  The bridge has
        # no action path for combat-pile selection at all (select_card covers just
        # the grid and choose-a-card overlays), and that prompt carries no
        # ``battle`` key, so keying off ``battle`` alone made the driver POST an
        # action the mod can only refuse.  Combat-owned prompts are left to the
        # Combat Solver, and the abstention is recorded rather than dropped.
        serviceable = {"select", "simple_select", "transform", "upgrade", "choose", "bundle"}
        screen_type = str(selection.get("screen_type") or "")
        if screen_type and screen_type not in serviceable:
            self.coverage.note_deferred_to_combat(screen_type)
            return None
        if selection.get("can_confirm"):
            self._last_step = None
            return {"action": "confirm_selection"}
        cards = [
            c for c in selection.get("cards") or [] if isinstance(c, dict)
        ]
        if not cards:
            return None
        pick = next(
            (c for c in cards if str(c.get("id") or "").startswith("STRIKE")),
            cards[0],
        )
        chosen = int(pick.get("index", 0))
        prompt = str(selection.get("prompt") or "")
        count_match = re.search(r"(\d+)\s*张", prompt)
        required = int(count_match.group(1)) if count_match else 1
        if required <= 1:
            return {"action": "select_card", "index": chosen}
        step = (
            self._last_step
            if isinstance(self._last_step, tuple)
            and self._last_step[0] == "card_select"
            else None
        )
        if step is None:
            self._last_step = ("card_select", chosen)
            return {"action": "select_card", "index": chosen}
        alt = next(
            (c for c in cards if int(c.get("index", -1)) != step[1]),
            None,
        )
        self._last_step = None
        if alt is None:
            return {"action": "select_card", "index": chosen}
        return {"action": "select_card", "index": int(alt.get("index", 0))}

    def _treasure_step(self, state: dict[str, Any]) -> dict[str, Any]:
        treasure = state.get("treasure") or {}
        if treasure.get("relics") and self._last_step != ("treasure", "claim"):
            self._last_step = ("treasure", "claim")
            return {"action": "claim_treasure_relic", "index": 0}
        return {"action": "proceed"}

    def _bundle_step(self) -> dict[str, Any]:
        if self._last_step != ("bundle", "select"):
            self._last_step = ("bundle", "select")
            return {"action": "select_bundle", "index": 0}
        self._last_step = ("bundle", "confirm")
        return {"action": "confirm_bundle_selection"}

    @staticmethod
    def _event_choice(state: dict[str, Any]) -> dict[str, Any]:
        event = state.get("event") or {}
        options = [
            o for o in (event.get("options") or [])
            if isinstance(o, dict) and o.get("is_locked") is not True
        ]
        if not options:
            return {"action": "choose_event_option", "index": 0}
        # An Ancient's options *are* the run-defining boon, so leaving one is a
        # loss; a plain event's proceed is the offer to walk away.  The old
        # Neow-only "移除" branch is subsumed by the rule -- removal is an
        # upside marker for every act's Ancient, not just the first one's.
        is_ancient = bool(event.get("is_ancient"))
        return {
            "action": "choose_event_option",
            "index": choose(options, prefer_proceed=not is_ancient),
        }

    # ------------------------------------------------- combat route executor
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
        bridge_unavailable_since: float | None = None
        while self.runs_started < self.max_runs and self._actions_total() < self.max_actions:
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
                            self.controller.send_action(
                                {"action": "menu_select", "option": "back"},
                                expected_decision_id=decision_id,
                            )
                        else:
                            options = set(state.get("options") or [])
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
                                state, decision_id = self._start_run(state_type)
                                continue
                else:
                    if state_type in _COMBAT_SCREEN_TYPES:
                        # combat: execute the solver's logged route over the
                        # bridge (the mod stays in advice mode; no UI toggles)
                        if self._out_of_combat_only or self._route_source is None:
                            time.sleep(self.poll)
                            continue
                        self._combat_tick(state)
                        continue
                    if state_type != self._stall_screen:
                        self._stall_since = None
                        self._stall_screen = state_type
                    payload = self.decide(state)
                    if payload is None:
                        # non-combat screens must always be decidable; an
                        # unsupported one would stall the batch silently
                        run_state = state.get("run") or {}
                        self.coverage.note_unhandled(state_type, run_state.get("act"),
                                                     run_state.get("floor"))
                        self._unhandled_counts[state_type] = (
                            self._unhandled_counts.get(state_type, 0) + 1
                        )
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
        self.consecutive_failures = 0
        time.sleep(self.poll)
        return state, decision_id

    def _actions_total(self) -> int:
        return sum(self.actions_by_screen.values())

    def _seal_run(self, reason: str) -> None:
        """Close off the run being observed and begin accounting for the next."""

        coverage = self.coverage.coverage()
        if coverage["acts_seen"]:
            coverage["sealed_by"] = reason
            self.completed_runs.append(coverage)
        self.coverage = RunCoverage()
        self._bypass_counts.clear()
        self._unhandled_counts.clear()

    def summary(self) -> dict[str, Any]:
        result = {
            "runs_started": self.runs_started,
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
