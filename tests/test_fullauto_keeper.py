"""Tests for the full-auto keeper's log-driven click decisions."""
from __future__ import annotations

import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

from bridge.fullauto_keeper import (
    _CLICK_SCRIPT,
    FullAutoKeeper,
    LogTail,
    _build_click_script,
    _click_via_power_shell,
    read_overlay_position_from_settings,
    should_click,
)

BATTLE_START = (
    "[INFO] [CombatSolver] [CombatSolver/Test] SEARCH_REQUEST generation=6 "
    "reason=AutoTurnStart cause=initial_search previous_boundary=- turn=1"
)
MID_TURN_SEARCH = (
    "[INFO] [CombatSolver] [CombatSolver/Test] SEARCH_REQUEST generation=7 "
    "reason=AutoTurnStart cause=initial_search previous_boundary=- turn=12"
)
ON_LINE = (
    "[INFO] [CombatSolver] [CombatSolver/Test] FULL_AUTO enabled=true "
    "stop_on_combat_end=False"
)
OFF_LINE = (
    "[INFO] [CombatSolver] [CombatSolver/Test] FULL_AUTO enabled=false "
    "stop_on_combat_end=False"
)
IRRELEVANT = "[INFO] [CombatSolver] [CombatSolver/Test] COVERAGE expanded=100"
READY = "[INFO] [CombatSolver] [CombatSolver/Test] UI_STATE state=ready turn=1 risk=True"
DEPLOY_TURN_ONE = "[INFO] [CombatSolver] [CombatSolver/Test] FULL_AUTO_DEPLOY turn=1"
DEPLOY_TURN_SIX = "[INFO] [CombatSolver] [CombatSolver/Test] FULL_AUTO_DEPLOY turn=6"
MANUAL_PLUS_SOLVER = (
    "[INFO] [CombatSolver] [CombatSolver/Test] REPLAN_SUMMARY "
    "reason=combat_ended control_mode=manual_plus_solver"
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class ShouldClickTests(unittest.TestCase):
    def test_explicit_false_clicks(self) -> None:
        self.assertTrue(should_click(OFF_LINE, last_known_on=None))
        self.assertTrue(should_click(OFF_LINE, last_known_on=True))

    def test_explicit_true_never_clicks(self) -> None:
        self.assertFalse(should_click(ON_LINE, last_known_on=None))
        self.assertFalse(should_click(ON_LINE, last_known_on=False))

    def test_battle_start_clicks_when_last_state_not_on(self) -> None:
        self.assertTrue(should_click(BATTLE_START, last_known_on=None))
        self.assertTrue(should_click(BATTLE_START, last_known_on=False))

    def test_battle_start_no_click_when_confirmed_on(self) -> None:
        self.assertFalse(should_click(BATTLE_START, last_known_on=True))

    def test_mid_turn_search_is_not_a_battle_start(self) -> None:
        self.assertFalse(should_click(MID_TURN_SEARCH, last_known_on=None))

    def test_irrelevant_lines_never_click(self) -> None:
        self.assertFalse(should_click(IRRELEVANT, last_known_on=None))
        self.assertFalse(should_click(IRRELEVANT, last_known_on=False))


class FullAutoStateMachineTests(unittest.TestCase):
    def make_keeper(self, **kwargs):
        clock = FakeClock()
        keeper = FullAutoKeeper(clock=clock, **kwargs)
        return keeper, clock

    def test_real_manual_plus_solver_sequence_recovers_without_false_line(self) -> None:
        """The observed failure has no FULL_AUTO enabled=false marker."""
        keeper, clock = self.make_keeper(battle_timeout=5.0)
        self.assertFalse(keeper.on_line(BATTLE_START))
        clock.now = 1.0
        self.assertFalse(keeper.on_line(READY))
        self.assertFalse(keeper.on_line(MANUAL_PLUS_SOLVER, now=1.1))
        # The old implementation would keep last_known_on=None forever and
        # had no timer.  A ready battle with no turn-one deploy must recover.
        clock.now = 6.0
        self.assertTrue(keeper.tick())
        self.assertEqual(keeper.pending_reason, "battle_turn1_deploy_timeout")
        keeper.click_started()
        self.assertTrue(keeper.click_in_flight)

        # This is the real late-human-toggle shape: it is too late for turn 1
        # but it must not trigger a second toggle after the first click.
        clock.now = 6.2
        self.assertFalse(keeper.on_line(ON_LINE))
        self.assertTrue(keeper.click_in_flight)
        clock.now = 6.3
        self.assertFalse(keeper.on_line(DEPLOY_TURN_SIX))
        self.assertFalse(keeper.click_in_flight)
        self.assertTrue(keeper.verification_warning)

    def test_each_new_battle_requires_its_own_turn_one_deploy(self) -> None:
        keeper, clock = self.make_keeper(battle_timeout=2.0)
        # A true state from the previous battle is not proof for this one.
        keeper.on_line(ON_LINE, now=0.0)
        keeper.on_line(DEPLOY_TURN_ONE, now=0.1)
        keeper.on_line("[INFO] [CombatSolver] [CombatSolver/Test] RESET reason=combat_ended", now=1.0)
        keeper.on_line(BATTLE_START, now=2.0)
        keeper.on_line(READY, now=2.1)
        self.assertFalse(keeper.tick(now=3.9))
        self.assertTrue(keeper.tick(now=4.2))
        self.assertEqual(keeper.pending_reason, "battle_turn1_deploy_timeout")

    def test_turn_one_deploy_cancels_timeout(self) -> None:
        keeper, clock = self.make_keeper(battle_timeout=2.0)
        keeper.on_line(BATTLE_START, now=0.0)
        keeper.on_line(READY, now=0.1)
        clock.now = 1.0
        self.assertFalse(keeper.on_line(DEPLOY_TURN_ONE))
        self.assertFalse(keeper.tick(now=5.0))
        self.assertTrue(keeper.turn_one_deployed)

    def test_old_true_state_does_not_count_as_post_click_confirmation(self) -> None:
        keeper, _ = self.make_keeper(
            battle_timeout=1.0, click_confirm_timeout=1.0, retry_interval=2.0
        )
        keeper.on_line(ON_LINE, now=0.0)
        keeper.on_line(DEPLOY_TURN_ONE, now=0.1)
        keeper.on_line("[INFO] [CombatSolver] [CombatSolver/Test] RESET reason=combat_ended", now=0.2)
        keeper.on_line(BATTLE_START, now=1.0)
        keeper.on_line(READY, now=1.1)
        self.assertTrue(keeper.tick(now=2.2))
        keeper.click_started(now=2.2)
        # ``last_known_on`` is still true from the previous battle, but there
        # was no post-click true/deploy event, so the click must be retried.
        self.assertFalse(keeper.tick(now=3.3))
        self.assertFalse(keeper.click_in_flight)
        self.assertTrue(keeper.tick(now=5.4))

    def test_delayed_confirmation_and_duplicate_clicks_are_suppressed(self) -> None:
        keeper, clock = self.make_keeper(
            battle_timeout=1.0, click_confirm_timeout=3.0, retry_interval=2.0
        )
        keeper.on_line(BATTLE_START, now=0.0)
        keeper.on_line(READY, now=0.1)
        self.assertTrue(keeper.tick(now=1.2))
        keeper.click_started(now=1.2)
        # Polling and duplicate OFF lines while the click is in flight cannot
        # produce another physical click.
        self.assertFalse(keeper.tick(now=1.3))
        self.assertFalse(keeper.on_line(OFF_LINE, now=1.4))
        self.assertTrue(keeper.click_in_flight is False)  # OFF is confirmation
        self.assertFalse(keeper.on_line(OFF_LINE, now=1.45))
        self.assertFalse(keeper.tick(now=1.5))  # debounce still active
        self.assertTrue(keeper.tick(now=2.3))
        keeper.click_started(now=2.3)
        # Confirmation may be delayed; no duplicate click before the timeout.
        self.assertFalse(keeper.tick(now=4.0))
        self.assertFalse(keeper.on_line(ON_LINE, now=4.1))
        self.assertTrue(keeper.click_in_flight)
        self.assertFalse(keeper.on_line(DEPLOY_TURN_ONE, now=4.2))
        self.assertFalse(keeper.click_in_flight)

    def test_click_confirm_timeout_retries_only_without_confirmation(self) -> None:
        keeper, clock = self.make_keeper(
            battle_timeout=1.0, click_confirm_timeout=1.0, retry_interval=2.0
        )
        keeper.on_line(BATTLE_START, now=0.0)
        keeper.on_line(READY, now=0.1)
        self.assertTrue(keeper.tick(now=1.2))
        keeper.click_started(now=1.2)
        self.assertFalse(keeper.tick(now=2.2))
        self.assertFalse(keeper.click_in_flight)
        self.assertFalse(keeper.tick(now=3.0))
        self.assertTrue(keeper.tick(now=4.3))


class LogTailTests(unittest.TestCase):
    def test_rotation_reads_new_source_and_resets_epoch(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "godot.log"
            first.write_text(ON_LINE + "\n", encoding="utf-8")
            tail = LogTail(root, start_at_end=False)
            self.assertEqual(tail.poll(), [ON_LINE])
            first.write_text(first.read_text(encoding="utf-8") + READY + "\n", encoding="utf-8")
            self.assertEqual(tail.poll(), [READY])

            rotated = root / "godot-rotated.log"
            rotated.write_text(BATTLE_START + "\n" + READY + "\n", encoding="utf-8")
            # Ensure the new file is newest on filesystems with coarse mtime.
            rotated.touch()
            lines = tail.poll()
            self.assertTrue(tail.source_changed)
            self.assertEqual(tail.epoch, 1)
            self.assertEqual(lines, [BATTLE_START, READY])

    def test_truncation_is_a_new_log_epoch(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "godot.log"
            log.write_text(BATTLE_START + "\n", encoding="utf-8")
            tail = LogTail(root, start_at_end=False)
            self.assertEqual(tail.poll(), [BATTLE_START])
            log.write_text(BATTLE_START + "\n" + READY + "\n", encoding="utf-8")
            self.assertEqual(tail.poll(), [READY])
            log.write_text(ON_LINE + "\n", encoding="utf-8")
            self.assertEqual(tail.poll(), [ON_LINE])
            self.assertTrue(tail.source_changed)
            self.assertEqual(tail.epoch, 1)


class ClickContractTests(unittest.TestCase):
    def test_click_script_converts_client_point_to_screen_point(self) -> None:
        # FindWindowW is a wide-char API: without CharSet.Unicode .NET
        # marshals the title as ANSI and the window is never found.
        self.assertIn('FindWindowW(string cls, string title)', _CLICK_SCRIPT)
        self.assertIn('CharSet = CharSet.Unicode', _CLICK_SCRIPT)
        self.assertIn("[NullString]::Value", _CLICK_SCRIPT)
        # structs declared inside -MemberDefinition compile as nested types
        self.assertIn('New-Object "W.U+RECT"', _CLICK_SCRIPT)
        self.assertIn('New-Object "W.U+POINT"', _CLICK_SCRIPT)
        self.assertNotIn("FindWindowW($null", _CLICK_SCRIPT)
        self.assertIn("ClientToScreen", _CLICK_SCRIPT)
        self.assertIn("$point.X = $localX", _CLICK_SCRIPT)
        self.assertIn("$point.Y = $localY", _CLICK_SCRIPT)
        self.assertIn("ClientToScreen($h, [ref]$point)", _CLICK_SCRIPT)
        self.assertIn("SetCursorPos($x, $y)", _CLICK_SCRIPT)
        self.assertNotIn("SetCursorPos($localX, $localY)", _CLICK_SCRIPT)

    def test_dragged_overlay_changes_local_target_by_position_delta(self) -> None:
        script = _build_click_script((20.0, 200.0))
        self.assertIn("+ (12.000000)", script)
        # dragged 200.0 vs calibrated 261.5 -> -61.5 vertical delta
        self.assertIn("+ (-61.500000)", script)
        self.assertIn("overlay 20.0,200.0", script)

    def test_offset_overlay_position_walks_and_wraps(self) -> None:
        from bridge.fullauto_keeper import (
            CALIBRATED_OVERLAY_POSITION,
            _offset_overlay_position,
        )

        self.assertEqual(
            _offset_overlay_position(CALIBRATED_OVERLAY_POSITION, 0),
            CALIBRATED_OVERLAY_POSITION,
        )
        first = _offset_overlay_position(CALIBRATED_OVERLAY_POSITION, 1)
        self.assertLess(first[1], CALIBRATED_OVERLAY_POSITION[1])
        self.assertEqual(first[0], CALIBRATED_OVERLAY_POSITION[0])
        # steps wrap around the list and return to the base position
        steps = [round(_offset_overlay_position(CALIBRATED_OVERLAY_POSITION, s)[1], 3)
                 for s in range(4)]
        self.assertIn(CALIBRATED_OVERLAY_POSITION[1], steps)

    def test_missing_overlay_position_fails_closed_before_subprocess(self) -> None:
        with patch("bridge.fullauto_keeper.subprocess.run") as run:
            with self.assertRaisesRegex(RuntimeError, "overlay position unavailable"):
                _click_via_power_shell()
            run.assert_not_called()

    def test_settings_position_is_validated(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(
                '{"overlayPositionX": 123.5, "overlayPositionY": 77}',
                encoding="utf-8",
            )
            self.assertEqual(
                read_overlay_position_from_settings(path), (123.5, 77.0)
            )
            path.write_text('{"overlayPositionX": "not-a-number"}', encoding="utf-8")
            self.assertIsNone(read_overlay_position_from_settings(path))

    def test_runtime_position_marker_updates_keeper(self) -> None:
        keeper = FullAutoKeeper()
        line = (
            "[INFO] [CombatSolver] [CombatSolver/Test] UI_POSITION_LOADED "
            "persisted=True x=321.5 y=99.25 size_persisted=False w=- h=-"
        )
        keeper.on_line(line, now=0.0)
        self.assertEqual(keeper.overlay_position, (321.5, 99.25))


if __name__ == "__main__":
    unittest.main()
