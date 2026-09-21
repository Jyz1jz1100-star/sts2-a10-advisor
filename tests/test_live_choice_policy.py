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
    has_out_of_combat_use,
    potion_to_discard_for,
    potion_to_sip_now,
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


def potion(slot, text, **extra):
    name, _, description = text.partition("|")
    payload = {
        "slot": slot,
        "id": f"POTION_{slot}",
        "name": name.strip(),
        "description": description.strip(),
    }
    payload.update(extra)
    return payload


def run_state(potions, hp=80, max_hp=80):
    return {
        "state_type": "map",
        "player": {
            "hp": hp,
            "max_hp": max_hp,
            "potions": potions,
            "max_potion_slots": 3,
        },
        "run": {"act": 1, "floor": 12},
    }


class SipPotionTests(unittest.TestCase):
    """Held potions the installed bridge cannot vouch for are never touched."""

    def test_nothing_is_sipped_when_usage_is_not_published(self) -> None:
        # This is the installed bridge's shape: can_use_in_combat is true for both
        # CombatOnly and AnyTime, so it says nothing about drinking on the map.
        state = run_state(
            [potion(0, "血之药水 | 回复你最大生命30%的生命", can_use_in_combat=True)],
            hp=20,
        )
        self.assertIsNone(potion_to_sip_now(state))

    def test_a_maximum_health_gain_is_taken_whenever_it_is_legal(self) -> None:
        state = run_state(
            [potion(0, "果汁 | 提升5点最大生命", usage="AnyTime", target_type="Self")]
        )
        picked = potion_to_sip_now(state)
        self.assertIsNotNone(picked)
        self.assertEqual(picked["slot"], 0)

    def test_a_heal_is_spent_on_low_health_and_kept_otherwise(self) -> None:
        heal = potion(1, "血之药水 | 回复你最大生命30%的生命",
                      usage="AnyTime", target_type="None")
        self.assertEqual(potion_to_sip_now(run_state([heal], hp=30, max_hp=90))["slot"], 1)
        self.assertIsNone(potion_to_sip_now(run_state([heal], hp=90, max_hp=90)))

    def test_a_rest_site_is_preferred_over_spending_a_heal(self) -> None:
        heal = potion(1, "血之药水 | 回复你最大生命30%的生命",
                      usage="AnyTime", target_type="None")
        self.assertIsNone(
            potion_to_sip_now(run_state([heal], hp=20), at_rest_site=True)
        )

    def test_a_potion_aimed_at_an_enemy_is_not_drinkable_between_fights(self) -> None:
        state = run_state(
            [potion(0, "腐蚀药液 | 对一个敌人施加", usage="AnyTime",
                    target_type="Enemies")]
        )
        self.assertIsNone(potion_to_sip_now(state))


class DiscardForPurchaseTests(unittest.TestCase):
    def test_only_a_strictly_better_offered_potion_costs_a_held_one(self) -> None:
        held = potion(0, "弱效药水 | 造成6点伤害", usage="CombatOnly")
        state = run_state([held], hp=80)
        better = {"category": "potion", "potion_name": "力量药水",
                  "potion_description": "获得2点力量"}
        self.assertEqual(potion_to_discard_for(state, better)["slot"], 0)
        worse = {"category": "potion", "potion_name": "同样平庸",
                 "potion_description": "造成6点伤害"}
        self.assertIsNone(potion_to_discard_for(state, worse))

    def test_an_offered_potion_without_published_text_cannot_buy_a_discard(self) -> None:
        state = run_state([potion(0, "弱效药水 | 造成6点伤害")])
        self.assertIsNone(
            potion_to_discard_for(state, {"category": "potion", "potion_name": "x"})
        )


class PotionRuleInputTests(unittest.TestCase):
    def test_the_potion_rules_read_visible_state_only(self) -> None:
        """Same guarantee as the event rule: no seed, floor or encounter lookup."""
        source = "".join(
            inspect.getsource(function)
            for function in (
                has_out_of_combat_use,
                potion_to_sip_now,
                potion_to_discard_for,
            )
        )
        self.assertNotIn("seed", source)
        self.assertNotIn('"floor"', source)
        self.assertNotIn('"act"', source)
        read = set(re.findall(r'\.get\("([a-z_]+)"', source))
        self.assertTrue(
            read <= {
                "usage", "target_type", "description", "name", "slot", "hp",
                "max_hp", "potions", "player", "potion_description",
                "potion_name", "id",
            },
            f"reads beyond published potion/player fields: {sorted(read)}",
        )


class RealRecordedPotionTextTests(unittest.TestCase):
    """The wording rules, judged against text the client actually published.

    These strings are lifted verbatim from live states in this machine's traces
    (``player.potions`` on a map frame).  They matter because the two that this
    rule exists for differ only in the verb: a heal says 回复你最大生命值的20%, a
    permanent gain says 获得5点最大生命值 -- so any rule that merely looks for
    最大生命 treats the heal as the gain and drinks it at full HP.
    """

    BLOOD = potion(
        0, "鲜血药水 | 回复你最大生命值的20%。",
        usage="AnyTime", target_type="AnyPlayer", can_use_in_combat=True,
    )
    JUICE = potion(
        1, "果汁 | 获得5点最大生命值。",
        usage="AnyTime", target_type="AnyPlayer", can_use_in_combat=True,
    )
    FOUL = potion(
        2,
        "污浊药水 | 对所有玩家和敌人造成12点伤害。\n也可以投掷给商人换取100金币。",
        usage="AnyTime", target_type="TargetedNoCreature", can_use_in_combat=True,
    )

    def test_a_heal_is_never_mistaken_for_a_maximum_health_gain(self) -> None:
        # Full HP: nothing is worth drinking, and the heal must not be read as
        # permanent just because its text names 最大生命值.
        state = run_state([dict(self.BLOOD)], hp=80, max_hp=80)
        self.assertIsNone(potion_to_sip_now(state))

    def test_the_maximum_health_gain_is_taken_at_full_health(self) -> None:
        state = run_state([dict(self.JUICE)], hp=80, max_hp=80)
        self.assertEqual(potion_to_sip_now(state)["slot"], 1)

    def test_the_heal_is_taken_when_health_is_low(self) -> None:
        state = run_state([dict(self.BLOOD)], hp=40, max_hp=80)
        self.assertEqual(potion_to_sip_now(state)["slot"], 0)

    def test_a_potion_with_no_good_effect_between_fights_is_left_alone(self) -> None:
        # Damaging every player including your own, on a map screen.
        state = run_state([dict(self.FOUL)], hp=40, max_hp=80)
        self.assertIsNone(potion_to_sip_now(state))
