"""The bridge may only call a run "won" on the producer's own word for it.

This is the claim the whole acceptance protocol turns on: an A10 clear. STS2MCP
exposes no win/loss flag, so the fallback matters more than the happy path -- a
run abandoned while the player is still alive would otherwise be announced, and
later quoted, as a victory.
"""
from __future__ import annotations

import unittest

from bridge.outcome import game_over_message, run_outcome


def _state(message: object, *, act: int = 1, floor: int = 4, hp: int = 27) -> dict:
    state: dict = {"run": {"act": act, "floor": floor},
                   "player": {"character": "The Ironclad", "hp": hp}}
    if message is not None:
        state["game_over"] = {"message": message}
    return state


class RunOutcomeTests(unittest.TestCase):
    def test_producer_win_word_is_a_victory(self) -> None:
        # Win words are checked before loss words, as they were before: "You have
        # defeated the Act 3 boss" contains the loss substring "defeat", so the
        # priority is load-bearing. The producer's real game_over wording has not
        # been observed on this machine, and nothing here should imply it has.
        self.assertIs(run_outcome(_state("Victory! You escaped the Spire.")), True)
        self.assertIn("VICTORY", game_over_message(_state("Victory!", act=3, floor=63)))

    def test_producer_loss_word_is_a_defeat(self) -> None:
        self.assertIs(run_outcome(_state("You have died.")), False)
        message = game_over_message(_state("You have died.", act=3, floor=58))
        self.assertIn("Defeated", message)
        self.assertNotIn("VICTORY", message)

    def test_surviving_the_screen_is_not_a_victory(self) -> None:
        # the case that used to fabricate a win: abandoned mid-run, still alive
        abandoned = _state("Run abandoned", act=1, floor=4, hp=40)
        self.assertIs(run_outcome(abandoned), None)
        message = game_over_message(abandoned)
        self.assertIn("undetermined", message.lower())
        self.assertNotIn("VICTORY", message)
        self.assertNotIn("beat the Spire", message)
        self.assertIn("not evidence of a clear", message)

    def test_missing_message_entirely_stays_undetermined(self) -> None:
        self.assertIs(run_outcome(_state(None)), None)
        self.assertIs(run_outcome(_state(None, hp=0)), None)
        self.assertIn("undetermined", game_over_message(_state(None, hp=0)).lower())

    def test_loss_words_beat_hp_when_both_are_present(self) -> None:
        # a death message with stale positive HP must not read as a win
        self.assertIs(run_outcome(_state("You were slain.", hp=12)), False)

    def test_negated_complete_is_not_a_victory(self) -> None:
        # The original substring test matched "complete" inside "incomplete" and
        # reported an unfinished run as a clear.
        self.assertIs(run_outcome(_state("Run incomplete.")), None)
        self.assertIs(run_outcome(_state("Your run is incomplete - the spire stands")), None)
        self.assertNotIn("VICTORY", game_over_message(_state("Run incomplete.")))

    def test_win_stems_still_read_their_inflections(self) -> None:
        # Guard against the opposite over-correction: whole-word matching would
        # stop \bvictor\b from matching "Victory" at all.
        self.assertIs(run_outcome(_state("Victory! You have triumphed.")), True)
        self.assertIs(run_outcome(_state("You escaped the Spire.")), True)
        self.assertIs(run_outcome(_state("Run complete")), True)

    def test_both_polarities_in_one_message_stay_undetermined(self) -> None:
        # Ordering alone must not turn "cleared X, then died" into a clear.
        self.assertIs(run_outcome(_state("Cleared the act, then perished in the next")), None)
        self.assertIs(run_outcome(_state("Completely defeated by the final boss")), None)

    def test_partial_act_clear_is_not_a_run_clear(self) -> None:
        # The real game's final act has two bosses, so an act-clear screen is not
        # evidence the run was finished.
        self.assertIs(run_outcome(_state("The Act was cleared but the run continues")), None)


if __name__ == "__main__":
    unittest.main()
