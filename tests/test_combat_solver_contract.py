"""Contract tests for the Combat Solver read-only interface schema, readers,
log-tail adapter, and executed-action inference.

Log-format fixtures mirror the real installed-mod grammar (CombatSolver
0.25.3, grammar v1, calibrated against the 2026-09-02 godot.log session)."""
from __future__ import annotations

import json
import hashlib
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
from combat_solver.logranges import (
    MARKER_EVENTS,
    LogRange,
    LogRangeError,
    capture_log_range,
    ranges_overlap,
    scan_markers,
    validate_deploy_grammar,
    verify_log_range,
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
            self.assertIsNotNone(deploy.log_range)
            evidence = deploy.log_range
            self.assertEqual(evidence.byte_start, 0)
            self.assertEqual(evidence.byte_end, (log_dir / "godot.log").stat().st_size)
            self.assertEqual(
                evidence.sha256,
                hashlib.sha256((log_dir / "godot.log").read_bytes()).hexdigest().upper(),
            )
            self.assertEqual(
                evidence.marker_counts,
                {
                    "SEARCH_REQUEST": 1,
                    "DEPLOY_START": 1,
                    "DEPLOY_ACTION": 2,
                    "DEPLOY_END": 1,
                },
            )
            # deploy lines must not leak into route snapshots
            self.assertEqual([e for e in events if e.snapshot is not None], [])

    def test_debug_marker_deploy_block_verifies_like_test(self) -> None:
        lines = [
            "[INFO] [CombatSolver] [CombatSolver/Debug] SEARCH_REQUEST "
            "generation=1 reason=AutoTurnStart turn=1",
            "[INFO] [CombatSolver] [CombatSolver/Debug] DEPLOY_START turn=1 "
            "action_count=1 fast_mode=FollowGame",
            "[INFO] [CombatSolver] [CombatSolver/Debug] DEPLOY_ACTION turn=1 "
            "card=BASH target_index=0 target_combat_id=1 choice=-",
            "[INFO] [CombatSolver] [CombatSolver/Debug] DEPLOY_END turn=1 "
            "action_count=1 end_turn=true",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "logs"
            log_dir.mkdir()
            log = log_dir / "godot.log"
            log.write_text("\n".join(lines) + "\n", encoding="utf-8")
            deploys = [
                event.deploy
                for event in LogTailSource(log_dir, replay=True).poll()
                if event.deploy is not None
            ]

        self.assertEqual(len(deploys), 1)
        self.assertEqual(
            [(action.kind, action.card_id, action.target_index) for action in deploys[0].actions],
            [("play", "BASH", 0)],
        )
        self.assertIsNotNone(deploys[0].log_range)
        self.assertEqual(deploys[0].log_range.marker_counts["DEPLOY_ACTION"], 1)

    def test_lone_cr_is_content_not_a_line_boundary(self) -> None:
        payload = (
            b"[CombatSolver/Test] SEARCH_REQUEST generation=1 turn=1\n"
            b"[CombatSolver/Test] DEPLOY_START turn=1\r"
            b"[CombatSolver/Test] DEPLOY_ACTION turn=1 card=BASH target_index=0\n"
            b"[CombatSolver/Test] DEPLOY_END turn=1 end_turn=true\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "logs"
            log_dir.mkdir()
            log = log_dir / "godot.log"
            log.write_bytes(payload)
            deploys = [
                event.deploy
                for event in LogTailSource(log_dir, replay=True).poll()
                if event.deploy is not None
            ]

        self.assertEqual(len(deploys), 1)
        self.assertEqual(deploys[0].actions, ())
        self.assertIsNotNone(deploys[0].log_range)
        self.assertEqual(deploys[0].log_range.marker_counts["DEPLOY_ACTION"], 0)

    def test_torn_tail_is_buffered_until_newline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "logs"
            log_dir.mkdir()
            log = log_dir / "godot.log"
            log.write_bytes(b"")
            source = LogTailSource(log_dir)
            self.assertEqual(source.poll(), [])
            prefix = (
                "[CombatSolver/Test] SEARCH_REQUEST generation=1 turn=1\n"
                "[CombatSolver/Test] DEPLOY_START turn=1\n"
                "[CombatSolver/Test] DEPLOY_ACTION turn=1 card=BASH target_index=0\n"
                "[CombatSolver/Test] DEPLOY_END turn="
            ).encode("utf-8")
            log.write_bytes(prefix)
            self.assertEqual([e for e in source.poll() if e.deploy], [])
            with log.open("ab") as handle:
                handle.write(b"1 end_turn=true\n")
            deploys = [e.deploy for e in source.poll() if e.deploy]
            self.assertEqual(len(deploys), 1)
            self.assertEqual(deploys[0].log_range.byte_end, log.stat().st_size)

    def test_same_path_rewrite_resets_parser_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "logs"
            log_dir.mkdir()
            log = log_dir / "godot.log"
            log.write_bytes(b"")
            source = LogTailSource(log_dir)
            source.poll()
            log.write_text(
                "[CombatSolver/Test] SEARCH_REQUEST generation=1 turn=1\n"
                "[CombatSolver/Test] DEPLOY_START turn=1\n",
                encoding="utf-8",
            )
            self.assertEqual([e for e in source.poll() if e.deploy], [])
            log.write_text(
                "[CombatSolver/Test] SEARCH_REQUEST generation=2 turn=2\n"
                "[CombatSolver/Test] DEPLOY_START turn=2\n"
                "[CombatSolver/Test] DEPLOY_END turn=2 end_turn=true\n",
                encoding="utf-8",
            )
            deploys = [e.deploy for e in source.poll() if e.deploy]
            self.assertEqual([deploy.turn for deploy in deploys], [2])
            self.assertEqual(deploys[0].log_range.marker_counts["DEPLOY_ACTION"], 0)

    def test_log_range_verification_rejects_tamper_and_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "logs"
            logs.mkdir()
            path = logs / "godot.log"
            payload = b"[CombatSolver/Test] SEARCH_REQUEST turn=1\n"
            path.write_bytes(payload)
            evidence = capture_log_range(path, 0, len(payload), allowed_root=logs)
            verified, raw, _events = verify_log_range(evidence, allowed_root=logs)
            self.assertEqual(verified, evidence)
            self.assertEqual(raw, payload)
            path.write_bytes(payload.replace(b"turn=1", b"turn=2"))
            with self.assertRaises(LogRangeError):
                verify_log_range(evidence, allowed_root=logs)
            outside = root / "outside.log"
            outside.write_bytes(payload)
            escaped = capture_log_range(outside, 0, len(payload))
            with self.assertRaises(LogRangeError):
                verify_log_range(escaped, allowed_root=logs)

    def test_log_range_schema_and_half_open_overlap(self) -> None:
        counts = {name: 0 for name in MARKER_EVENTS}
        left = LogRange("same.log", 0, 10, "0" * 64, counts)
        touching = LogRange("same.log", 10, 20, "1" * 64, counts)
        overlap = LogRange("same.log", 9, 20, "2" * 64, counts)
        self.assertFalse(ranges_overlap(left, touching))
        self.assertTrue(ranges_overlap(left, overlap))
        malformed = left.to_json()
        malformed["byte_start"] = True
        with self.assertRaises(LogRangeError):
            LogRange.from_json(malformed)

    def test_solver_line_detection_and_kv(self) -> None:
        self.assertTrue(is_solver_line(self.RESULT_LINE.format(reused="False")))
        self.assertFalse(is_solver_line("regular game log line"))
        kv = parse_kv_tokens(self.RESULT_LINE.format(reused="False"))
        self.assertEqual(kv["projected_battle_hp_lost"], "5")
        self.assertEqual(kv["total_worker_allocated_bytes"], "486933496")
        self.assertEqual(kv["reused"], "False")

    def _live_log(self, tmp: Path) -> tuple[Path, Path]:
        log_dir = tmp / "logs"
        log_dir.mkdir()
        log = log_dir / "godot.log"
        log.write_bytes(b"")
        return log_dir, log

    def _seed_active_deploy_block(self, log_dir: Path, log: Path) -> LogTailSource:
        source = LogTailSource(log_dir)
        self.assertEqual(source.poll(), [])
        log.write_text(
            "[CombatSolver/Test] SEARCH_REQUEST generation=1 turn=1\n"
            "[CombatSolver/Test] DEPLOY_START turn=1\n"
            "[CombatSolver/Test] NOTE padding=" + "x" * 80 + "\n",
            encoding="utf-8",
        )
        source.poll()
        self.assertEqual(source._offsets[log], log.stat().st_size)
        self.assertIsNotNone(source._last_search)
        self.assertEqual(source._deploy_range_starts[1][0], log)
        return source

    def test_short_replacement_persists_cursor_zero_and_clears_state(self) -> None:
        replacements = (
            b"",
            b"[CombatSolver/Test] SEARCH_REQUEST generation=2 turn=2",
        )
        for replacement in replacements:
            with self.subTest(replacement=replacement):
                with tempfile.TemporaryDirectory() as tmp:
                    log_dir, log = self._live_log(Path(tmp))
                    source = self._seed_active_deploy_block(log_dir, log)
                    self.assertGreater(source._offsets[log], len(replacement))
                    log.write_bytes(replacement)
                    self.assertEqual(source.poll(), [])
                    self.assertEqual(source._offsets[log], 0)
                    self.assertIsNone(source._last_search)
                    self.assertEqual(source._deploy_range_starts, {})
                    self.assertEqual(source._deploy_actions, {})

    def test_same_path_rewrite_kept_behind_anchor_is_prefix_hashed(self) -> None:
        tail = "[CombatSolver/Test] NOTE padding=" + "x" * 80 + "\n"

        def render(turn: int) -> str:
            return (
                f"[CombatSolver/Test] SEARCH_REQUEST generation={turn} turn={turn}\n"
                f"[CombatSolver/Test] DEPLOY_START turn={turn}\n"
                f"[CombatSolver/Test] DEPLOY_ACTION turn={turn} "
                "card=BASH target_index=0\n"
                + tail
            )

        original = render(1)
        rewritten = render(2)
        self.assertEqual(len(original), len(rewritten))
        self.assertEqual(
            original.encode("utf-8")[-64:], rewritten.encode("utf-8")[-64:]
        )
        with tempfile.TemporaryDirectory() as tmp:
            log_dir, log = self._live_log(Path(tmp))
            source = LogTailSource(log_dir)
            self.assertEqual(source.poll(), [])
            log.write_text(original, encoding="utf-8")
            source.poll()
            log.write_text(rewritten, encoding="utf-8")
            source.poll()
            self.assertIsNotNone(source._deploy_range_starts.get(2))
            with log.open("ab") as handle:
                handle.write(
                    b"[CombatSolver/Test] DEPLOY_END turn=2 end_turn=true\n"
                )
            deploys = [event.deploy for event in source.poll() if event.deploy]
            self.assertEqual([deploy.turn for deploy in deploys], [2])
            self.assertEqual([action.card_id for action in deploys[0].actions], ["BASH"])
            self.assertIsNotNone(deploys[0].log_range)
            self.assertEqual(deploys[0].log_range.byte_start, 0)

    def test_append_only_torn_tail_defers_until_newline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_dir, log = self._live_log(Path(tmp))
            source = LogTailSource(log_dir)
            self.assertEqual(source.poll(), [])
            log.write_bytes(
                b"[CombatSolver/Test] SEARCH_REQUEST generation=3 turn=7"
            )
            self.assertEqual(source.poll(), [])
            self.assertEqual(source._offsets[log], 0)
            with log.open("ab") as handle:
                handle.write(b" reason=AutoTurnStart\n")
            events = source.poll()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].failure.reason, "NO_ROUTE")
            self.assertEqual(events[0].failure.battle_turn, 7)
            self.assertEqual(source._offsets[log], log.stat().st_size)
            self.assertEqual(source.poll(), [])


class LogRangeGrammarTests(unittest.TestCase):
    def test_scan_markers_requires_exact_solver_event_prefix(self) -> None:
        payload = (
            b"[INFO] [OtherMod] DEPLOY_START turn=1\n"
            b"[CombatSolver/Test] note=DEPLOY_ACTION turn=1\n"
            b"[CombatSolver/Test] DEPLOY_START turn=1\n"
        )
        counts, events = scan_markers(payload)
        self.assertEqual(counts["DEPLOY_START"], 1)
        self.assertEqual(counts["DEPLOY_ACTION"], 0)
        self.assertEqual([marker for marker, _tokens in events], ["DEPLOY_START"])

    def test_validate_deploy_grammar_accepts_ordered(self) -> None:
        events = [
            ("SEARCH_REQUEST", {"turn": "2"}),
            ("DEPLOY_START", {"turn": "2"}),
            ("DEPLOY_ACTION", {"turn": "2"}),
            ("DEPLOY_END", {"turn": "2"}),
        ]
        validate_deploy_grammar(events, turn=2)

    def test_validate_deploy_grammar_rejects_out_of_order(self) -> None:
        cases = [
            [("DEPLOY_END", {"turn": "1"}), ("DEPLOY_START", {"turn": "1"})],
            [
                ("SEARCH_REQUEST", {"turn": "1"}),
                ("DEPLOY_ACTION", {"turn": "1"}),
                ("DEPLOY_START", {"turn": "1"}),
                ("DEPLOY_END", {"turn": "1"}),
            ],
            [
                ("SEARCH_REQUEST", {"turn": "1"}),
                ("DEPLOY_START", {"turn": "1"}),
                ("DEPLOY_END", {"turn": "1"}),
                ("DEPLOY_ACTION", {"turn": "1"}),
            ],
        ]
        for events in cases:
            with self.subTest(events=events), self.assertRaises(LogRangeError):
                validate_deploy_grammar(events, turn=1)

    def test_capture_requires_lf_aligned_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "godot.log"
            path.write_bytes(b"[CombatSolver/Test] DEPLOY_START turn=1\r")
            with self.assertRaises(LogRangeError):
                capture_log_range(path, 0, path.stat().st_size, allowed_root=root)
            payload = b"[CombatSolver/Test] DEPLOY_START turn=1\n"
            path.write_bytes(payload)
            with self.assertRaises(LogRangeError):
                capture_log_range(path, 0, len(payload) - 1, allowed_root=root)
            with self.assertRaises(LogRangeError):
                capture_log_range(path, 4, len(payload), allowed_root=root)

    def test_capture_rejects_symlinked_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "real.log"
            payload = b"[CombatSolver/Test] DEPLOY_START turn=1\n"
            target.write_bytes(payload)
            link = root / "link.log"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is not permitted on this platform")
            with self.assertRaises(LogRangeError):
                capture_log_range(link, 0, len(payload), allowed_root=root)

    def test_capture_rejects_embedded_nul_path(self) -> None:
        try:
            bad_path = Path("bad\x00path.log")
        except ValueError:
            self.skipTest("platform rejects embedded NUL in path")
        with self.assertRaises(LogRangeError):
            capture_log_range(bad_path, 0, 1)


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
