"""Fixture-backed tests for the read-only STS2MCP live candidate codec."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from advisor_core.live_candidate_codec import (
    AmbiguousCandidateError,
    EmptyCandidateError,
    LiveCandidateContractError,
    MissingStateError,
    UnsupportedScreenError,
    extract_live_candidates,
)


FIXTURES = Path(__file__).parent / "fixtures" / "screens"


def load_envelope(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def load(name: str) -> dict:
    return load_envelope(name)["state"]


class LiveCandidateCodecFixtureTests(unittest.TestCase):
    def test_local_v040_fixtures_use_exact_wire_actions(self) -> None:
        map_set = extract_live_candidates(load("map"))
        self.assertEqual(map_set.family, "map")
        self.assertEqual(
            [candidate.wire_action for candidate in map_set],
            [
                {"action": "choose_map_node", "index": 0},
                {"action": "choose_map_node", "index": 1},
            ],
        )

        card_set = extract_live_candidates(load("card_reward"))
        self.assertEqual(card_set.family, "card_reward")
        self.assertEqual(
            [candidate.wire_action for candidate in card_set],
            [
                {"action": "select_card_reward", "card_index": 0},
                {"action": "select_card_reward", "card_index": 1},
                {"action": "skip_card_reward"},
            ],
        )

        shop_set = extract_live_candidates(load("shop"))
        self.assertEqual(
            [candidate.wire_action for candidate in shop_set],
            [
                {"action": "shop_purchase", "index": 0},
                {"action": "shop_purchase", "index": 8},
                {"action": "proceed"},
            ],
        )

        rest_set = extract_live_candidates(load("rest_site"))
        self.assertEqual(
            [candidate.wire_action for candidate in rest_set],
            [
                {"action": "choose_rest_option", "index": 0},
                {"action": "choose_rest_option", "index": 1},
            ],
        )

        # The C# source emits state_type=event for Neow.  The stable family is
        # derived from its visible event_id rather than an invented container.
        event_set = extract_live_candidates(load("event"))
        self.assertEqual(event_set.state_type, "event")
        self.assertEqual(event_set.family, "neow")
        self.assertEqual(
            [candidate.wire_action for candidate in event_set],
            [
                {"action": "choose_event_option", "index": 0},
                {"action": "choose_event_option", "index": 1},
                {"action": "choose_event_option", "index": 2},
            ],
        )

    def test_fixture_envelope_and_raw_state_have_contract_label(self) -> None:
        envelope = load_envelope("card_reward")
        from_envelope = extract_live_candidates(envelope)
        from_raw = extract_live_candidates(envelope["state"])
        self.assertEqual(from_envelope.to_dict(), from_raw.to_dict())
        self.assertEqual(from_envelope.build, "public-beta-v0.111.0")

    def test_explicit_neow_alias_requires_and_preserves_a_neow_container(self) -> None:
        state = load("event")
        state["state_type"] = "neow"
        state["neow"] = state.pop("event")
        candidate_set = extract_live_candidates(state)
        self.assertEqual(candidate_set.family, "neow")
        self.assertEqual(candidate_set.state_type, "neow")

        state["event"] = copy.deepcopy(state["neow"])
        with self.assertRaises(AmbiguousCandidateError):
            extract_live_candidates(state)

    def test_identity_and_wire_action_round_trip(self) -> None:
        candidates = extract_live_candidates(load("shop"))
        for candidate in candidates:
            self.assertEqual(candidates.candidate_for(candidate.identity), candidate)
            self.assertEqual(candidates.identity_for(candidate.wire_action), candidate.identity)
            self.assertEqual(candidates.decode(candidate.identity), candidate.wire_action)
            self.assertEqual(candidates.encode(candidate.wire_action), candidate.identity)

    def test_empty_arrays_are_allowed_only_when_a_real_transition_is_exposed(self) -> None:
        rest = load("rest_site")
        rest["rest_site"] = {"options": [], "can_proceed": True}
        self.assertEqual(
            [candidate.wire_action for candidate in extract_live_candidates(rest)],
            [{"action": "proceed"}],
        )

        shop = load("shop")
        shop["shop"] = {"items": [], "can_proceed": True}
        self.assertEqual(
            [candidate.wire_action for candidate in extract_live_candidates(shop)],
            [{"action": "proceed"}],
        )

    def test_stocked_shop_with_disabled_proceed_still_exposes_leave(self) -> None:
        # The mod ForceOpens the merchant inventory on entry, so the live
        # shop reports can_proceed=false while stocked items are shown; the
        # wire proceed action closes the inventory and leaves.
        shop = load("shop")
        shop["shop"]["can_proceed"] = False
        candidates = extract_live_candidates(shop)
        proceed = [c for c in candidates if c.identity == "shop:proceed"]
        self.assertEqual(len(proceed), 1)
        self.assertEqual(proceed[0].wire_action, {"action": "proceed"})
        self.assertEqual(proceed[0].features.get("can_proceed"), False)

    def test_empty_shop_with_disabled_proceed_stays_fail_closed(self) -> None:
        shop = load("shop")
        shop["shop"] = {"items": [], "can_proceed": False}
        with self.assertRaises(EmptyCandidateError):
            extract_live_candidates(shop)

    def test_unaffordable_stocked_shop_leaves_via_proceed(self) -> None:
        shop = load("shop")
        for item in shop["shop"]["items"]:
            item["can_afford"] = False
        shop["shop"]["can_proceed"] = False
        self.assertEqual(
            [candidate.wire_action for candidate in extract_live_candidates(shop)],
            [{"action": "proceed"}],
        )

    def test_empty_arrays_are_allowed_only_when_a_real_transition_is_exposed_part2(
        self,
    ) -> None:
        card_reward = load("card_reward")
        card_reward["card_reward"] = {"cards": [], "can_skip": True}
        self.assertEqual(
            [candidate.wire_action for candidate in extract_live_candidates(card_reward)],
            [{"action": "skip_card_reward"}],
        )

        map_state = load("map")
        map_state["map"]["next_options"] = []
        with self.assertRaises(EmptyCandidateError):
            extract_live_candidates(map_state)

        rest = load("rest_site")
        no_transition = copy.deepcopy(rest)
        no_transition["rest_site"]["options"] = []
        no_transition["rest_site"]["can_proceed"] = False
        with self.assertRaises(EmptyCandidateError):
            extract_live_candidates(no_transition)

    def test_real_boolean_flags_are_required_instead_of_defaulted(self) -> None:
        card_reward = load("card_reward")
        del card_reward["card_reward"]["can_skip"]
        with self.assertRaises(MissingStateError):
            extract_live_candidates(card_reward)

        rest = load("rest_site")
        del rest["rest_site"]["options"][0]["is_enabled"]
        with self.assertRaises(MissingStateError):
            extract_live_candidates(rest)

        event = load("event")
        del event["event"]["in_dialogue"]
        with self.assertRaises(MissingStateError):
            extract_live_candidates(event)

        event = load("event")
        del event["event"]["options"][0]["is_locked"]
        with self.assertRaises(MissingStateError):
            extract_live_candidates(event)

        event = load("event")
        del event["event"]["options"][0]["was_chosen"]
        with self.assertRaises(MissingStateError):
            extract_live_candidates(event)

        shop = load("shop")
        del shop["shop"]["can_proceed"]
        with self.assertRaises(MissingStateError):
            extract_live_candidates(shop)

    def test_duplicate_card_and_shop_offerings_are_distinguished_by_source_slot(self) -> None:
        card_reward = load("card_reward")
        first = copy.deepcopy(card_reward["card_reward"]["cards"][0])
        second = copy.deepcopy(first)
        second["index"] = 7
        card_reward["card_reward"]["cards"] = [first, second]
        card_set = extract_live_candidates(card_reward)
        self.assertEqual(
            [candidate.wire_action for candidate in card_set],
            [
                {"action": "select_card_reward", "card_index": 0},
                {"action": "select_card_reward", "card_index": 7},
                {"action": "skip_card_reward"},
            ],
        )
        self.assertNotEqual(card_set[0].identity, card_set[1].identity)

        shop = load("shop")
        first = copy.deepcopy(shop["shop"]["items"][0])
        second = copy.deepcopy(first)
        second["index"] = 4
        shop["shop"]["items"] = [first, second]
        shop_set = extract_live_candidates(shop)
        purchases = [candidate for candidate in shop_set if candidate.wire_action["action"] == "shop_purchase"]
        self.assertEqual([item.wire_action["index"] for item in purchases], [0, 4])
        self.assertNotEqual(purchases[0].identity, purchases[1].identity)

    def test_duplicate_source_indices_fail_closed(self) -> None:
        for name, container_key, list_key in (
            ("map", "map", "next_options"),
            ("card_reward", "card_reward", "cards"),
            ("shop", "shop", "items"),
            ("rest_site", "rest_site", "options"),
            ("event", "event", "options"),
        ):
            with self.subTest(name=name):
                state = load(name)
                values = state[container_key][list_key]
                duplicate = copy.deepcopy(values[0])
                duplicate["index"] = values[0]["index"]
                values.append(duplicate)
                with self.assertRaises(AmbiguousCandidateError):
                    extract_live_candidates(state)

    def test_disabled_and_unaffordable_entries_are_not_legal_candidates(self) -> None:
        rest = load("rest_site")
        rest["rest_site"]["options"][0]["is_enabled"] = False
        rest["rest_site"]["options"][1]["is_enabled"] = False
        with self.assertRaises(EmptyCandidateError):
            extract_live_candidates(rest)

        shop = load("shop")
        for item in shop["shop"]["items"]:
            item["can_afford"] = False
            item["is_stocked"] = True
        shop_set = extract_live_candidates(shop)
        self.assertEqual([item.wire_action for item in shop_set], [{"action": "proceed"}])

    def test_transitional_or_malformed_states_are_rejected(self) -> None:
        shop = load("shop")
        shop["shop"]["error"] = "Shop inventory is not ready yet"
        with self.assertRaises(MissingStateError):
            extract_live_candidates(shop)

        shop = load("shop")
        shop["shop"]["items"][0]["category"] = "unknown"
        with self.assertRaises(LiveCandidateContractError):
            extract_live_candidates(shop)

        card_reward = load("card_reward")
        card_reward["card_reward"]["cards"][0]["id"] = ""
        with self.assertRaises(MissingStateError):
            extract_live_candidates(card_reward)

        event = load("event")
        event["event"]["in_dialogue"] = True
        event["event"]["options"] = []
        with self.assertRaises(MissingStateError):
            extract_live_candidates(event)

        unsupported = load("map")
        unsupported["state_type"] = "monster"
        with self.assertRaises(UnsupportedScreenError):
            extract_live_candidates(unsupported)

        wrong_build = load_envelope("map")["state"]
        wrong_build["build"] = "public-beta-v0.110.0"
        with self.assertRaises(UnsupportedScreenError):
            extract_live_candidates(wrong_build)

    def test_nested_visible_allowlist_drops_unknown_fields(self) -> None:
        map_state = load("map")
        option = map_state["map"]["next_options"][0]
        option["secret"] = {"hidden": "do not retain"}
        option["leads_to"][0]["secret"] = {"hidden": "do not retain"}
        map_candidate = extract_live_candidates(map_state)[0]
        self.assertNotIn("secret", map_candidate.features)
        self.assertNotIn("secret", map_candidate.features["leads_to"][0])

        card_state = load("card_reward")
        card = card_state["card_reward"]["cards"][0]
        card["secret"] = {"hidden": "do not retain"}
        card["keywords"] = [
            {"name": "Visible", "description": "Shown", "secret": "do not retain"}
        ]
        card_candidate = extract_live_candidates(card_state)[0]
        self.assertNotIn("secret", card_candidate.features)
        self.assertEqual(card_candidate.features["keywords"], [{"name": "Visible", "description": "Shown"}])

    def test_candidate_mappings_are_safe_from_mutation(self) -> None:
        candidate = extract_live_candidates(load("card_reward"))[0]
        action = candidate.wire_action
        action["card_index"] = 99
        features = candidate.features
        features["id"] = "tampered"
        features["keywords"].append({"name": "tampered"})
        self.assertEqual(candidate.wire_action["card_index"], 0)
        self.assertEqual(candidate.features["id"], "UPPERCUT")
        self.assertEqual(candidate.features["keywords"], [])

        serialized = candidate.to_dict()
        serialized["wire_action"]["card_index"] = 88
        self.assertEqual(candidate.wire_action["card_index"], 0)


if __name__ == "__main__":
    unittest.main()
