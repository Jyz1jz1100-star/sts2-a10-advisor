"""Does a run's own record say it covered the required flow, and only that?

These lock the delivery boundary: three acts, one Ancient each, 1+1+2 bosses and
the game's victory terminal.  A ledger that could be satisfied by a floor counter
or by an act number would let an implementation defect be reported as a strategy
loss, which is the misfiling this whole audit exists to stop.
"""
from __future__ import annotations

import unittest

from bridge.autoplay import AutoPlayer
from bridge.outcome import outcome_source, run_outcome
from bridge.run_progress import RunCoverage


def state(act, floor, state_type, *, hp=50, event=None, enemies=None, game_over=None):
    payload = {
        "state_type": state_type,
        "run": {"act": act, "floor": floor, "ascension": 10},
        "player": {"hp": hp},
    }
    if event is not None:
        payload["event"] = event
    if enemies is not None:
        payload["battle"] = {"enemies": [{"entity_id": e} for e in enemies]}
    if game_over is not None:
        payload["game_over"] = game_over
    return payload


def walk(*, victory, second_boss=True, ancient_act_1=True):
    """A three-act run with the build's real bosses, ending on its own screen."""
    ledger = RunCoverage()
    steps = []
    if ancient_act_1:
        steps.append(state(1, 1, "event", event={"is_ancient": True, "event_id": "NEOW"}))
    steps += [
        state(1, 17, "boss", enemies=["VANTOM_0"]),
        state(1, 17, "rewards"),
        state(2, 18, "event", event={"is_ancient": True, "event_id": "OROBAS"}),
        state(2, 33, "boss", enemies=["THE_INSATIABLE_0"]),
        state(2, 33, "rewards"),
        state(3, 34, "event", event={"is_ancient": True, "event_id": "TANX"}),
        state(3, 48, "boss", enemies=["QUEEN_0"]),
        state(3, 48, "rewards"),
    ]
    if second_boss:
        steps.append(state(3, 49, "boss", enemies=["AEONGLASS_0"]))
    terminal = {
        "victory": state(3, 49, "game_over", hp=0,
                         game_over={"is_victory": True, "message": "Run ended."}),
        "death": state(3, 49, "game_over", hp=0,
                       game_over={"is_victory": False, "message": "Run ended."}),
        "legacy": state(3, 49, "game_over", hp=0,
                        game_over={"message": "Run ended."}),
    }[victory]
    for step in steps + [terminal]:
        ledger.observe(step)
    return ledger.coverage()


class CompleteRunTests(unittest.TestCase):
    def test_only_a_victory_terminal_with_every_step_certifies_a_clear(self) -> None:
        self.assertTrue(walk(victory="victory")["run_complete"])

    def test_second_boss_is_not_optional(self) -> None:
        coverage = walk(victory="victory", second_boss=False)
        # Its first boss is genuinely cleared; what is missing is the second node,
        # and four distinct boss nodes is what the delivery target asks for.
        self.assertEqual(sorted(coverage["bosses_cleared"]), ["1:17", "2:33", "3:48"])
        self.assertFalse(coverage["run_complete"])

    def test_losing_to_the_second_boss_cannot_be_a_clear(self) -> None:
        coverage = walk(victory="death")
        self.assertFalse(coverage["run_complete"])
        self.assertNotIn("3:49", coverage["bosses_cleared"])

    def test_a_missing_ancient_is_reported_as_a_gap_not_a_win(self) -> None:
        coverage = walk(victory="victory", ancient_act_1=False)
        self.assertEqual(coverage["ancients_covered_acts"], [2, 3])


class VictoryEvidenceTests(unittest.TestCase):
    """The locked bridge cannot express a win; nothing may infer one instead."""

    def test_a_malformed_terminal_payload_stays_undetermined(self) -> None:
        # The mod has been observed to put something other than an object here
        # after a crash; a reading must not be manufactured, and it must not raise.
        for payload in ({"game_over": "Run ended."}, {"game_over": None}, {}):
            self.assertIsNone(run_outcome(payload), payload)
            self.assertEqual(outcome_source(payload), "no_victory_signal", payload)

    def test_locked_bridge_message_never_reads_as_a_victory(self) -> None:
        coverage = walk(victory="legacy")
        self.assertIsNone(coverage["outcome"])
        self.assertEqual(coverage["outcome_source"], "game_over_message_wording")
        self.assertFalse(coverage["run_complete"])

    def test_explicit_flag_overrides_wording_in_both_directions(self) -> None:
        won = {"game_over": {"is_victory": True, "message": "Run ended."}}
        lost = {"game_over": {"is_victory": False, "message": "You triumphed!"}}
        self.assertIs(run_outcome(won), True)
        self.assertIs(run_outcome(lost), False)
        self.assertEqual(outcome_source(won), "bridge_is_victory_flag")

    def test_arriving_alive_is_not_a_win(self) -> None:
        # Abandoning mid-act also ends alive; HP is not victory evidence.
        self.assertIsNone(run_outcome({"game_over": {"message": "Run ended."},
                                       "player": {"hp": 40}}))
        self.assertIsNone(run_outcome({"game_over": {}}))
        self.assertEqual(outcome_source({"game_over": {}}), "no_victory_signal")


class StreamHygieneTests(unittest.TestCase):
    def test_repeated_terminal_polls_do_not_create_phantom_runs(self) -> None:
        ledger = RunCoverage()
        ledger.observe(state(1, 17, "boss", enemies=["VANTOM_0"]))
        ledger.observe(state(1, 17, "rewards"))
        terminal = state(1, 17, "game_over", hp=0, game_over={"message": "Run ended."})
        for _ in range(6):
            ledger.observe(terminal)
        self.assertEqual(ledger.bosses_cleared_by_act(), {1: 1})
        self.assertTrue(ledger.closed)

    def test_menu_frames_do_not_count_as_visiting_act_one(self) -> None:
        ledger = RunCoverage()
        for _ in range(4):
            ledger.observe(state(1, 0, "menu"))
        self.assertEqual(ledger.coverage()["acts_seen"], [])
        self.assertFalse(ledger.coverage()["started"])

    def test_every_generic_proceed_is_recorded(self) -> None:
        ledger = RunCoverage()
        ledger.note_bypass("crystal_sphere", 2, 21, reason="generic_proceed_fallback")
        ledger.note_bypass("rewards", 1, 17, reason="generic_proceed_fallback")
        coverage = ledger.coverage()
        self.assertEqual(len(coverage["proceed_bypasses"]), 2)
        self.assertEqual(
            [row["state_type"] for row in coverage["unhandled_screen_bypasses"]],
            ["crystal_sphere"],
        )


class SilentSkipBoundsTests(unittest.TestCase):
    """An unmodelled screen may be stepped past once; it may not be walked through."""

    def test_unknown_screen_with_a_proceed_control_is_reported_not_swallowed(self) -> None:
        player = AutoPlayer(controller=None)
        mystery = {
            "state_type": "fake_merchant",
            "fake_merchant": {"can_proceed": True},
            "run": {"act": 3, "floor": 49},
        }
        self.assertEqual(player.decide(mystery), {"action": "proceed"})
        self.assertEqual(
            [row["state_type"] for row in player.coverage.coverage()["proceed_bypasses"]],
            ["fake_merchant"],
        )

    def test_repeated_bypass_of_the_same_unmodelled_screen_stops_the_batch(self) -> None:
        player = AutoPlayer(controller=None)
        mystery = {
            "state_type": "fake_merchant",
            "fake_merchant": {"can_proceed": True},
            "run": {"act": 3, "floor": 49},
        }
        with self.assertRaises(Exception) as caught:
            for _ in range(5):
                player.decide(mystery)
        self.assertIn("unmodelled content", str(caught.exception))

    def test_a_ruled_screen_is_not_counted_as_unhandled(self) -> None:
        player = AutoPlayer(controller=None)
        coverage = player.coverage
        coverage.observe(state(3, 49, "game_over", hp=0,
                               game_over={"message": "Run ended."}))
        self.assertEqual(coverage.coverage()["unhandled_screen_bypasses"], [])


class SummaryOutcomeTests(unittest.TestCase):
    def test_summary_keeps_clears_and_victory_evidence_apart(self) -> None:
        player = AutoPlayer(controller=None)
        player.coverage.observe(state(1, 1, "map"))
        self.assertEqual(player.summary()["runs_with_certified_clear"], 0)
        self.assertFalse(player.summary()["victory_evidence_available"])

    def test_sealed_runs_are_carried_into_the_summary(self) -> None:
        player = AutoPlayer(controller=None)
        ledger = player.coverage
        ledger.observe(state(1, 17, "boss", enemies=["VANTOM_0"]))
        ledger.observe(state(1, 17, "rewards"))
        ledger.observe(state(2, 33, "boss", enemies=["THE_INSATIABLE_0"]))
        ledger.observe(state(2, 33, "game_over", hp=0,
                             game_over={"message": "Run ended."}))
        player._seal_run("game_over")
        runs = player.summary()["runs"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["sealed_by"], "game_over")
        self.assertEqual(runs[0]["boss_battles_by_act_floor"],
                         {"1:17": ["VANTOM_0"], "2:33": ["THE_INSATIABLE_0"]})


class ScriptedController:
    """Feeds a recorded state sequence through the real polling loop."""

    recorder = None

    def __init__(self, states) -> None:
        self._states = list(states)
        self._cursor = 0
        self.posts: list[dict] = []

    def get_state(self):
        index = min(self._cursor, len(self._states) - 1)
        self._cursor += 1
        return self._states[index], f"decision-{index}"

    def send_action(self, payload, **kwargs):
        self.posts.append(payload)
        return {"status": "ok"}, None


class LoopIntegrationTests(unittest.TestCase):
    """The ledger has to work in the driver, not just beside it.

    No live game was available for this round, so this replays the shape the
    2026-09-20 traces actually had -- three acts, each act's Ancient, the final
    act's first boss, then a terminal screen -- through ``AutoPlayer.run()``.
    """

    def test_run_loop_seals_one_run_and_reports_what_it_covered(self) -> None:
        states = [
            state(1, 1, "event", event={"is_ancient": True, "event_id": "NEOW"}),
            state(1, 17, "boss", enemies=["VANTOM_0"]),
            state(1, 17, "rewards"),
            state(2, 18, "event", event={"is_ancient": True, "event_id": "OROBAS"}),
            state(2, 33, "boss", enemies=["THE_INSATIABLE_0"]),
            state(2, 33, "rewards"),
            state(3, 34, "event", event={"is_ancient": True, "event_id": "TANX"}),
            state(3, 48, "boss", enemies=["QUEEN_0"]),
            state(3, 48, "rewards"),
            state(3, 48, "game_over", hp=0, game_over={"message": "Run ended."}),
        ]
        controller = ScriptedController(states)
        player = AutoPlayer(controller, max_runs=99, max_actions=20, poll=0)
        summary = player.run()

        runs = summary["runs"]
        self.assertEqual(len(runs), 1, runs)
        row = runs[0]
        self.assertEqual(row["sealed_by"], "game_over")
        self.assertEqual(row["acts_seen"], [1, 2, 3])
        self.assertEqual(row["ancients_by_act"],
                         {"1": "NEOW", "2": "OROBAS", "3": "TANX"})
        self.assertEqual(sorted(row["boss_battles_by_act_floor"]), ["1:17", "2:33", "3:48"])
        # Every boss actually fought was actually left alive; what is missing is
        # the final act's second boss node, which this run never reached.
        self.assertNotIn("3:49", row["bosses_cleared"])
        self.assertEqual(row["bosses_cleared_by_act"]["3"], 1)
        self.assertIsNone(row["outcome"])
        self.assertFalse(row["run_complete"])
        self.assertEqual(summary["runs_with_certified_clear"], 0)
        self.assertFalse(summary["victory_evidence_available"])
        # The terminal screen is left by a real POST, not by assuming it away.
        self.assertIn({"action": "menu_select", "option": "main_menu"}, controller.posts)


if __name__ == "__main__":
    unittest.main()
