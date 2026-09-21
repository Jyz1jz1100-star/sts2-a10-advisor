"""Tests for the autonomous out-of-combat driver's decision logic."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from bridge.autoplay import (
    AutoPlayer,
    AutoplayClassifiedStop,
    RunIdentityError,
    WaitForTransition,
    _require_continued_run_identity,
    _require_saved_run_identity,
    _to_payload,
)
from bridge.seed_allocation import load_seed_allocation
from bridge.trace_controller import (
    BridgeConnectionError,
    BridgeProtocolError,
    TraceRecorder,
)
from advisor_core.live_candidate_codec import LiveCandidateContractError

FIXTURES = Path(__file__).parent / "fixtures" / "screens"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["state"]


def player() -> AutoPlayer:
    return AutoPlayer(controller=None)  # decide() never touches the controller


class AutoplayDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.player = player()

    def test_combat_screens_are_left_to_the_mod(self) -> None:
        for name in ("monster", "elite", "boss", "hand_select"):
            self.assertIsNone(self.player.decide(load(name)), name)

    def test_out_of_combat_only_never_reads_route_or_posts_in_combat(self) -> None:
        class RouteBomb:
            def poll(self):
                raise AssertionError("out-of-combat-only must not poll combat routes")

        player = AutoPlayer(
            controller=None,
            route_source=RouteBomb(),
            out_of_combat_only=True,
            poll=0,
        )
        self.assertEqual(player._combat_tick(load("monster")), "combat-owner-mod")

    def test_run_guard_skips_combat_even_when_route_is_injected(self) -> None:
        class Controller:
            recorder = None

            def get_state(self):
                return load("monster"), "combat-1"

        class GuardedPlayer(AutoPlayer):
            def __init__(self):
                super().__init__(
                    Controller(),
                    max_runs=1,
                    max_actions=1,
                    poll=0,
                    route_source=object(),
                    out_of_combat_only=True,
                )
                self._ticks = 0

            def _actions_total(self):
                self._ticks += 1
                return 0 if self._ticks == 1 else 1

            def _combat_tick(self, _state):
                raise AssertionError("combat owner guard must bypass _combat_tick")

        self.assertEqual(GuardedPlayer().run()["actions_total"], 1)

    def test_to_payload_maps_to_wire_names(self) -> None:
        self.assertEqual(
            _to_payload({"type": "map_choose_node", "index": 3}),
            {"action": "choose_map_node", "index": 3},
        )
        self.assertEqual(
            _to_payload({"type": "rewards_pick_card", "index": 2}),
            {"action": "select_card_reward", "card_index": 2},
        )
        self.assertEqual(
            _to_payload({"type": "rewards_skip"}),
            {"action": "skip_card_reward"},
        )
        # Keep the pre-policy spelling accepted for recorded/replayed advice.
        self.assertEqual(
            _to_payload({"type": "rewards_skip_card"}),
            {"action": "skip_card_reward"},
        )
        self.assertEqual(
            _to_payload({"type": "combat_play_card", "card": 1, "target": "E_0"}),
            {"action": "play_card", "card_index": 1, "target": "E_0"},
        )
        self.assertEqual(
            _to_payload({"type": "combat_end_turn"}),
            {"action": "end_turn"},
        )

    def test_card_reward_payload_uses_wire_format(self) -> None:
        payload = self.player.decide(load("card_reward"))
        self.assertIsNotNone(payload)
        self.assertEqual(payload["action"], "select_card_reward")
        self.assertIn("card_index", payload)

    def test_map_payload_uses_wire_format(self) -> None:
        payload = self.player.decide(load("map"))
        self.assertIsNotNone(payload)
        self.assertEqual(payload["action"], "choose_map_node")

    def test_shop_can_leave(self) -> None:
        state = load("shop")
        state["player"] = {"gold": 0}
        for item in state["shop"]["items"]:
            item["can_afford"] = False
        payload = self.player.decide(state)
        self.assertEqual(payload, {"action": "proceed"})

    def test_an_event_that_offers_nothing_yet_is_waited_out(self) -> None:
        """Zero options is not a reason to post index 0.

        Observed on the real client 2026-09-21: PUNCH_OFF and SLIPPERY_BRIDGE
        each reported an event with no options for one poll and two the next,
        and the driver's blind ``choose_event_option index 0`` came back
        ``No event options available``.  A trace that has to certify "every
        action was legal" cannot contain a post the state did not offer.
        """
        state = load("event")
        state["event"]["options"] = []
        self.assertIsNone(self.player.decide(state))
        state["event"]["options"] = [
            {"index": 0, "title": "顺从", "is_locked": True},
            {"index": 1, "title": "我能打倒他们", "is_locked": True},
        ]
        self.assertIsNone(self.player.decide(state))

    def test_neow_prefers_card_removal(self) -> None:
        state = load("event")  # fixture is the NEOW screen
        payload = self.player.decide(state)
        self.assertEqual(payload["action"], "choose_event_option")
        # the 移除 option is index 1 in the fixture
        self.assertEqual(payload["index"], 1)

    def test_the_same_rule_applies_whatever_the_event_id_says(self) -> None:
        """The pick used to be special-cased on ``event_id == "NEOW"``.

        Reading wording instead of identity means Act 2's and Act 3's Ancients
        get the same treatment, and no run can be answered by name.
        """
        named = load("event")
        renamed = load("event")
        renamed["event"]["event_id"] = "SOME_OTHER_EVENT"
        self.assertEqual(
            self.player.decide(named), self.player.decide(renamed)
        )

    def test_a_rest_site_that_cannot_choose_yet_is_waited_out(self) -> None:
        """``can_choose`` is the bridge's own precondition for the choice.

        8 of the 80 ``choose_rest_option`` posts recorded on this machine came
        back ``Rest site room is not open``: the options come from the room
        model, the click needs the room's UI node, and the second arrives a
        moment after the first.  An older bridge sends no flag, and then the
        driver must keep acting, so only an explicit false holds.
        """
        state = load("rest_site")
        state["rest_site"]["can_choose"] = False
        decision = self.player.decide(state)
        self.assertIsInstance(decision, WaitForTransition)
        state["rest_site"].pop("can_choose")
        self.assertEqual(
            self.player.decide({**state, "player": {"hp": 20, "max_hp": 80}}),
            {"action": "choose_rest_option", "index": 0},
        )

    def test_rest_site_payload_uses_option_index(self) -> None:
        state = load("rest_site")
        state["player"] = {"hp": 20, "max_hp": 80}
        payload = self.player.decide(state)
        self.assertEqual(payload, {"action": "choose_rest_option", "index": 0})

    def test_card_select_two_step_flow_is_state_driven(self) -> None:
        state = load("card_select")
        state.pop("battle", None)
        # fresh screen: highlight a basic strike first
        self.assertEqual(
            self.player.decide(state), {"action": "select_card", "index": 0}
        )
        # once can_confirm is true, commit — regardless of call history
        state["card_select"]["can_confirm"] = True
        self.assertEqual(
            self.player.decide(state), {"action": "confirm_selection"}
        )

    def test_in_combat_card_select_belongs_to_the_mod(self) -> None:
        state = load("card_select")
        state["battle"] = {"round": 3}
        self.assertIsNone(self.player.decide(state))

    def test_combat_pile_card_select_is_named_not_guessed(self) -> None:
        """The one card-select screen the build's auto-player does not drive."""
        state = load("card_select")
        state["card_select"]["screen_type"] = "NCombatPileCardSelectScreen"
        state["card_select"]["can_cancel"] = False
        self.assertIsNone(self.player.decide(state))
        self.assertEqual(
            self.player.coverage.deferred_to_combat,
            {"NCombatPileCardSelectScreen": 1},
        )

    def test_an_unnamed_out_of_combat_grid_is_still_ours(self) -> None:
        """A class name is not evidence of combat ownership.

        Observed on the real client 2026-09-21: ``NDeckEnchantSelectScreen`` was
        read as "unrecognised, so the Combat Solver owns it" and sat unattended
        for 11 polls, which stopped the batch.  Nothing in combat serves an
        out-of-combat grid, so abstention on an unknown name is a deadlock, not a
        hand-off.
        """
        state = load("card_select")
        state["card_select"]["screen_type"] = "NDeckEnchantSelectScreen"
        state["card_select"]["can_cancel"] = False
        self.assertEqual(
            self.player.decide(state), {"action": "select_card", "index": 0}
        )
        self.assertEqual(self.player.coverage.deferred_to_combat, {})
        state["card_select"]["can_confirm"] = True
        self.assertEqual(self.player.decide(state), {"action": "confirm_selection"})

    def test_card_select_walks_distinct_indices_until_the_screen_can_confirm(
        self
    ) -> None:
        """``select_card`` toggles, so re-clicking one card can never complete a
        multi-pick screen.  The screen's own ``can_confirm`` ends the walk."""
        state = load("card_select")
        state["card_select"]["can_cancel"] = False
        picks = [self.player.decide(state)["index"] for _ in range(3)]
        self.assertEqual(picks, [0, 1, 2])
        state["card_select"]["can_confirm"] = True
        self.assertEqual(self.player.decide(state), {"action": "confirm_selection"})
        # A new screen instance restarts the walk.
        state["card_select"]["can_confirm"] = False
        state["run"]["floor"] = 9
        self.assertEqual(
            self.player.decide(state), {"action": "select_card", "index": 0}
        )

    def test_card_select_exhausted_without_confirming_is_left_unhandled(
        self
    ) -> None:
        state = load("card_select")
        state["card_select"]["can_cancel"] = False
        for _ in range(len(state["card_select"]["cards"])):
            self.player.decide(state)
        self.assertIsNone(self.player.decide(state))
        self.assertEqual(self.player.coverage.deferred_to_combat, {})

    def test_rewards_claim_card_opens_card_reward_and_pick_proceeds(self) -> None:
        state = {
            "state_type": "rewards",
            "rewards": {
                "can_proceed": True,
                "items": [
                    {"index": 0, "type": "gold", "description": "13金币", "gold_amount": 13},
                    {"index": 1, "type": "card", "description": "将一张牌添加到你的牌组。"},
                ],
            },
        }
        self.assertEqual(
            self.player.decide(state), {"action": "claim_reward", "index": 0}
        )
        # The live StateBuilder removes the claimed button and re-numbers the
        # remaining enabled reward; the card is now index 0.
        state_after_gold = {
            "state_type": "rewards",
            "rewards": {
                "can_proceed": True,
                "items": [{"index": 0, "type": "card", "description": "将一张牌添加到你的牌组。"}],
            },
        }
        self.assertEqual(
            self.player.decide(state_after_gold),
            {"action": "claim_reward", "index": 0},
        )

        card_reward = load("card_reward")
        self.assertEqual(
            self.player.decide(card_reward),
            {"action": "select_card_reward", "card_index": 1},
        )
        # After selecting the card, the reward screen has no remaining items
        # and exposes its enabled proceed button.
        state_after_pick = {
            "state_type": "rewards",
            "rewards": {"can_proceed": True, "items": []},
        }
        self.assertEqual(self.player.decide(state_after_pick), {"action": "proceed"})

    def test_card_reward_skip_returns_to_rewards_then_proceeds(self) -> None:
        state = load("card_reward")
        state["card_reward"]["cards"] = [
            {
                "index": 0,
                "id": "BASH",
                "name": "Bash",
                "is_upgraded": False,
                "type": "Attack",
                "rarity": "Basic",
            },
            {
                "index": 1,
                "id": "ASCENDERS_BANE",
                "name": "Ascender's Bane",
                "is_upgraded": False,
                "type": "Curse",
                "rarity": "Curse",
            },
        ]
        self.assertEqual(
            self.player.decide(state), {"action": "skip_card_reward"}
        )
        state_after_skip = {
            "state_type": "rewards",
            "rewards": {"can_proceed": True, "items": []},
        }
        self.assertEqual(self.player.decide(state_after_skip), {"action": "proceed"})

    def test_rewards_without_items_waits_when_proceed_is_disabled(self) -> None:
        state = {
            "state_type": "rewards",
            "rewards": {"can_proceed": False, "items": []},
        }
        self.assertIsNone(self.player.decide(state))

    def test_malformed_shop_does_not_fall_back_to_proceed(self) -> None:
        state = load("shop")
        state["shop"]["error"] = "Shop inventory is not ready yet"
        with self.assertRaises(LiveCandidateContractError):
            self.player.decide(state)

    def test_duplicate_shop_indexes_do_not_fall_back_to_proceed(self) -> None:
        state = load("shop")
        duplicate = dict(state["shop"]["items"][0])
        duplicate["index"] = state["shop"]["items"][0]["index"]
        state["shop"]["items"].append(duplicate)
        with self.assertRaises(LiveCandidateContractError):
            self.player.decide(state)

    def test_wrong_build_does_not_fall_back_to_proceed(self) -> None:
        state = load("shop")
        state["build"] = "public-beta-v0.110.0"
        with self.assertRaises(LiveCandidateContractError):
            self.player.decide(state)

    def test_rest_continue_screen_presses_proceed(self) -> None:
        # after the rest choice is applied the options vanish and the screen
        # offers can_proceed (the "继续" button the user had to click by hand)
        state = load("rest_site")
        state["rest_site"] = {"can_proceed": True, "options": []}
        self.assertEqual(self.player.decide(state), {"action": "proceed"})

    def test_event_dialogue_advances(self) -> None:
        state = load("event")
        state["event"]["in_dialogue"] = True
        state["event"]["options"] = []
        self.assertEqual(self.player.decide(state), {"action": "advance_dialogue"})

    def test_treasure_claims_then_proceeds(self) -> None:
        state = load("treasure")
        first = self.player.decide(state)
        self.assertEqual(first, {"action": "claim_treasure_relic", "index": 0})
        second = self.player.decide(state)  # same screen after the claim
        self.assertEqual(second, {"action": "proceed"})

    def test_bundle_select_then_confirm(self) -> None:
        state = load("bundle_select")
        self.assertEqual(
            self.player.decide(state), {"action": "select_bundle", "index": 0}
        )
        self.assertEqual(
            self.player.decide(state), {"action": "confirm_bundle_selection"}
        )

    def test_relic_select_prefers_the_option_with_no_visible_cost(self) -> None:
        """Was `index: 0` unconditionally; now the rule, and it is explainable.

        On this screen slot 0 drips out relics slowly while slot 1 is a per-combat
        potion with no wording cost, so the rule takes 1 -- a decision a constant
        could not have made, and one a reader can check against the fixture.
        """
        payload = self.player.decide(load("relic_select"))
        self.assertEqual(payload["action"], "select_relic")
        self.assertEqual(payload["index"], 1)

    def test_heuristic_shop_payload_maps_to_purchase(self) -> None:
        state = load("shop")
        state["player"] = {"gold": 500}
        state["shop"]["items"][3]["is_stocked"] = True
        payload = self.player.decide(state)
        self.assertEqual(payload, {"action": "shop_purchase", "index": 10})


class AutoplayRunIdentityTests(unittest.TestCase):
    def _saved(self, **overrides: object) -> dict:
        current = {
            "is_in_progress": True,
            "game_mode": "standard",
            "ascension": 10,
            "run_id": "modded:profile1:run-1",
        }
        current.update(overrides)
        return {"current_run": current}

    def _saved_file(self, **overrides: object) -> dict:
        saved = {
            "is_saved": True,
            "game_mode": "standard",
            "ascension": 10,
            "run_id": "modded:profile1:run-1",
        }
        saved.update(overrides)
        return {"current_run": None, "saved_run": saved}

    def _active(self, character_id: str = "CHARACTER.IRONCLAD", ascension: int = 10) -> dict:
        return {
            "state_type": "monster",
            "run": {"act": 1, "floor": 2, "ascension": ascension},
            "player": {"character_id": character_id, "hp": 80},
        }

    def test_continue_requires_machine_verified_standard_a10_save(self) -> None:
        self.assertEqual(
            _require_saved_run_identity(self._saved()),
            {
                "is_in_progress": True,
                "game_mode": "standard",
                "ascension": 10,
                "run_id": "modded:profile1:run-1",
                "seed": None,
                "save_scope": None,
                "is_multiplayer": None,
                "singleplayer_verified": True,
            },
        )
        for overrides in (
            {"game_mode": "daily"},
            {"game_mode": None},
            {"ascension": 9},
            {"is_in_progress": False},
            {"is_multiplayer": True},
            {"parse_error": "unreadable"},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(RunIdentityError):
                    _require_saved_run_identity(self._saved(**overrides))

    def test_continue_accepts_disk_saved_run_when_current_run_is_unloaded(self) -> None:
        identity = _require_saved_run_identity(
            self._saved_file(seed="2450ZAR9EF", save_scope="modded")
        )
        self.assertTrue(identity["is_saved"])
        self.assertIsNone(identity["is_in_progress"])
        self.assertEqual(identity["run_id"], "modded:profile1:run-1")
        self.assertEqual(identity["seed"], "2450ZAR9EF")
        self.assertEqual(identity["save_scope"], "modded")
        self.assertTrue(identity["singleplayer_verified"])

    def test_saved_run_parse_error_fails_before_continue(self) -> None:
        with self.assertRaises(RunIdentityError):
            _require_saved_run_identity(
                self._saved_file(
                    seed="2450ZAR9EF",
                    parse_error="current_run.save could not be parsed or read.",
                )
            )

    def test_saved_run_requires_run_id_and_seed_for_binding(self) -> None:
        for missing in ("run_id", "seed"):
            with self.subTest(missing=missing):
                saved = self._saved_file(seed="2450ZAR9EF")
                saved["saved_run"].pop(missing, None)
                with self.assertRaises(RunIdentityError):
                    _require_saved_run_identity(saved)

    def test_active_current_run_and_saved_run_conflict_fails_closed(self) -> None:
        compendium = self._saved_file(seed="2450ZAR9EF")
        compendium["current_run"] = {
            "is_in_progress": True,
            "game_mode": "daily",
            "ascension": 10,
            "run_id": "modded:profile1:active-run",
        }
        with self.assertRaises(RunIdentityError):
            _require_saved_run_identity(compendium)

    def test_continue_requires_ironclad_a10_live_state(self) -> None:
        saved = _require_saved_run_identity(self._saved())
        self.assertEqual(
            _require_continued_run_identity(self._active(), saved)["character_id"],
            "CHARACTER.IRONCLAD",
        )
        chinese_state = self._active("")
        chinese_state["player"] = {"character": "铁甲战士", "hp": 80}
        self.assertEqual(
            _require_continued_run_identity(chinese_state, saved)["character"],
            "铁甲战士",
        )
        for state in (
            self._active("CHARACTER.SILENT"),
            self._active("CHARACTER.IRONCLAD", ascension=9),
            {**self._active(), "players": [{}, {}]},
            {**self._active(), "is_multiplayer": True},
        ):
            with self.subTest(state=state):
                with self.assertRaises(RunIdentityError):
                    _require_continued_run_identity(state, saved)

    def test_continue_preserves_saved_run_id_and_seed_when_live_state_omits_them(self) -> None:
        saved = _require_saved_run_identity(
            self._saved(seed="2450ZAR9EF")
        )
        merged = _require_continued_run_identity(self._active(), saved)
        self.assertEqual(merged["run_id"], "modded:profile1:run-1")
        self.assertEqual(merged["seed"], "2450ZAR9EF")

    def test_continue_accepts_matching_active_compendium_identity(self) -> None:
        saved = _require_saved_run_identity(
            self._saved_file(seed="2450ZAR9EF")
        )
        identity = _require_continued_run_identity(
            self._active(),
            saved,
            {
                "current_run": {
                    "is_in_progress": True,
                    "game_mode": "standard",
                    "ascension": 10,
                    "run_id": "modded:profile1:run-1",
                    "seed": "2450ZAR9EF",
                },
                "saved_run": None,
            },
        )
        self.assertEqual(identity["run_id"], "modded:profile1:run-1")
        self.assertEqual(identity["seed"], "2450ZAR9EF")
        self.assertEqual(identity["guard"], "continue_post_state")

    def test_continue_rejects_missing_active_compendium_identity(self) -> None:
        saved = _require_saved_run_identity(
            self._saved_file(seed="2450ZAR9EF")
        )
        with self.assertRaises(RunIdentityError):
            _require_continued_run_identity(
                self._active(),
                saved,
                {"current_run": None, "saved_run": saved},
            )

    def test_continue_rejects_active_compendium_with_saved_fallback(self) -> None:
        saved = _require_saved_run_identity(
            self._saved_file(seed="2450ZAR9EF")
        )
        with self.assertRaises(RunIdentityError):
            _require_continued_run_identity(
                self._active(),
                saved,
                {
                    "current_run": {
                        "is_in_progress": True,
                        "game_mode": "standard",
                        "ascension": 10,
                        "run_id": "modded:profile1:run-1",
                        "seed": "2450ZAR9EF",
                    },
                    "saved_run": saved,
                },
            )

    def test_continue_rejects_conflicting_saved_and_live_identity(self) -> None:
        saved = _require_saved_run_identity(
            self._saved(seed="2450ZAR9EF")
        )
        conflicting = self._active()
        conflicting["run"]["run_id"] = "modded:profile1:other-run"
        conflicting["run"]["seed"] = "DIFFERENT"
        with self.assertRaises(RunIdentityError):
            _require_continued_run_identity(conflicting, saved)

        conflicting_mode = self._active()
        conflicting_mode["run"]["game_mode"] = "daily"
        with self.assertRaises(RunIdentityError):
            _require_continued_run_identity(conflicting_mode, saved)

    def test_invalid_saved_run_fails_before_continue_post(self) -> None:
        class FakeController:
            recorder = None

            def __init__(self) -> None:
                self.posts: list[dict] = []

            def get_state(self):
                return {
                    "state_type": "menu",
                    "menu_screen": "main",
                    "options": ["continue", "singleplayer"],
                }, "menu-1"

            def get_compendium(self, record: bool = True):
                return {
                    "current_run": {
                        "is_in_progress": True,
                        "game_mode": "daily",
                        "ascension": 10,
                    }
                }

            def send_action(self, payload, **kwargs):
                self.posts.append(payload)
                return {"status": "ok"}, None

        controller = FakeController()
        with self.assertRaises(RunIdentityError):
            AutoPlayer(controller, max_runs=1, max_actions=1, poll=0).run()
        self.assertEqual(controller.posts, [])

    def test_invalid_continued_run_fails_before_any_gameplay_post(self) -> None:
        class FakeController:
            recorder = None

            def __init__(self) -> None:
                self.posts: list[dict] = []
                self.states = [
                    ({
                        "state_type": "menu",
                        "menu_screen": "main",
                        "options": ["continue", "singleplayer"],
                    }, "menu-1"),
                    (self_state, "run-1"),
                ]

            def get_state(self):
                return self.states.pop(0)

            def get_compendium(self, record: bool = True):
                return {
                    "current_run": {
                        "is_in_progress": True,
                        "game_mode": "standard",
                        "ascension": 10,
                    }
                }

            def send_action(self, payload, **kwargs):
                self.posts.append(payload)
                return {"status": "ok"}, None

        self_state = self._active("CHARACTER.SILENT")
        controller = FakeController()
        with self.assertRaises(RunIdentityError):
            AutoPlayer(controller, max_runs=1, max_actions=2, poll=0).run()
        self.assertEqual(
            controller.posts,
            [{"action": "menu_select", "option": "continue"}],
        )

    def test_continue_rechecks_active_compendium_before_gameplay_post(self) -> None:
        class FakeController:
            recorder = None

            def __init__(self) -> None:
                self.posts: list[dict] = []
                self.states = [
                    ({
                        "state_type": "menu",
                        "menu_screen": "main",
                        "options": ["continue", "singleplayer"],
                    }, "menu-1"),
                    (self_state, "run-1"),
                ]
                self.compendia = [
                    {
                        "current_run": None,
                        "saved_run": {
                            "is_saved": True,
                            "game_mode": "standard",
                            "ascension": 10,
                            "run_id": "modded:profile1:run-1",
                            "seed": "2450ZAR9EF",
                        },
                    },
                    {
                        "current_run": {
                            "is_in_progress": True,
                            "game_mode": "standard",
                            "ascension": 10,
                            "run_id": "modded:profile1:other-run",
                            "seed": "DIFFERENT",
                        },
                        "saved_run": None,
                    },
                ]

            def get_state(self):
                return self.states.pop(0)

            def get_compendium(self, record: bool = True):
                return self.compendia.pop(0)

            def send_action(self, payload, **kwargs):
                self.posts.append(payload)
                return {"status": "ok"}, None

        self_state = self._active()
        controller = FakeController()
        with self.assertRaises(RunIdentityError):
            AutoPlayer(controller, max_runs=1, max_actions=2, poll=0).run()
        self.assertEqual(
            controller.posts,
            [{"action": "menu_select", "option": "continue"}],
        )


    def test_identity_less_transition_frame_does_not_refuse_a_real_ironclad_resume(self):
        """Observed on the real client 2026-09-21: an Ironclad A10 resume was refused.

        The first frame after ``continue`` was ``unknown`` at act 2 floor 18 with
        no ``player`` block at all, and absence was being read as "not Ironclad".
        Absence must be re-read under a deadline; a real conflict still refuses.
        """
        class FakeController:
            recorder = None

            def __init__(self, states, compendia):
                self._states = states
                self._compendia = compendia
                self._cursor = 0
                self.posts = []
                self.identity_events = []

            def get_state(self):
                index = min(self._cursor, len(self._states) - 1)
                self._cursor += 1
                return self._states[index], f"decision-{index}"

            def get_compendium(self, record=True):
                index = min(len(self.identity_events), len(self._compendia) - 1)
                self.identity_events.append(index)
                return self._compendia[index]

            def send_action(self, payload, **kwargs):
                self.posts.append(payload)
                return {"status": "ok"}, None

        transition = {"state_type": "unknown",
                      "run": {"act": 2, "floor": 18, "ascension": 10}}
        terminal = {"state_type": "game_over",
                    "run": {"act": 2, "floor": 19, "ascension": 10},
                    "player": {"character_id": "IRONCLAD", "hp": 0},
                    "game_over": {"message": "Run ended.", "is_victory": False}}
        controller = FakeController(
            [
                {"state_type": "menu", "menu_screen": "main",
                 "options": ["continue", "singleplayer"]},
                transition,
                transition,
                self._active(),
                terminal,
            ],
            [self._saved_file(seed="2450ZAR9EF"), self._saved(seed="2450ZAR9EF")],
        )
        player = AutoPlayer(controller, max_runs=1, max_actions=6, poll=0)
        player.run()

        self.assertIsNone(player._continued_run_guard)
        self.assertEqual(controller.posts[0], {"action": "menu_select", "option": "continue"})
        self.assertIn({"action": "menu_select", "option": "main_menu"}, controller.posts)
        # Two unreadable frames were waited through, not acted on and not refused.
        self.assertEqual(player._continue_identity_frames, 2)
        # The terminal was read from the flag, not from the message wording.
        runs = player.summary()["runs"]
        self.assertIs(runs[0]["outcome"], False)
        self.assertEqual(runs[0]["outcome_source"], "bridge_is_victory_flag")

    def test_a_resume_that_never_exposes_a_character_stops_under_a_deadline(self):
        """Waiting is bounded: an unreadable identity cannot stall the batch forever."""
        class FakeController:
            recorder = None

            def __init__(self):
                self._cursor = 0
                self.posts = []

            def get_state(self):
                self._cursor += 1
                if self._cursor == 1:
                    return ({"state_type": "menu", "menu_screen": "main",
                             "options": ["continue"]}, "d1")
                return ({"state_type": "unknown",
                         "run": {"act": 2, "floor": 18, "ascension": 10}}, f"d{self._cursor}")

            def get_compendium(self, record=True):
                return {"current_run": None, "saved_run": {
                    "is_saved": True, "game_mode": "standard", "ascension": 10,
                    "run_id": "modded:profile1:run-1", "seed": "2450ZAR9EF"}}

            def send_action(self, payload, **kwargs):
                self.posts.append(payload)
                return {"status": "ok"}, None

        controller = FakeController()
        player = AutoPlayer(controller, max_runs=1, max_actions=50, poll=0)
        player._continue_identity_timeout = -1.0  # expire on the first unreadable frame
        with self.assertRaises(RunIdentityError) as caught:
            player.run()
        self.assertIn("no state exposed a character", str(caught.exception))
        self.assertEqual(controller.posts, [{"action": "menu_select", "option": "continue"}])


class AutoplayClassifiedStopTests(unittest.TestCase):
    """Regressions for the ssb-20260906T102557Z-183d0b05 failure class.

    The live batch faced a frozen main menu (byte-identical options listing
    ``continue``/``abandon_run`` but no ``singleplayer``) while the compendium
    proved no saved run, and autoplay crash-looped 61 identical attempts
    before dying with a bare traceback.  These tests pin the classified,
    bounded behaviour that replaced it.
    """

    FROZEN_MENU = {
        "state_type": "menu",
        "menu_screen": "main",
        "options": [
            "continue",
            "abandon_run",
            "multiplayer",
            "compendium",
            "timeline",
            "settings",
            "quit",
        ],
    }

    def _frozen_menu_controller(self) -> Any:
        class FrozenMenuController:
            recorder = None

            def __init__(self) -> None:
                self.start_seeds: list[str | None] = []

            def get_state(self):
                return dict(self.MENU), "local-sha256:c07eab"

            def get_compendium(self, record: bool = True):
                return {"current_run": None}

            def start_ironclad_a10(self, **kwargs):
                self.start_seeds.append(kwargs.get("seed"))
                raise BridgeProtocolError(
                    "Singleplayer is not currently actionable"
                )

        controller = FrozenMenuController()
        controller.MENU = self.FROZEN_MENU
        return controller

    def test_frozen_menu_stops_classified_after_bounded_retries(self) -> None:
        controller = self._frozen_menu_controller()
        with self.assertRaises(AutoplayClassifiedStop) as ctx:
            AutoPlayer(
                controller,
                max_runs=1,
                max_actions=1,
                poll=0,
                failure_backoff=0,
            ).run()
        self.assertEqual(ctx.exception.reason, "stale_state")
        self.assertIn(
            "Singleplayer is not currently actionable", ctx.exception.detail
        )
        # 60 consecutive same-state failures are tolerated, the 61st ends the
        # batch: bounded, and every attempt re-read the same frozen state.
        self.assertEqual(len(controller.start_seeds), 61)

    def test_stale_menu_never_clicks_continue_or_abandon(self) -> None:
        controller = self._frozen_menu_controller()

        def no_posts(payload, **kwargs):  # pragma: no cover - must not run
            raise AssertionError(f"unexpected POST {payload}")

        controller.send_action = no_posts  # type: ignore[method-assign]
        with self.assertRaises(AutoplayClassifiedStop):
            AutoPlayer(
                controller,
                max_runs=1,
                max_actions=1,
                poll=0,
                failure_backoff=0,
            ).run()

    def test_failed_starts_never_consume_seed_allocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            allocation_path = Path(tmp) / "allocation.json"
            ledger_path = Path(tmp) / "seed_allocation.ledger.json"
            allocation_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "allocation_kind": "run_seed",
                        "seeds": [1600000000, 1600000001],
                    }
                ),
                encoding="utf-8",
            )
            load_seed_allocation(allocation_path)
            controller = self._frozen_menu_controller()
            player = AutoPlayer(
                controller,
                max_runs=1,
                max_actions=1,
                poll=0,
                failure_backoff=0,
                seed_file=allocation_path,
                seed_ledger=ledger_path,
            )
            with self.assertRaises(AutoplayClassifiedStop):
                player.run()
            snapshot = player._seed_ledger.snapshot()
            self.assertEqual(snapshot["consumed"], [])
            self.assertEqual(snapshot["next_index"], 0)
            # The same reserved seed is retried; no second seed is ever
            # touched and the reservation stays reconcilable for recovery.
            self.assertEqual(set(controller.start_seeds), {"1600000000"})
            self.assertEqual(snapshot["active"]["raw_seed"], "1600000000")

    def test_bridge_loss_stops_classified_within_budget(self) -> None:
        class GoneBridge:
            recorder = None

            def __init__(self) -> None:
                self.get_calls = 0

            def get_state(self):
                self.get_calls += 1
                raise BridgeConnectionError(
                    "GET http://127.0.0.1:15526/api/v1/singleplayer failed: "
                    "connection refused"
                )

        controller = GoneBridge()
        with self.assertRaises(AutoplayClassifiedStop) as ctx:
            AutoPlayer(
                controller,
                max_runs=1,
                max_actions=1,
                poll=0,
                bridge_backoff=0,
                bridge_unavailable_timeout_seconds=0,
            ).run()
        self.assertEqual(ctx.exception.reason, "bridge_unavailable")
        self.assertIn("connection refused", ctx.exception.detail)
        self.assertEqual(controller.get_calls, 1)

    def test_transient_bridge_failure_recovers(self) -> None:
        class FlakyBridge:
            recorder = None

            def __init__(self) -> None:
                self.get_calls = 0

            def get_state(self):
                self.get_calls += 1
                if self.get_calls <= 2:
                    raise BridgeConnectionError("transient blip")
                return {
                    "state_type": "menu",
                    "menu_screen": "main",
                    "options": ["singleplayer"],
                }, "menu-1"

            def start_ironclad_a10(self, **kwargs):
                return {"state_type": "monster"}, "run-1"

        summary = AutoPlayer(
            FlakyBridge(), max_runs=1, max_actions=1, poll=0, bridge_backoff=0
        ).run()
        self.assertEqual(summary["runs_started"], 1)

    def test_classified_stop_writes_session_end_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace_path = Path(tmp) / "autoplay_trace.jsonl"
            trace_recorder = TraceRecorder(
                trace_path, metadata={"component": "autoplay"}
            )

            class GoneBridge:
                recorder = trace_recorder

                def get_state(self):
                    raise BridgeConnectionError("gone")

            with self.assertRaises(AutoplayClassifiedStop):
                AutoPlayer(
                    GoneBridge(),
                    max_runs=1,
                    max_actions=1,
                    poll=0,
                    bridge_backoff=0,
                    bridge_unavailable_timeout_seconds=0,
                ).run()
            rows = [
                json.loads(line)
                for line in trace_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(rows[-1]["event_type"], "session_end")
            self.assertEqual(
                rows[-1]["raw"]["summary"]["stop_reason"], "bridge_unavailable"
            )

    def test_repeated_state_read_failures_stop_classified(self) -> None:
        class Http500Bridge:
            recorder = None

            def __init__(self) -> None:
                self.get_calls = 0

            def get_state(self):
                self.get_calls += 1
                raise BridgeProtocolError(
                    "GET http://127.0.0.1:15526/api/v1/singleplayer -> HTTP 500: boom"
                )

        controller = Http500Bridge()
        with self.assertRaises(AutoplayClassifiedStop) as ctx:
            AutoPlayer(
                controller,
                max_runs=1,
                max_actions=1,
                poll=0,
                failure_backoff=0,
                max_total_failures=3,
            ).run()
        self.assertEqual(ctx.exception.reason, "repeated_state_failures")
        self.assertEqual(controller.get_calls, 4)


if __name__ == "__main__":
    unittest.main()
