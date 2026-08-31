from __future__ import annotations

import json
import unittest
from typing import Any

from bridge.convert_traces import (
    RawSession,
    _result_from,
    convert_session,
    enumerate_legal_actions,
    is_verified_ironclad_a10,
    payload_to_action_id,
    visible_view,
)
from training.trace_contract import ValidationIssue, validate_record

PLAYER = {
    "character_id": "IRONCLAD",
    "character": "铁甲战士",
    "hp": 61,
    "max_hp": 80,
    "energy": 3,
    "block": 0,
    "gold": 99,
    "hand": [],
    "potions": [],
    "relics": [{"id": "BURNING_BLOOD", "name": "燃烧之血"}],
    "status": [],
    "draw_pile": [
        {"id": "DEFEND_IRONCLAD", "name": "防御", "is_upgraded": False},
        {"id": "BASH", "name": "使用", "is_upgraded": True},
    ],
    "draw_pile_count": 2,
    "discard_pile": [{"id": "STRIKE_IRONCLAD", "name": "打击"}],
    "discard_pile_count": 1,
    "exhaust_pile": [],
    "exhaust_pile_count": 0,
}
RUN = {"act": 1, "ascension": 10, "floor": 2}
ENEMY = {
    "entity_id": "TOADPOLE_0",
    "combat_id": 1,
    "name": "小蝌蚪",
    "hp": 22,
    "max_hp": 23,
    "block": 0,
    "status": [],
    "intents": [{"type": "Attack", "label": "8"}],
}


def combat_state(**overrides: Any) -> dict[str, Any]:
    state = {
        "state_type": "monster",
        "run": dict(RUN),
        "player": {
            **PLAYER,
            "hand": [
                {
                    "index": 0,
                    "id": "BASH",
                    "name": "使用",
                    "cost": "2",
                    "target_type": "AnyEnemy",
                    "can_play": True,
                    "is_upgraded": False,
                    "type": "Attack",
                },
                {
                    "index": 1,
                    "id": "DEFEND_IRONCLAD",
                    "name": "防御",
                    "cost": "1",
                    "target_type": "Self",
                    "can_play": True,
                    "is_upgraded": False,
                    "type": "Skill",
                },
                {
                    "index": 2,
                    "id": "ASCENDERS_BANE",
                    "name": "升天者的祸根",
                    "cost": "0",
                    "target_type": "AllEnemies",
                    "can_play": False,
                    "is_upgraded": False,
                    "type": "Attack",
                    "unplayable_reason": "不可打出：虚弱",
                },
            ],
        },
        "battle": {
            "round": 3,
            "turn": "player",
            "is_play_phase": True,
            "enemies": [ENEMY],
        },
    }
    state.update(overrides)
    return state


class VisibleFilterTests(unittest.TestCase):
    def test_draw_pile_order_is_scrubbed_to_public_counts(self) -> None:
        visible = visible_view(combat_state())
        pile = visible["player"]["draw_pile"]
        self.assertEqual(pile["count"], 2)
        self.assertEqual(pile["composition"], sorted(["DEFEND_IRONCLAD", "BASH+"]))
        self.assertNotIn("draw_pile_count", visible["player"])
        self.assertNotIn("discard_pile_count", visible["player"])
        # Deterministic: two runs over identical states produce identical JSON.
        self.assertEqual(
            json.dumps(visible, sort_keys=True),
            json.dumps(visible_view(combat_state()), sort_keys=True),
        )

    def test_message_field_is_dropped(self) -> None:
        state = combat_state(message="transient ui toast")
        self.assertNotIn("message", visible_view(state))


class LegalActionTests(unittest.TestCase):
    def test_combat_ids_round_trip_from_wire_payloads(self) -> None:
        actions = enumerate_legal_actions(combat_state())
        ids = {action["action_id"] for action in actions}
        self.assertIn("play:0:TOADPOLE_0", ids)  # targeted card
        self.assertIn("play:1", ids)  # self-target card
        self.assertNotIn("play:2", ids)  # can_play False
        self.assertIn("end_turn", ids)
        for action in actions:
            derived = payload_to_action_id(
                {"action": action["action_type"], **action.get("payload", {})},
                "monster",
            )
            self.assertEqual(derived, action["action_id"])

    def test_combat_potion_targets_enumerated(self) -> None:
        state = combat_state()
        state["player"]["potions"] = [
            {"slot": 0, "id": "FIRE_POTION", "target_type": "AnyEnemy", "can_use": True}
        ]
        ids = {a["action_id"] for a in enumerate_legal_actions(state)}
        self.assertIn("potion:0:TOADPOLE_0", ids)

    def test_map_and_event_and_card_select(self) -> None:
        map_state = {
            "state_type": "map",
            "run": RUN,
            "player": PLAYER,
            "map": {
                "next_options": [
                    {"index": 0, "col": 1, "row": 2, "type": "Monster"},
                    {"index": 1, "col": 3, "row": 2, "type": "RestSite"},
                ]
            },
        }
        ids = {a["action_id"] for a in enumerate_legal_actions(map_state)}
        self.assertEqual(ids, {"map:0", "map:1"})

        event_state = {
            "state_type": "event",
            "run": RUN,
            "player": PLAYER,
            "event": {
                "event_id": "NEOW",
                "in_dialogue": False,
                "options": [
                    {"index": 0, "title": "钓鱼竿", "is_locked": False},
                    {"index": 1, "title": "剪刀", "is_locked": False},
                    {"index": 2, "title": "未解锁", "is_locked": True},
                ],
            },
        }
        ids = {a["action_id"] for a in enumerate_legal_actions(event_state)}
        self.assertEqual(ids, {"event:0", "event:1"})

        dialogue = json.loads(json.dumps(event_state))
        dialogue["event"]["in_dialogue"] = True
        self.assertEqual(
            [a["action_id"] for a in enumerate_legal_actions(dialogue)],
            ["event:advance"],
        )

        select_state = {
            "state_type": "card_select",
            "run": RUN,
            "player": PLAYER,
            "card_select": {
                "cards": [{"index": 0, "id": "STRIKE_IRONCLAD"},
                          {"index": 1, "id": "DEFEND_IRONCLAD"}],
                "can_confirm": True,
                "can_cancel": False,
            },
        }
        ids = {a["action_id"] for a in enumerate_legal_actions(select_state)}
        self.assertEqual(ids, {"grid:card:0", "grid:card:1", "grid:confirm"})

    def test_shop_stocked_gate(self) -> None:
        state = {
            "state_type": "shop",
            "run": RUN,
            "player": PLAYER,
            "shop": {
                "items": [
                    {"index": 0, "category": "card", "is_stocked": True, "price": 50},
                    {"index": 1, "category": "relic", "is_stocked": False, "price": 150},
                ],
                "can_proceed": True,
            },
        }
        ids = {a["action_id"] for a in enumerate_legal_actions(state)}
        self.assertEqual(ids, {"shop:0", "proceed"})

    def test_rewards_proceed_only_when_allowed(self) -> None:
        state = {
            "state_type": "rewards",
            "run": RUN,
            "player": PLAYER,
            "rewards": {
                "items": [{"index": 0, "type": "gold", "gold_amount": 12}],
                "can_proceed": False,
            },
        }
        self.assertEqual(
            [a["action_id"] for a in enumerate_legal_actions(state)], ["reward:0"]
        )


class IroncladA10GateTests(unittest.TestCase):
    def test_gate(self) -> None:
        self.assertTrue(is_verified_ironclad_a10(combat_state()))
        wrong_asc = json.loads(json.dumps(combat_state()))
        wrong_asc["run"]["ascension"] = 9
        self.assertFalse(is_verified_ironclad_a10(wrong_asc))
        other_char = json.loads(json.dumps(combat_state()))
        other_char["player"]["character_id"] = "SILENT"
        other_char["player"]["character"] = "静默猎手"
        self.assertFalse(is_verified_ironclad_a10(other_char))


class SessionConversionTests(unittest.TestCase):
    @staticmethod
    def _session(events: list[dict[str, Any]]) -> RawSession:
        session = RawSession(path=__import__("pathlib").Path("mem.jsonl"))
        session.mode = "actions-enabled"
        session.observed_mods = ("STS2_MCP",)
        session.observed_game = {"version": "v0.111.0", "branch": "public-beta"}
        session.run_id = "ironclad:profile1:1667266914"
        session.seed = "SEED-7"
        session.events = events
        return session

    def test_clean_combat_action_becomes_valid_record(self) -> None:
        before = combat_state()
        after = combat_state()
        after["player"]["hand"] = after["player"]["hand"][1:]
        after["battle"]["enemies"] = []
        events = [
            {"event_type": "session", "raw": {}, "sequence": 0},
            {"event_type": "state", "raw": before, "decision_id": "d1", "sequence": 1},
            {"event_type": "action", "raw": {"action": "play_card", "card_index": 0,
                                             "target": "TOADPOLE_0"},
             "decision_id": "d1", "sequence": 2},
            {"event_type": "result", "raw": {"status": "ok", "message": "Playing '使用'"},
             "decision_id": "d1", "sequence": 3},
            {"event_type": "state", "raw": after, "decision_id": "d2", "sequence": 4},
        ]
        report_records = convert_session(self._session(events), split="train")
        self.assertEqual(len(report_records), 1)
        record = report_records[0]
        self.assertEqual(record["chosen_action"]["action_id"], "play:0:TOADPOLE_0")
        self.assertEqual(record["result"]["status"], "applied")
        self.assertEqual(record["result"]["next_decision_id"], "d2")
        self.assertEqual(record["seed"], "SEED-7")
        self.assertEqual(record["build"], "public-beta-v0.111.0")
        issues = validate_record(record)
        self.assertEqual([issue.render() for issue in issues], [])
        # hidden order must be absent anywhere in the serialized record
        blob = json.dumps(record, ensure_ascii=False)
        self.assertNotIn('"draw_pile": [{', blob)

    def test_illegal_chosen_action_is_dropped(self) -> None:
        events = [
            {"event_type": "state", "raw": combat_state(), "decision_id": "d1"},
            {"event_type": "action", "raw": {"action": "play_card", "card_index": 2},
             "decision_id": "d1", "sequence": 2},
            {"event_type": "result", "raw": {"status": "error", "message": "cannot be played"},
             "decision_id": "d1", "sequence": 3},
        ]
        session = self._session(events)
        from bridge.convert_traces import ConversionReport

        report = ConversionReport()
        records = convert_session(session, split="train", report=report)
        self.assertEqual(records, [])
        self.assertEqual(report.counters.get("actions_skipped_illegal"), 1)

    def test_error_result_stays_unobserved(self) -> None:
        events = [
            {"event_type": "state", "raw": combat_state(), "decision_id": "d1"},
            {"event_type": "action", "raw": {"action": "end_turn"}, "decision_id": "d1"},
            {"event_type": "result", "raw": {"status": "error", "message": "boom"},
             "decision_id": "d1"},
        ]
        records = convert_session(self._session(events), split="train")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["result"]["status"], "error")
        self.assertFalse(records[0]["result"]["observed"])

    def test_game_over_next_state_marks_terminal(self) -> None:
        after = {"state_type": "game_over", "game_over": {"options": ["main_menu"]},
                 "run": RUN, "player": PLAYER}
        events = [
            {"event_type": "state", "raw": combat_state(), "decision_id": "d1"},
            {"event_type": "action", "raw": {"action": "end_turn"}, "decision_id": "d1"},
            {"event_type": "result", "raw": {"status": "ok", "message": "Ending turn"},
             "decision_id": "d1"},
            {"event_type": "state", "raw": after, "decision_id": "d2"},
        ]
        records = convert_session(self._session(events), split="train")
        self.assertEqual(records[0]["result"]["status"], "terminal")

    def test_contaminated_session_is_excluded(self) -> None:
        session = self._session(
            [{"event_type": "state", "raw": combat_state(), "decision_id": "d1"}]
        )
        session.observed_mods = ("STS2_MCP", "Rewind")
        records = convert_session(session, split="train")
        self.assertEqual(records, [])

    def test_non_a10_session_is_excluded(self) -> None:
        state = combat_state()
        state["run"]["ascension"] = 3
        events = [
            {"event_type": "state", "raw": state, "decision_id": "d1"},
            {"event_type": "action", "raw": {"action": "end_turn"}, "decision_id": "d1"},
            {"event_type": "result", "raw": {"status": "ok", "message": "Ending turn"},
             "decision_id": "d1"},
        ]
        records = convert_session(self._session(events), split="train")
        self.assertEqual(records, [])


class ResultAttributionTests(unittest.TestCase):
    def test_delta_fields(self) -> None:
        before = combat_state()
        after = combat_state()
        after["player"]["hp"] = 55
        result = _result_from({"status": "ok", "message": "x"}, after, before)
        self.assertEqual(result["hp_delta"], -6)
        self.assertEqual(result["status"], "applied")
        self.assertTrue(result["observed"])


if __name__ == "__main__":
    unittest.main()
