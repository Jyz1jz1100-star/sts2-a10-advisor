"""Contract tests for the Combat Solver read-only interface schema, readers,
log-tail adapter, and executed-action inference.

Log-format fixtures mirror the real installed-mod grammar (CombatSolver
0.25.3, grammar v1, calibrated against the 2026-09-02 godot.log session)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from combat_solver.executed import infer_executed_turn
from combat_solver.logformat import (
    LogTailSource,
    is_solver_line,
    parse_kv_tokens,
    read_solver_settings,
)
from combat_solver.reader import (
    DirectorySource,
    JsonlSource,
    parse_normalized_json_bytes,
)
from combat_solver.snapshot import (
    SnapshotError,
    failure_from_json,
    snapshot_from_json,
    snapshot_from_line,
    snapshot_to_json,
)

from tests.test_combat_solver_states import (
    enemy,
    monster_state,
    play_card,
)


def snapshot_payload(**overrides):
    payload = {
        "schema_version": 1,
        "state_hash": "local-sha256:" + "0" * 64,
        "battle_turn": 1,
        "route": [
            {
                "turn": 1,
                "actions": [
                    {"kind": "play", "card_id": "STRIKE", "target_index": 0},
                    {"kind": "end_turn"},
                ],
                "predicted_hp_end": 55,
            }
        ],
        "predicted": {"hp_loss": 5},
        "budget": {"tier": "mid", "elapsed_ms": 123},
        "provenance": {
            "reader": "jsonl",
            "captured_at_utc": "2026-09-02T00:00:00Z",
        },
        "candidates": [
            {"rank": 0, "hp_loss": 5, "summary": "best"},
            {"rank": 1, "hp_loss": 7, "summary": "alt"},
        ],
    }
    payload.update(overrides)
    return payload


class SnapshotSchemaTests(unittest.TestCase):
    def test_round_trip_preserves_fields(self) -> None:
        snapshot = snapshot_from_json(snapshot_payload())
        data = snapshot_from_line(json.dumps(snapshot_to_json(snapshot)))
        self.assertEqual(data.state_hash, "local-sha256:" + "0" * 64)
        self.assertEqual(data.battle_turn, 1)
        self.assertEqual(data.route[0].actions[0].card_id, "STRIKE")
        self.assertEqual(data.predicted.hp_loss, 5)
        self.assertEqual(data.budget.tier, "mid")
        self.assertEqual(len(data.candidates), 2)

    def test_state_hash_accepts_bare_hex(self) -> None:
        payload = snapshot_payload(state_hash="a" * 64)
        snapshot = snapshot_from_json(payload)
        self.assertEqual(snapshot.state_hash, "a" * 64)

    def test_state_hash_optional_with_battle_turn(self) -> None:
        payload = snapshot_payload(state_hash=None, battle_turn=3)
        snapshot = snapshot_from_json(payload)
        self.assertIsNone(snapshot.state_hash)
        self.assertEqual(snapshot.battle_turn, 3)

    def test_binding_key_required(self) -> None:
        payload = snapshot_payload(state_hash=None, battle_turn=None)
        with self.assertRaises(SnapshotError):
            snapshot_from_json(payload)

    def test_rejects_unknown_keys(self) -> None:
        with self.assertRaises(SnapshotError):
            snapshot_from_json(snapshot_payload(extra_key=1))

    def test_rejects_bad_state_hash(self) -> None:
        with self.assertRaises(SnapshotError):
            snapshot_from_json(snapshot_payload(state_hash="ui-panel-x"))

    def test_rejects_duplicate_route_turns(self) -> None:
        payload = snapshot_payload()
        payload["route"].append(dict(payload["route"][0]))
        with self.assertRaises(SnapshotError):
            snapshot_from_json(payload)

    def test_rejects_wrong_schema_version(self) -> None:
        with self.assertRaises(SnapshotError):
            snapshot_from_json(snapshot_payload(schema_version=2))

    def test_predicted_must_be_populated(self) -> None:
        with self.assertRaises(SnapshotError):
            snapshot_from_json(snapshot_payload(predicted={}))

    def test_play_requires_card_id(self) -> None:
        payload = snapshot_payload()
        payload["route"][0]["actions"][0] = {"kind": "play", "target_index": 0}
        with self.assertRaises(SnapshotError):
            snapshot_from_json(payload)

    def test_candidate_ranks_must_increase(self) -> None:
        payload = snapshot_payload()
        payload["candidates"][1]["rank"] = 0
        with self.assertRaises(SnapshotError):
            snapshot_from_json(payload)

    def test_route_step_predicted_hp_lost_accepted(self) -> None:
        payload = snapshot_payload()
        payload["route"][0]["predicted_hp_lost"] = 3
        snapshot = snapshot_from_json(payload)
        self.assertEqual(snapshot.route[0].predicted_hp_lost, 3)

    def test_failure_reasons_are_closed(self) -> None:
        failure = failure_from_json(
            {"reason": "TIMEOUT", "captured_at_utc": "2026-09-02T00:00:00Z", "battle_turn": 3}
        )
        self.assertEqual(failure.reason, "TIMEOUT")
        with self.assertRaises(SnapshotError):
            failure_from_json({"reason": "SOMETHING_ELSE", "captured_at_utc": "x"})


class ReaderTests(unittest.TestCase):
    def test_jsonl_source_parses_snapshot_and_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshots.jsonl"
            source = JsonlSource(path)
            self.assertEqual(source.poll(), [])
            with path.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps(snapshot_payload()) + "\n")
                handle.write(
                    json.dumps(
                        {"kind": "failure", "reason": "NO_ROUTE", "captured_at_utc": "t"}
                    )
                    + "\n"
                )
            events = source.poll()
            self.assertEqual(len(events), 2)
            self.assertIsNotNone(events[0].snapshot)
            self.assertEqual(events[0].snapshot.budget.elapsed_ms, 123)
            self.assertIsNotNone(events[1].failure)
            self.assertEqual(events[1].failure.reason, "NO_ROUTE")
            # no duplicate delivery on the next poll
            self.assertEqual(source.poll(), [])

    def test_jsonl_source_survives_bad_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshots.jsonl"
            path.write_text("not json\n", encoding="utf-8")
            events = JsonlSource(path).poll()
            self.assertEqual(len(events), 1)
            self.assertIsNone(events[0].snapshot)
            self.assertEqual(events[0].failure.reason, "PARSE_ERROR")

    def test_directory_source_consumes_files_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "a.json").write_text(
                json.dumps([snapshot_payload(state_hash="b" * 64)]), encoding="utf-8"
            )
            source = DirectorySource(directory, parse_fn=parse_normalized_json_bytes)
            events = source.poll()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].snapshot.provenance.source_file, "a.json")
            self.assertEqual(source.poll(), [])

    def test_directory_source_missing_dir_is_empty(self) -> None:
        source = DirectorySource(Path("Z:/does/not/exist"), parse_fn=parse_normalized_json_bytes)
        self.assertEqual(source.poll(), [])


class LogFormatTests(unittest.TestCase):
    """Grammar v1, mirroring the real 2026-09-02 session log."""

    RESULT_LINE = (
        "[INFO] [CombatSolver] [CombatSolver/Test] RESULT phase=Short "
        "deep_triggered=False deep_improved=False reused={reused} "
        "reused_from_turn=1 expanded=2772 searched_turns=2 "
        "battle_hp_lost_so_far=0 projected_battle_hp_lost=5 "
        "elapsed_ms=1119 total_elapsed_ms=1119 total_worker_allocated_bytes=486933496 "
        "final_hp=55 final_block=0 final_enemy_hp=0 combat_ended_turn=2 death_turn=- "
        "only_death_routes=False"
    )

    REQUEST_LINE = (
        "[INFO] [CombatSolver] [CombatSolver/Test] SEARCH_REQUEST "
        "generation=1 reason=AutoTurnStart cause=initial_search "
        "previous_boundary=- turn=1 deploy_when_ready=False theft_policy=- max_dop=8"
    )

    ACTION_LINES = [
        "[CombatSolver/Test] ACTION turn=1 kind=PlayCard card_id=NEUTRALIZE "
        "occurrence=0 title=中和 target_index=0 target_combat_id=1 target=毛绒伏地虫 "
        "choice_effect=- choice_cards=- kills=-",
        "[CombatSolver/Test] ACTION turn=1 kind=PlayCard card_id=DEFEND_SILENT "
        "occurrence=0 title=防御 target_index=-1 target_combat_id=- target=- "
        "choice_effect=- choice_cards=- kills=-",
        "[CombatSolver/Test] ACTION turn=2 kind=EndTurn",
    ]

    OUTCOME_LINES = [
        "[CombatSolver/Test] TURN_OUTCOME turn=1 hp_lost=2 sold_hp=0 max_block=10 "
        "actual_block=5 energy_left=0",
        "[CombatSolver/Test] TURN_OUTCOME turn=2 hp_lost=3 sold_hp=0 max_block=8 "
        "actual_block=8 energy_left=1",
    ]

    def _write_log(self, tmp: Path, lines: list[str]) -> Path:
        log_dir = tmp / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "godot.log").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
        return log_dir

    def test_full_block_emits_snapshot_with_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = self._write_log(
                Path(tmp),
                [self.REQUEST_LINE]
                + [self.RESULT_LINE.format(reused="False")]
                + self.OUTCOME_LINES
                + self.ACTION_LINES,
            )
            source = LogTailSource(log_dir, mod_version="0.25.3", replay=True)
            events = source.poll()
            snapshots = [e.snapshot for e in events if e.snapshot is not None]
            self.assertEqual(len(snapshots), 1)
            snap = snapshots[0]
            self.assertEqual(snap.battle_turn, 1)
            self.assertEqual(snap.predicted.hp_loss, 5)
            self.assertEqual(snap.predicted.hp_end, 55)
            self.assertEqual(snap.budget.elapsed_ms, 1119)
            self.assertEqual(snap.budget.nodes_expanded, 2772)
            self.assertEqual(snap.budget.peak_memory_mb, 464)
            self.assertEqual(snap.provenance.mod_version, "0.25.3")
            kinds = [a.kind for step in snap.route for a in step.actions]
            self.assertEqual(kinds, ["play", "play", "end_turn"])
            play = snap.route[0].actions[0]
            self.assertEqual(play.card_id, "NEUTRALIZE")
            self.assertEqual(play.target_index, 0)
            self.assertIsNone(snap.route[0].actions[1].target_index)
            self.assertEqual(snap.route[0].predicted_hp_lost, 2)
            self.assertEqual(snap.route[1].predicted_hp_lost, 3)

    def test_reused_block_rebinds_to_first_action_turn(self) -> None:
        reused_actions = [
            "[CombatSolver/Test] ACTION turn=3 kind=PlayCard card_id=SURVIVOR "
            "occurrence=0 title=生存者 target_index=-1 target_combat_id=- target=- "
            "choice_effect=- choice_cards=- kills=-",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = self._write_log(
                Path(tmp),
                [self.REQUEST_LINE, self.RESULT_LINE.format(reused="True")]
                + self.OUTCOME_LINES
                + reused_actions,
            )
            source = LogTailSource(log_dir, mod_version="0.25.3", replay=True)
            snapshots = [
                e.snapshot for e in source.poll() if e.snapshot is not None
            ]
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(snapshots[0].battle_turn, 3)
            # reused blocks carry no cost stats
            self.assertIsNone(snapshots[0].budget.elapsed_ms)
            self.assertIsNone(snapshots[0].budget.nodes_expanded)

    def test_duplicate_reused_package_suppressed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = self._write_log(
                Path(tmp),
                [self.REQUEST_LINE]
                + [self.RESULT_LINE.format(reused="True")]
                + self.OUTCOME_LINES
                + self.ACTION_LINES
                + [self.RESULT_LINE.format(reused="True")]
                + self.OUTCOME_LINES
                + self.ACTION_LINES,
            )
            source = LogTailSource(log_dir, mod_version="0.25.3", replay=True)
            snapshots = [
                e.snapshot for e in source.poll() if e.snapshot is not None
            ]
            self.assertEqual(len(snapshots), 1)

    def test_request_without_result_is_no_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = self._write_log(Path(tmp), [self.REQUEST_LINE])
            source = LogTailSource(log_dir, replay=True)
            events = source.poll()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].failure.reason, "NO_ROUTE")
            self.assertEqual(events[0].failure.battle_turn, 1)

    def test_search_failure_line_is_search_error(self) -> None:
        failure_line = (
            "[ERROR] [CombatSolver] [CombatSolver/Test] SEARCH_FAILURE "
            "generation=25 exception=System.InvalidOperationException: "
            "回放时找不到手牌 LEG_SWEEP#0。"
        )
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = self._write_log(
                Path(tmp), [self.REQUEST_LINE, failure_line]
            )
            source = LogTailSource(log_dir, replay=True)
            events = source.poll()
            failures = [e.failure for e in events if e.failure is not None]
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0].reason, "SEARCH_ERROR")
            self.assertIn("LEG_SWEEP", failures[0].detail or "")

    def test_stale_line_is_stale_state(self) -> None:
        stale = "[INFO] [CombatSolver] [CombatSolver/Test] SEARCH_STALE generation=18"
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = self._write_log(Path(tmp), [stale])
            source = LogTailSource(log_dir, replay=True)
            events = source.poll()
            failures = [e.failure for e in events if e.failure is not None]
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0].reason, "STALE_STATE")

    def test_live_tail_skips_existing_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = self._write_log(
                Path(tmp),
                [self.REQUEST_LINE, self.RESULT_LINE.format(reused="False")],
            )
            source = LogTailSource(log_dir)  # replay=False (default)
            self.assertEqual(source.poll(), [])

    def test_settings_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "combat_solver_settings.json"
            settings.write_text(
                json.dumps(
                    {
                        "performancePreset": "VeryHigh",
                        "deepTimeLimitSeconds": 300,
                        "noGcRegionBudgetGigabytes": 16,
                    }
                ),
                encoding="utf-8",
            )
            extracted = read_solver_settings(settings)
            self.assertEqual(extracted["tier"], "VeryHigh")
            self.assertEqual(extracted["time_budget_ms"], 300000)
            self.assertEqual(extracted["memory_limit_mb"], 16384)

    def test_deploy_lines_produce_deploy_record(self) -> None:
        from combat_solver.reader import DeployRecord

        lines = [
            "[INFO] [CombatSolver] [CombatSolver/Test] SEARCH_REQUEST "
            "generation=1 reason=AutoTurnStart turn=1",
            "[INFO] [CombatSolver] [CombatSolver/Test] DEPLOY_START turn=1 "
            "action_count=2 fast_mode=FollowGame inter_action_delay_seconds=0",
            "[INFO] [CombatSolver] [CombatSolver/Test] DEPLOY_ACTION turn=1 "
            "card=BASH target_index=0 target_combat_id=1 choice=-",
            "[INFO] [CombatSolver] [CombatSolver/Test] DEPLOY_ACTION turn=1 "
            "potion=STRENGTH_POTION slot=0 target_index=-1 target_combat_id=-",
            "[INFO] [CombatSolver] [CombatSolver/Test] DEPLOY_END turn=1 "
            "action_count=2 end_turn=true forecast_turn_start_choices=0",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "logs"
            log_dir.mkdir()
            (log_dir / "godot.log").write_text(
                "\n".join(lines) + "\n", encoding="utf-8"
            )
            source = LogTailSource(log_dir, replay=True)
            events = source.poll()
            deploys = [e.deploy for e in events if e.deploy is not None]
            self.assertEqual(len(deploys), 1)
            deploy = deploys[0]
            self.assertIsInstance(deploy, DeployRecord)
            self.assertEqual(deploy.turn, 1)
            self.assertTrue(deploy.end_turn)
            kinds = [(a.kind, a.card_id, a.target_index) for a in deploy.actions]
            self.assertEqual(
                kinds,
                [("play", "BASH", 0), ("potion", "STRENGTH_POTION", None)],
            )
            # deploy lines must not leak into route snapshots
            self.assertEqual([e for e in events if e.snapshot is not None], [])

    def test_solver_line_detection_and_kv(self) -> None:
        self.assertTrue(is_solver_line(self.RESULT_LINE.format(reused="False")))
        self.assertFalse(is_solver_line("regular game log line"))
        kv = parse_kv_tokens(self.RESULT_LINE.format(reused="False"))
        self.assertEqual(kv["projected_battle_hp_lost"], "5")
        self.assertEqual(kv["total_worker_allocated_bytes"], "486933496")
        self.assertEqual(kv["reused"], "False")


class ExecutedInferenceTests(unittest.TestCase):
    def test_play_target_and_end_turn(self) -> None:
        prev = monster_state(
            round_no=1,
            hp=60,
            energy=3,
            hand=[play_card("STRIKE", 0, cost=1), play_card("DEFEND", 1, cost=1, target="Self")],
            enemies=[enemy("CULTIST_0", hp=48)],
        )
        nxt = monster_state(
            round_no=2,
            hp=54,
            energy=2,
            hand=[play_card("DEFEND", 0, cost=1, target="Self")],
            enemies=[enemy("CULTIST_0", hp=42)],
        )
        turn = infer_executed_turn(prev, nxt, turn=1)
        self.assertFalse(turn.ambiguous)
        kinds = [a.comparable() for a in turn.actions]
        self.assertIn(("play", "STRIKE", 0), kinds)
        self.assertIn(("end_turn", None, None), kinds)
        self.assertNotIn(("play", "DEFEND", None), kinds)

    def test_energy_mismatch_marks_ambiguous(self) -> None:
        prev = monster_state(
            round_no=1,
            energy=3,
            hand=[play_card("STRIKE", 0, cost=1)],
            enemies=[enemy("CULTIST_0", hp=48)],
        )
        nxt = monster_state(
            round_no=2,
            energy=3,  # played 1 but energy unchanged -> unexplained
            hand=[],
            enemies=[enemy("CULTIST_0", hp=42)],
        )
        turn = infer_executed_turn(prev, nxt, turn=1)
        self.assertTrue(turn.ambiguous)
        self.assertTrue(any("energy mismatch" in n for n in turn.notes))

    def test_potion_use_is_inferred(self) -> None:
        potion = {"id": "FIRE_POTION", "name": "火焰药水"}
        prev = monster_state(round_no=1, potions=[potion])
        nxt = monster_state(round_no=2, potions=[])
        turn = infer_executed_turn(prev, nxt, turn=1)
        kinds = [a.comparable() for a in turn.actions]
        self.assertIn(("potion", "FIRE_POTION", None), kinds)

    def test_ambiguous_target_stays_none(self) -> None:
        prev = monster_state(
            round_no=1,
            hand=[play_card("STRIKE", 0)],
            enemies=[enemy("E0", hp=20), enemy("E1", hp=20)],
        )
        nxt = monster_state(
            round_no=2,
            hand=[],
            enemies=[enemy("E0", hp=14), enemy("E1", hp=14)],
        )
        turn = infer_executed_turn(prev, nxt, turn=1)
        self.assertIn(("play", "STRIKE", None), [a.comparable() for a in turn.actions])


if __name__ == "__main__":
    unittest.main()
