"""Tests for the live out-of-combat heuristic advisor, driven by the real
screen fixtures captured from the locked build."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from advisor_core.live_candidate_codec import EmptyCandidateError, MissingStateError
from advisor_core.contracts import Recommendation
from advisor_core.policy_live import LiveHeuristicPolicy
from bridge.autoplay import _to_payload

FIXTURES = Path(__file__).parent / "fixtures" / "screens"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["state"]


class LiveHeuristicPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = LiveHeuristicPolicy()

    def test_card_reward_prefers_rare_and_skips_common(self) -> None:
        rec = self.policy.recommend(load("card_reward"))
        self.assertIsInstance(rec, Recommendation)
        # fixture: UPPERCUT (Uncommon) + IRON_WIND (Rare) -> pick the Rare
        self.assertIn("铁风", rec.primary.label)
        self.assertGreaterEqual(rec.primary.score, 3.0)
        self.assertTrue(any("上勾拳" in c.label for c in rec.alternatives))

    def test_card_reward_skips_when_only_basics(self) -> None:
        state = load("card_reward")
        state["card_reward"]["cards"] = [
            {"id": "STRIKE_IRONCLAD", "name": "打击", "rarity": "Basic",
             "type": "Attack", "index": 0, "is_upgraded": False},
        ]
        rec = self.policy.recommend(state)
        self.assertEqual(rec.primary.action["type"], "rewards_skip")
        self.assertIn("跳过", rec.primary.label)

    def test_card_reward_picks_common_over_skip(self) -> None:
        state = load("card_reward")
        state["card_reward"]["cards"] = [
            {"id": "CLOTHESLINE", "name": "顺劈斩", "rarity": "Common",
             "type": "Attack", "index": 0, "is_upgraded": False},
        ]
        rec = self.policy.recommend(state)
        self.assertEqual(rec.primary.action["type"], "rewards_pick_card")
        self.assertIn("顺劈斩", rec.primary.label)

    def test_recommendation_actions_round_trip_through_codec_and_bridge(self) -> None:
        """Policy actions remain internal, but resolve to exact legal wire bodies."""

        for name in ("card_reward", "shop", "rest_site", "map"):
            with self.subTest(screen=name):
                state = load(name)
                recommendation = self.policy.recommend(state)
                for candidate in (recommendation.primary, *recommendation.alternatives):
                    wire = self.policy.wire_action_for(state, candidate.action)
                    self.assertEqual(_to_payload(candidate.action), wire)

        # The normal fixture has usable cards, so explicitly exercise the
        # historical heuristic skip action through the same bridge mapping.
        skip_state = load("card_reward")
        skip_state["card_reward"]["cards"] = [
            {
                "id": "STRIKE_IRONCLAD",
                "name": "打击",
                "rarity": "Basic",
                "type": "Attack",
                "index": 0,
                "is_upgraded": False,
            }
        ]
        skip_candidate = self.policy.recommend(skip_state).primary
        self.assertEqual(skip_candidate.action["type"], "rewards_skip")
        self.assertEqual(
            _to_payload(skip_candidate.action), {"action": "skip_card_reward"}
        )

    def test_card_reward_without_skip_fails_closed(self) -> None:
        state = load("card_reward")
        state["card_reward"] = {"cards": [], "can_skip": False}
        with self.assertRaises(EmptyCandidateError):
            self.policy.recommend(state)

    def test_malformed_card_reward_does_not_fall_back_to_a_guess(self) -> None:
        state = load("card_reward")
        del state["card_reward"]["cards"][0]["is_upgraded"]
        with self.assertRaises(MissingStateError):
            self.policy.recommend(state)

    def test_shop_prioritizes_removal_then_relic(self) -> None:
        state = load("shop")
        state["player"] = {"gold": 500}  # make everything affordable
        state["shop"]["items"][1]["can_afford"] = True  # relic affordable again
        state["shop"]["items"][3]["is_stocked"] = True  # card_removal back in stock
        rec = self.policy.recommend(state)
        self.assertEqual(rec.primary.action["type"], "shop_buy")
        self.assertIn("删卡", rec.primary.label)
        # the affordable relic is the next alternative
        self.assertTrue(any("金刚杵" in c.label for c in rec.alternatives))

    def test_shop_skips_sold_out_removal_and_picks_rare(self) -> None:
        # the fixture's card_removal is sold out (is_stocked=false)
        state = load("shop")
        state["player"] = {"gold": 500}
        rec = self.policy.recommend(state)
        self.assertIn("献祭", rec.primary.label)  # the Rare card OFFERING

    def test_shop_leaves_when_nothing_affordable(self) -> None:
        state = load("shop")
        state["player"] = {"gold": 0}
        for item in state["shop"]["items"]:
            item["can_afford"] = False
        rec = self.policy.recommend(state)
        self.assertEqual(rec.primary.action["type"], "shop_leave")

    def test_shop_cannot_leave_when_proceed_is_not_legal(self) -> None:
        state = load("shop")
        state["shop"]["can_proceed"] = False
        for item in state["shop"]["items"]:
            item["can_afford"] = False
        with self.assertRaises(EmptyCandidateError):
            self.policy.recommend(state)

    def test_rest_site_rests_when_low(self) -> None:
        state = load("rest_site")
        state["player"] = {"hp": 20, "max_hp": 80}
        rec = self.policy.recommend(state)
        self.assertEqual(rec.primary.action["index"], 0)
        self.assertIn("低于", rec.primary.facts[0])

    def test_rest_site_smiths_when_healthy(self) -> None:
        state = load("rest_site")
        state["player"] = {"hp": 75, "max_hp": 80}
        rec = self.policy.recommend(state)
        self.assertEqual(rec.primary.action["index"], 1)

    def test_rest_fallback_explains_disabled_rest_option(self) -> None:
        state = load("rest_site")
        state["player"] = {"hp": 20, "max_hp": 80}
        state["rest_site"]["options"][0]["is_enabled"] = False
        rec = self.policy.recommend(state)
        self.assertEqual(rec.primary.action, {"type": "rest_choose_option", "index": 1})
        self.assertIn("休息不可用", rec.primary.facts[0])
        self.assertNotIn("先回血", rec.primary.facts[0])

    def test_map_prefers_campfire_when_hurt(self) -> None:
        state = load("map")
        state["player"] = {"hp": 30, "max_hp": 80}
        state["map"]["next_options"] = [
            {"index": 0, "col": 1, "row": 2, "type": "Monster"},
            {"index": 1, "col": 3, "row": 2, "type": "Campfire"},
        ]
        rec = self.policy.recommend(state)
        self.assertEqual(rec.primary.action["index"], 1)
        self.assertIn("Campfire", rec.primary.label)

    def test_map_prefers_elite_when_healthy(self) -> None:
        state = load("map")
        state["player"] = {"hp": 78, "max_hp": 80}
        state["map"]["next_options"] = [
            {"index": 0, "col": 1, "row": 2, "type": "Monster"},
            {"index": 1, "col": 3, "row": 2, "type": "Elite"},
        ]
        rec = self.policy.recommend(state)
        self.assertEqual(rec.primary.action["index"], 1)

    def test_map_elite_avoided_when_hurt(self) -> None:
        state = load("map")
        state["player"] = {"hp": 25, "max_hp": 80}
        state["map"]["next_options"] = [
            {"index": 0, "col": 1, "row": 2, "type": "Monster"},
            {"index": 1, "col": 3, "row": 2, "type": "Elite"},
        ]
        rec = self.policy.recommend(state)
        self.assertEqual(rec.primary.action["index"], 0)

    def test_combat_screens_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.policy.recommend(load("monster"))

    def test_unsupported_screens_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.policy.recommend(load("event"))

    def test_event_candidates_are_available_without_inventing_event_strategy(self) -> None:
        candidate_set = self.policy.candidates(load("event"))
        self.assertEqual(candidate_set.family, "neow")
        with self.assertRaises(ValueError):
            self.policy.recommend(load("event"))


if __name__ == "__main__":
    unittest.main()
