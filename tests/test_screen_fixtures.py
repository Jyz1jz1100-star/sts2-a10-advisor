from __future__ import annotations

import json
import unittest
from pathlib import Path

from bridge.convert_traces import enumerate_legal_actions, visible_view

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "screens"

# Screens whose legal-action surface must be non-empty on this bridge version.
DECISION_SCREENS = {
    "monster",
    "elite",
    "boss",
    "hand_select",
    "rewards",
    "card_reward",
    "map",
    "event",
    "rest_site",
    "shop",
    "fake_merchant",
    "treasure",
    "card_select",
    "bundle_select",
    "relic_select",
    "crystal_sphere",
}

EXPECTED_ACTION_TYPES = {
    "monster": {"play_card", "use_potion", "end_turn"},
    "map": {"choose_map_node"},
    "event": {"choose_event_option"},
    "card_reward": {"select_card_reward", "skip_card_reward"},
    "rest_site": {"choose_rest_option"},
    "shop": {"shop_purchase", "proceed"},
    "treasure": {"claim_treasure_relic", "proceed"},
    "relic_select": {"select_relic", "skip_relic_selection"},
    "card_select": {"select_card", "confirm_selection", "cancel_selection"},
    "crystal_sphere": {
        "crystal_sphere_set_tool",
        "crystal_sphere_click_cell",
        "crystal_sphere_proceed",
    },
    "hand_select": {"combat_select_card", "combat_confirm_selection"},
}


def load_fixtures() -> list[dict]:
    fixtures = []
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        fixtures.append(json.loads(path.read_text(encoding="utf-8")))
    return fixtures


@unittest.skipUnless(FIXTURE_DIR.is_dir(), "run scripts/make_screen_fixtures.py first")
class ScreenFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixtures = load_fixtures()
        self.assertTrue(self.fixtures, "fixture directory is empty")

    def test_every_decision_screen_enumerates_unique_legal_actions(self) -> None:
        seen_screens = {fixture["screen"] for fixture in self.fixtures}
        missing = DECISION_SCREENS - seen_screens
        self.assertFalse(missing, f"screens without fixtures: {sorted(missing)}")
        for fixture in self.fixtures:
            if fixture["screen"] not in DECISION_SCREENS:
                continue
            actions = enumerate_legal_actions(fixture["state"])
            ids = [action["action_id"] for action in actions]
            self.assertTrue(actions, f"no actions for {fixture['screen']}")
            self.assertEqual(len(ids), len(set(ids)), f"dup ids in {fixture['screen']}")
            allowed = EXPECTED_ACTION_TYPES.get(fixture["screen"])
            if allowed:
                for action in actions:
                    self.assertIn(action["action_type"], allowed, fixture["screen"])

    def test_visible_view_is_serializable_and_scrubs_draw_pile(self) -> None:
        for fixture in self.fixtures:
            visible = visible_view(fixture["state"])
            blob = json.dumps(visible, ensure_ascii=False, sort_keys=True)
            player = visible.get("player")
            if isinstance(player, dict) and isinstance(player.get("draw_pile"), list):
                # ordered cards must have been collapsed into a public view
                for entry in player["draw_pile"]:
                    self.assertIsInstance(entry, str)
                self.assertEqual(
                    player["draw_pile"],
                    sorted(player["draw_pile"]),
                    f"draw pile leaked order in {fixture['screen']}",
                )
            self.assertIn('"state_type"', blob)

    def test_captured_fixtures_match_locked_build(self) -> None:
        for fixture in self.fixtures:
            if fixture.get("captured"):
                self.assertEqual(fixture.get("build"), "public-beta-v0.111.0")
                self.assertIn("live_traces", fixture["source"])


if __name__ == "__main__":
    unittest.main()
