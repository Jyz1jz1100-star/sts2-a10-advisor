"""Tests for the combat route executor's mapping helpers."""
from __future__ import annotations

import unittest

from bridge.autoplay import (
    AutoPlayer,
    hand_index_for,
    potion_slot_for,
    target_entity_id,
)

from combat_solver.reader import SourceEvent
from tests.test_combat_solver_states import enemy, monster_state


def route_action(kind, card_id=None, target_index=None):
    from combat_solver.snapshot import RouteAction

    return RouteAction(kind=kind, card_id=card_id, target_index=target_index)


class HandIndexTests(unittest.TestCase):
    HAND = [
        {"id": "DEFEND", "can_play": True},
        {"id": "STRIKE", "can_play": True},
        {"id": "STRIKE", "can_play": True},
        {"id": "BASH", "can_play": False},
    ]

    def test_nth_occurrence(self) -> None:
        self.assertEqual(hand_index_for("STRIKE", 0, self.HAND), 1)
        self.assertEqual(hand_index_for("STRIKE", 1, self.HAND), 2)
        self.assertEqual(hand_index_for("DEFEND", 0, self.HAND), 0)

    def test_unplayable_excluded(self) -> None:
        self.assertEqual(hand_index_for("BASH", 0, self.HAND), None)

    def test_missing_card(self) -> None:
        self.assertEqual(hand_index_for("NOPE", 0, self.HAND), None)
        self.assertEqual(hand_index_for("STRIKE", 2, self.HAND), None)


class TargetTests(unittest.TestCase):
    def test_index_into_enemies(self) -> None:
        enemies = [enemy("E_0", 10), enemy("E_1", 10)]
        self.assertEqual(target_entity_id(enemies, 1), "E_1")
        self.assertIsNone(target_entity_id(enemies, None))
        self.assertIsNone(target_entity_id(enemies, -1))
        self.assertIsNone(target_entity_id(enemies, 5))  # dead/out-of-range


class PotionSlotTests(unittest.TestCase):
    def test_by_id_with_explicit_slot(self) -> None:
        potions = [{"id": "FIRE", "slot": 1}]
        self.assertEqual(potion_slot_for("FIRE", potions), 1)
        self.assertEqual(potion_slot_for("NOPE", potions), None)

    def test_fallback_position(self) -> None:
        self.assertEqual(potion_slot_for("FIRE", [{"potion_id": "FIRE"}]), 0)


class CombatPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.player = AutoPlayer(controller=None)

    def test_play_payload_maps_hand_and_target(self) -> None:
        state = monster_state(
            round_no=1,
            hand=[
                {"id": "BASH", "can_play": True},
                {"id": "STRIKE", "can_play": True},
            ],
            enemies=[enemy("JELLY_0", 29), enemy("JELLY_1", 27)],
        )
        payload = self.player._combat_payload(
            state, route_action("play", "STRIKE", target_index=1)
        )
        self.assertEqual(
            payload, {"action": "play_card", "card_index": 1, "target": "JELLY_1"}
        )

    def test_end_turn_payload(self) -> None:
        state = monster_state(round_no=1)
        self.assertEqual(
            self.player._combat_payload(state, route_action("end_turn")),
            {"action": "end_turn"},
        )

    def test_potion_payload_maps_slot(self) -> None:
        state = monster_state(round_no=1, potions=[{"id": "FIRE", "slot": 0}])
        payload = self.player._combat_payload(
            state, route_action("potion", "FIRE", target_index=None)
        )
        self.assertEqual(payload, {"action": "use_potion", "slot": 0})


class SelectRouteActionTests(unittest.TestCase):
    def test_plays_first_card_still_in_hand(self) -> None:
        from collections import Counter

        from bridge.autoplay import select_route_action

        actions = [
            route_action("play", "STRIKE", 0),
            route_action("play", "BASH", None),
            route_action("end_turn"),
        ]
        hand = Counter({"BASH": 1})  # STRIKE already played/gone
        index, action = select_route_action(actions, hand, set())
        self.assertEqual(index, 1)
        self.assertEqual(action.card_id, "BASH")

    def test_stale_route_falls_through_to_end_turn(self) -> None:
        from collections import Counter

        from bridge.autoplay import select_route_action

        actions = [route_action("play", "STRIKE", 0), route_action("end_turn")]
        self.assertEqual(
            select_route_action(actions, Counter(), set()),
            (1, actions[1]),
        )

    def test_failed_indices_are_skipped(self) -> None:
        from collections import Counter

        from bridge.autoplay import select_route_action

        actions = [route_action("play", "STRIKE", 0), route_action("play", "BASH", None)]
        index, action = select_route_action(
            actions, Counter({"STRIKE": 1, "BASH": 1}), {0}
        )
        self.assertEqual(index, 1)
        self.assertEqual(action.card_id, "BASH")

    def test_empty_route_waits(self) -> None:
        from collections import Counter

        from bridge.autoplay import select_route_action

        self.assertIsNone(select_route_action([], Counter(), set()))


class CombatTickTests(unittest.TestCase):
    """Full _combat_tick pass with fake controller + route source."""

    def test_tick_plays_route_action_without_error(self) -> None:
        from collections import Counter

        from bridge.autoplay import AutoPlayer
        from combat_solver.logformat import LogTailSource  # noqa: F401
        from combat_solver.snapshot import RouteAction, RouteStep
        from combat_solver.snapshot import snapshot_from_json
        from tests.test_combat_solver_contract import snapshot_payload

        state = monster_state(
            round_no=1,
            hp=60,
            energy=3,
            hand=[{"id": "STRIKE", "can_play": True}],
            enemies=[enemy("JELLY_0", 29)],
        )
        snap = snapshot_from_json(
            snapshot_payload(
                state_hash=None,
                battle_turn=1,
                route=[
                    {"turn": 1,
                     "actions": [
                         {"kind": "play", "card_id": "STRIKE", "target_index": 0},
                         {"kind": "end_turn"},
                     ],
                     "predicted_hp_lost": 2},
                ],
                predicted={"hp_end": 58},
            )
        )

        class _FakeController:
            def __init__(self):
                self.sent = []

            def send_action(self, payload, expected_decision_id=None, **kw):
                self.sent.append(payload)
                return {"status": "ok"}, None

        class _FakeSource:
            def poll(self):
                return [SourceEvent.of(snap)]

        from combat_solver.reader import SourceEvent
        player = AutoPlayer(controller=_FakeController(), route_source=_FakeSource())
        player._combat_tick(state)  # must not raise (regression: NameError)
        self.assertEqual(player.controller.sent[0]["action"], "play_card")
        tick_note = player._combat_tick(state)
        # second tick: confirm end-turn or continue; either way no crash
        self.assertIsInstance(tick_note, str)


if __name__ == "__main__":
    unittest.main()
