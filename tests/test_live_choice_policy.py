"""The pick rule must be a rule, not a remembered answer.

These fail if the policy ever starts keying on a seed, a floor, or a specific
encounter, and fail if it can name a candidate the bridge did not advertise.
"""
from __future__ import annotations

import inspect
import re
import unittest

from advisor_core.live_choice_policy import (
    POLICY_VERSION,
    choose,
    score_option,
)


def option(index, text, **extra):
    title, _, description = text.partition("|")
    payload = {"index": index, "title": title.strip(), "description": description.strip()}
    payload.update(extra)
    return payload


class ChooseTests(unittest.TestCase):
    def test_prefers_the_option_without_a_visible_cost(self) -> None:
        picked = choose([
            option(0, "赐福 | 每回合失去1点生命"),
            option(1, "移除 | 移除一张牌"),
        ])
        self.assertEqual(picked, 1)

    def test_ties_fall_back_to_the_lowest_advertised_index(self) -> None:
        # The old behaviour was index 0; the rule must refine it, never relocate
        # the risk without saying so.
        self.assertEqual(choose([option(0, "甲|无用"), option(1, "乙|无用")]), 0)

    def test_locked_candidates_are_never_chosen(self) -> None:
        picked = choose([
            option(0, "移除 | 移除一张牌", is_locked=True),
            option(1, "升级 | 升级一张牌", is_locked=True),
            option(2, "金币 | 获得金币"),
        ])
        self.assertEqual(picked, 2)

    def test_plain_events_may_walk_away_but_ancients_must_not(self) -> None:
        offers = [option(0, "交易 | 获得遗物"), option(1, "离开", is_proceed=True)]
        self.assertEqual(choose(offers, prefer_proceed=True), 1)
        self.assertEqual(choose(offers, prefer_proceed=False), 0)

    def test_empty_candidates_degrade_to_the_previous_constant(self) -> None:
        self.assertEqual(choose([]), 0)

    def test_nothing_is_keyed_on_seed_floor_or_encounter(self) -> None:
        """The rule's whole input is the advertised candidates.

        A parameter named ``seed``/``floor``/``act`` or a lookup of one would let
        the policy encode an answer for a particular run, which is what this has
        to make impossible.
        """
        params = set(inspect.signature(choose).parameters) | set(
            inspect.signature(score_option).parameters
        )
        self.assertEqual(params, {"candidates", "prefer_proceed", "option"}, params)
        source = inspect.getsource(choose) + inspect.getsource(score_option)
        read = set(re.findall(r'\.get\("([a-z_]+)"', source))
        self.assertTrue(
            read <= {"index", "is_proceed", "is_locked"},
            f"reads beyond the advertised candidate fields: {sorted(read)}",
        )

    def test_the_rule_is_versioned(self) -> None:
        self.assertTrue(POLICY_VERSION.startswith("conservative-visible-"))

    def test_scoring_is_deterministic_across_orders(self) -> None:
        a = [option(0, "移除 | 移除一张牌"), option(1, "诅咒 | 每回合失去生命")]
        self.assertEqual(choose(list(a)), choose(list(reversed(a))[::-1]))


if __name__ == "__main__":
    unittest.main()
