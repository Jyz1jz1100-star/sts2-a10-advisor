from __future__ import annotations

import unittest

from advisor_core.explain import render_zh
from advisor_core.legal_actions import combat_actions, decision_actions
from advisor_core.policy import SmokeBaselinePolicy
from training.wilson import wilson_interval


class CoreTests(unittest.TestCase):
    def test_combat_actions_respect_playability_and_targets(self) -> None:
        state = {
            "player": {
                "hand": [
                    {"name": "重击", "can_play": True, "target_type": "AnyEnemy"},
                    {"name": "防御", "can_play": True, "target_type": "Self"},
                    {"name": "打击", "can_play": False, "target_type": "AnyEnemy"},
                ],
                "potions": [],
            },
            "battle": {
                "enemies": [
                    {"entity_id": "SLIME_0", "name": "史莱姆", "hp": 12}
                ]
            },
        }
        actions = combat_actions(state)
        self.assertEqual([label for _, label in actions], ["打出重击 → 史莱姆", "打出防御", "结束回合"])

    def test_reward_includes_skip(self) -> None:
        state = {
            "state_type": "card_reward",
            "card_reward": {"cards": [{"index": 0, "name": "燃烧契约"}], "can_skip": True},
        }
        self.assertEqual(len(decision_actions(state)), 2)

    def test_baseline_is_explicitly_untrained(self) -> None:
        state = {
            "state_type": "map",
            "map": {"next_options": [{"index": 0, "type": "Monster"}]},
        }
        text = render_zh(SmokeBaselinePolicy().recommend(state))
        self.assertIn("尚未经过策略训练", text)
        self.assertIn("不得计入 A10 胜率", text)

    def test_wilson_interval(self) -> None:
        low, high = wilson_interval(100, 200)
        self.assertLess(low, 0.5)
        self.assertGreater(high, 0.5)
        self.assertAlmostEqual(low, 0.4314, places=3)


if __name__ == "__main__":
    unittest.main()
