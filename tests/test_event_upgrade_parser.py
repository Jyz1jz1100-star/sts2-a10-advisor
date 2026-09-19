"""The event-upgrade census is only trustworthy while its source parser still matches the source.

``classify_events`` refuses to run when the number of ``AddEventRewardCard`` call sites disagrees
with the number of event arms it classified, and that refusal is the whole reason the census can
be cited as evidence: a silent drop of one event would understate opportunities, and a silently
unclassified argument would look like "no opportunity" rather than "parser out of date". These
tests hand it sources where each of those failures is present and require it to say so.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
_SPEC = importlib.util.spec_from_file_location(
    "measure_event_upgrade_opportunity",
    ROOT / "scripts" / "measure_event_upgrade_opportunity.py")
M = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(M)

CONSTANTS = "public const int EventAlpha = 7, EventBeta = 9, EventGamma = 11;"


def step_source(extra_call_outside_cases: bool = False,
                gamma_argument: str = "upgraded: true") -> str:
    arms = f"""
            case RunConstants.EventAlpha:
                AddEventRewardCard(upgraded: true);
                return 0;
            case RunConstants.EventBeta:
                AddEventRewardCard(upgraded: action == 2);
                return 0;
            case RunConstants.EventGamma:
                AddEventRewardCard({gamma_argument});
                return 0;
"""
    stray = "\n                AddEventRewardCard();\n" if extra_call_outside_cases else ""
    return f"""
public sealed class RunEngine
{{
    private int StepEvent(int action)
    {{
        switch (State.EventId)
        {{
{arms}            default:
                return -1;
        }}
    }}

    private void Other(int action)
    {{
{stray}    }}

    private void AddEventRewardCard(bool upgraded = false)
    {{
    }}
}}
"""


class EventUpgradeParserTests(unittest.TestCase):
    def test_each_arm_gets_its_upgrade_condition_and_numeric_id(self) -> None:
        classified = M.classify_events(step_source(), CONSTANTS)
        self.assertEqual(
            {7: {"name": "EventAlpha", "mechanisms": {"always_upgraded_card": 1}},
             9: {"name": "EventBeta", "mechanisms": {"upgraded_card_if_option_two": 1}},
             11: {"name": "EventGamma", "mechanisms": {"always_upgraded_card": 1}}}, classified)

    def test_a_call_site_outside_the_cases_refuses_to_run(self) -> None:
        # Three call sites in the file, two classified arms plus gamma with an unknown argument
        # form: the guard has to fire rather than quietly measure a subset of the events.
        with self.assertRaises(SystemExit) as caught:
            M.classify_events(step_source(extra_call_outside_cases=True), CONSTANTS)
        self.assertIn("AddEventRewardCard call sites", str(caught.exception))

    def test_an_unrecognised_argument_is_loud_not_dropped(self) -> None:
        classified = M.classify_events(step_source(gamma_argument="upgraded: flag"), CONSTANTS)
        self.assertTrue(next(iter(classified[11]["mechanisms"])).startswith("unclassified("),
                        "an argument form the parser does not know must still be reported")

    def test_plain_card_arms_are_classified_separately(self) -> None:
        source = step_source(gamma_argument="")
        classified = M.classify_events(source, CONSTANTS)
        self.assertEqual({"plain_card": 1}, classified[11]["mechanisms"])
        self.assertEqual({"always_upgraded_card": 1}, classified[7]["mechanisms"])

    def test_a_held_card_upgrade_arm_is_classified_as_its_own_mechanism(self) -> None:
        # Ten events promote a card already in the deck via UpgradeFirstCard(State), and that is
        # the dominant upgrade route -- it must not be invisible next to AddEventRewardCard.
        gamma = "            case RunConstants.EventGamma:\n"
        source = step_source().replace(
            gamma, gamma + "                RunNonCombatEffects.UpgradeFirstCard(State);\n")
        classified = M.classify_events(source, CONSTANTS)
        # Gamma keeps its card-granting arm too: one event may hold both mechanisms, and neither
        # may crowd the other out of the classification.
        self.assertEqual({"always_upgraded_card": 1, "upgrades_a_held_card": 1},
                         classified[11]["mechanisms"])

    def test_a_held_card_upgrade_outside_the_event_switch_refuses_to_run(self) -> None:
        other = "    private void Other(int action)\n    {\n"
        stray = step_source().replace(
            other, other + "        RunNonCombatEffects.UpgradeFirstCard(State);\n")
        with self.assertRaises(SystemExit) as caught:
            M.classify_events(stray, CONSTANTS)
        self.assertIn("UpgradeFirstCard(State) sites", str(caught.exception))

    def test_option_two_is_the_documented_upgraded_arm(self) -> None:
        self.assertEqual(2, M.UPGRADE_OPTION)


if __name__ == "__main__":
    unittest.main()
