"""Grammar v2 regression tests: the CombatSolver JSON Lines journal.

Fixtures under ``tests/fixtures/combat_solver_v2`` are sanitized recordings of
the real 0.41.0 sessions on this machine: the record envelope, marker
vocabulary, field names, block grouping and deploy interleaving are the
producer's, while card titles, memory counters and absolute paths are not.
``tests/fixtures/combat_solver_v1/godot.log`` is a pre-0.41.0 Godot log.  Both
grammars must keep parsing their own source, and no test here reads
``%APPDATA%``.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from combat_solver.evidence import _validate_deploy_action_alignment
from combat_solver.logformat import GRAMMAR_AUTO, GRAMMAR_V1, GRAMMAR_V2, LogTailSource
from combat_solver.loggrammar import (
    EnvelopeError,
    SNIFF_UNDECIDED,
    V2_UNREACHABLE_REASONS,
    decode_v2_record,
    discover_v2_sources,
    sniff_grammar,
)
from combat_solver.logranges import (
    MARKER_EVENTS,
    MARKER_EVENTS_V2,
    LogRange,
    LogRangeError,
    capture_log_range,
    marker_events_for,
    scan_markers,
    validate_deploy_grammar,
    verify_log_range,
)
from combat_solver.logv2 import V1_ONLY_FAILURE_MARKERS
from combat_solver.reader import DeployRecord
from combat_solver.snapshot import FAILURE_REASONS, RouteAction

FIXTURES = Path(__file__).resolve().parent / "fixtures"
V1_DIR = FIXTURES / "combat_solver_v1"
V2_FIXTURES = FIXTURES / "combat_solver_v2"
V2_SESSION = V2_FIXTURES / "11111-0000000000000000000000000000ffff"

BATTLE_A = "a1a1a1a1b2b2c3c4d5d6e7e8f9f0a1a2"
BATTLE_B = "b1b1b1b1b2b2c3c4d5d6e7e8f9f0b1b2"
BATTLE_C = "c1c1c1c1b2b2c3c4d5d6e7e8f9f0c1c2"
BATTLE_D = "d1d1d1d1b2b2c3c4d5d6e7e8f9f0d1d2"
TRACE = "53d1abc39f82418da9bc2401906a949c"


def combat_name(battle_id: str) -> str:
    return f"combat-{battle_id}.jsonl"


def copy_session(tmp: Path, *, keep: tuple[str, ...] | None = None) -> Path:
    """Copy the v2 fixture into a writable directory (fixtures stay untouched)."""

    target = tmp / "CombatSolver" / V2_SESSION.name
    target.mkdir(parents=True, exist_ok=True)
    allowed = None if keep is None else {
        "process.jsonl" if item == "process" else combat_name(item) for item in keep
    }
    for path in sorted(V2_SESSION.iterdir()):
        if allowed is not None and path.name not in allowed:
            continue
        shutil.copyfile(path, target / path.name)
    return target


def replay(directory: Path, **kwargs) -> LogTailSource:
    return LogTailSource(directory, replay=True, **kwargs)


def split(events):
    return (
        [event.snapshot for event in events if event.snapshot is not None],
        [event.failure for event in events if event.failure is not None],
        [event.deploy for event in events if event.deploy is not None],
    )


def actions_of(snapshot) -> list[tuple]:
    return [
        action.comparable()
        for step in snapshot.route
        for action in step.actions
    ]


def summed_stats(source: LogTailSource) -> dict[str, int]:
    merged: dict[str, int] = {}
    for row in source.v2_stats().values():
        for key, value in row.items():
            if isinstance(value, int):
                merged[key] = merged.get(key, 0) + value
    return merged


class GrammarSniffingTests(unittest.TestCase):
    def test_container_is_chosen_by_content_not_by_name(self) -> None:
        self.assertEqual(sniff_grammar(V1_DIR / "godot.log"), GRAMMAR_V1)
        self.assertEqual(sniff_grammar(V2_SESSION / "process.jsonl"), GRAMMAR_V2)
        self.assertEqual(sniff_grammar(V2_SESSION / combat_name(BATTLE_A)), GRAMMAR_V2)
        with tempfile.TemporaryDirectory() as tmp:
            # a journal file renamed to .log is still read as a journal
            shutil.copyfile(V2_SESSION / combat_name(BATTLE_A), Path(tmp) / "captured.log")
            snapshots, failures, deploys = split(replay(Path(tmp)).poll())
            # the same answers as the in-place journal: the extension is ignored
            self.assertEqual([snapshot.battle_turn for snapshot in snapshots], [1, 2, 3, 5])
            self.assertEqual(
                [(failure.reason, failure.battle_turn) for failure in failures],
                [("NO_ROUTE", 3), ("TIMEOUT", 4)],
            )
            self.assertEqual([deploy.turn for deploy in deploys], [1, 2, 3])

    def test_undecidable_and_broken_first_records_are_distinguished(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "empty.log").write_text("", encoding="utf-8")
            self.assertEqual(sniff_grammar(root / "empty.log"), SNIFF_UNDECIDED)
            (root / "broken.jsonl").write_text(
                '{"Time":1,"Level":"info","Message":\n', encoding="utf-8"
            )
            self.assertIsNone(sniff_grammar(root / "broken.jsonl"))
            (root / "engine.log").write_text(
                "Godot v4.2 engine boot line\n", encoding="utf-8"
            )
            self.assertEqual(sniff_grammar(root / "engine.log"), GRAMMAR_V1)

    def test_broken_first_record_is_rejected_not_read_as_v1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "logs"
            log_dir.mkdir()
            (log_dir / "godot.log").write_text(
                '{"Time":1,"Level":"info","Message":\n', encoding="utf-8"
            )
            snapshots, failures, deploys = split(replay(log_dir).poll())
            self.assertEqual((snapshots, deploys), ([], []))
            self.assertEqual([failure.reason for failure in failures], ["READER_DOWN"])
            self.assertIn("no grammar classifies", failures[0].detail or "")
            # reported once per file, never on every poll
            self.assertEqual(replay_source_polls(log_dir), [])

    def test_pinned_grammar_refuses_the_other_container(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp) / "logs"
            session = copy_session(logs)
            pinned_v1 = replay(logs, grammar=GRAMMAR_V1).poll()
            snapshots, failures, deploys = split(pinned_v1)
            self.assertEqual((snapshots, deploys), ([], []))
            self.assertEqual([failure.reason for failure in failures], ["READER_DOWN"])
            self.assertIn("pinned to grammar v1", failures[0].detail or "")

            pinned_v2 = replay(logs / "CombatSolver" / session.name, grammar=GRAMMAR_V2).poll()
            self.assertEqual(
                [f.reason for f in split(pinned_v2)[1] if f.reason == "READER_DOWN"], []
            )
            with self.assertRaises(ValueError):
                LogTailSource(logs, grammar="v3")

    def test_v1_pinned_reader_still_reads_godot_logs(self) -> None:
        snapshots, failures, deploys = split(replay(V1_DIR, grammar=GRAMMAR_V1).poll())
        self.assertEqual([snapshot.battle_turn for snapshot in snapshots], [1, 2])
        self.assertEqual([failure.reason for failure in failures], ["SEARCH_ERROR"])
        self.assertEqual(len(deploys), 1)

    def test_default_reader_classifies_each_file_separately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp) / "logs"
            session = copy_session(logs)
            shutil.copyfile(V1_DIR / "godot.log", logs / "godot.log")
            source = replay(logs)
            self.assertEqual(source.grammar, GRAMMAR_AUTO)
            events = source.poll()
            self.assertEqual(source.file_grammar(logs / "godot.log"), GRAMMAR_V1)
            self.assertEqual(
                source.file_grammar(session / "process.jsonl"), GRAMMAR_V2
            )
            snapshots, _failures, deploys = split(events)
            self.assertEqual(
                sorted(
                    snapshot.provenance.source_file or "godot.log" for snapshot in snapshots
                ),
                sorted(
                    ["godot.log", "godot.log"]
                    + [
                        combat_name(BATTLE_A),
                        combat_name(BATTLE_A),
                        combat_name(BATTLE_A),
                        combat_name(BATTLE_A),
                        combat_name(BATTLE_B),
                    ]
                ),
            )
            self.assertTrue(
                any(deploy.log_range.grammar == GRAMMAR_V1 for deploy in deploys)
            )
            self.assertTrue(
                any(deploy.log_range.grammar == GRAMMAR_V2 for deploy in deploys)
            )


class EnvelopeUnwrapTests(unittest.TestCase):
    def test_single_line_record_and_level_are_decoded(self) -> None:
        record = decode_v2_record(
            json.dumps(
                {
                    "Time": 1789738007294,
                    "Level": "info",
                    "Message": "[CombatSolver/Test] DEPLOY_START turn=1 action_count=2",
                }
            )
        )
        self.assertEqual(record.level, "info")
        self.assertEqual(record.time_ms, 1789738007294)
        self.assertEqual(len(record.lines), 1)

    def test_multi_line_record_decodes_to_logical_lines_without_cr(self) -> None:
        record = decode_v2_record(
            json.dumps(
                {
                    "Time": 1,
                    "Level": "info",
                    "Message": "[CombatSolver/Test] RESULT reused=False\r\n"
                    "[CombatSolver/Test] ACTION turn=1 kind=EndTurn\r\n",
                }
            )
        )
        self.assertEqual(
            record.lines,
            (
                "[CombatSolver/Test] RESULT reused=False",
                "[CombatSolver/Test] ACTION turn=1 kind=EndTurn",
            ),
        )

    def test_malformed_envelopes_are_errors_never_guesses(self) -> None:
        broken = (
            "not json at all",
            "[CombatSolver/Test] RESULT reused=False",
            '{"Time":1,"Level":"info"}',
            '{"Time":1,"Message":"[CombatSolver/Test] RESET"}',
            '{"Time":1,"Level":"","Message":"x"}',
            '{"Time":1,"Level":"info","Message":""}',
            "[1, 2, 3]",
        )
        for payload in broken:
            with self.subTest(payload=payload[:40]), self.assertRaises(EnvelopeError):
                decode_v2_record(payload)

    def test_missing_time_is_accepted_but_not_synthesized(self) -> None:
        record = decode_v2_record(
            json.dumps({"Level": "info", "Message": "[CombatSolver/Test] RESET"})
        )
        self.assertIsNone(record.time_ms)

    def test_undecodable_record_inside_a_v2_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = copy_session(Path(tmp), keep=("process", BATTLE_A))
            combat = session / combat_name(BATTLE_A)
            with combat.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write('{"Time":1,"Level":"info","Message":42}\n')
            _snapshots, failures, _deploys = split(replay(session).poll())
            parse = [f for f in failures if f.reason == "PARSE_ERROR"]
            self.assertEqual(len(parse), 1)
            self.assertIn("not a decodable envelope", parse[0].detail or "")
            self.assertIn(combat.name, parse[0].detail or "")


class DiscoveryTests(unittest.TestCase):
    def test_process_first_then_combat_files_sorted_by_name(self) -> None:
        sources = discover_v2_sources(V2_SESSION)
        self.assertEqual([source.kind for source in sources][0], "process")
        self.assertEqual(
            [source.claimed_battle_id for source in sources][1:],
            sorted([BATTLE_A, BATTLE_B, BATTLE_C, BATTLE_D]),
        )
        self.assertTrue(all(source.session_dir == V2_SESSION for source in sources))

    def test_journal_tree_is_found_under_a_game_log_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp) / "logs"
            session = copy_session(logs)
            sources = discover_v2_sources(logs)
            self.assertEqual([source.path.parent for source in sources], [session] * 5)
            self.assertEqual(
                [(source.kind, source.path.name) for source in sources][0],
                ("process", "process.jsonl"),
            )
            # a second session is a second group: sessions ordered by directory
            # name, and process.jsonl ahead of its own combat files
            second = session.parent / "22222-0000000000000000000000000000fffe"
            shutil.copytree(session, second)
            found = discover_v2_sources(logs)
            self.assertEqual(len(found), 10)
            self.assertEqual(
                [source.path.parent.name for source in found],
                [session.name] * 5 + [second.name] * 5,
            )
            self.assertEqual([source.kind for source in found],
                             ["process"] + ["combat"] * 4 + ["process"] + ["combat"] * 4)

    def test_non_journal_files_are_not_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("settings.json", "notes.jsonl", "combat-not-a-guid.jsonl"):
                (root / name).write_text("{}\n", encoding="utf-8")
            (root / "process.jsonl").write_text("", encoding="utf-8")
            sources = discover_v2_sources(root)
            self.assertEqual([source.path.name for source in sources], ["process.jsonl"])
            self.assertIsNone(sources[0].claimed_battle_id)

    def test_live_tail_does_not_replay_history(self) -> None:
        self.assertEqual(LogTailSource(V2_SESSION).poll(), [])


class SessionBoundaryTests(unittest.TestCase):
    def test_battle_id_requires_the_producers_own_announcement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = copy_session(Path(tmp))
            source = replay(session)
            _snapshots, _failures, deploys = split(source.poll())
            self.assertEqual(
                source.battle_ids(),
                {
                    combat_name(BATTLE_A): BATTLE_A,
                    combat_name(BATTLE_B): BATTLE_B,
                    combat_name(BATTLE_D): BATTLE_D,
                },
            )
            self.assertEqual(
                {
                    Path(deploy.log_range.source_path).name: deploy.battle_log_id
                    for deploy in deploys
                },
                {
                    combat_name(BATTLE_A): BATTLE_A,
                    combat_name(BATTLE_B): BATTLE_B,
                },
            )

    def test_without_process_jsonl_no_battle_identity_is_claimed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = copy_session(Path(tmp), keep=(BATTLE_C,))
            source = replay(session)
            _snapshots, _failures, deploys = split(source.poll())
            self.assertEqual(source.battle_ids(), {})
            self.assertEqual([deploy.battle_log_id for deploy in deploys], [])

    def test_battle_id_of_an_unannounced_combat_stays_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = copy_session(Path(tmp), keep=("process", BATTLE_C))
            source = replay(session)
            events = source.poll()
            self.assertNotIn(combat_name(BATTLE_C), source.battle_ids())
            deploys = [event.deploy for event in events if event.deploy is not None]
            self.assertEqual(deploys, [])
            failures = [event.failure for event in events if event.failure is not None]
            self.assertEqual(
                sorted(failure.reason for failure in failures), ["CRASH", "NO_ROUTE"]
            )

    def test_combat_log_boundaries_are_counted_not_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = copy_session(Path(tmp))
            source = replay(session)
            source.poll()
            process = next(
                row
                for path, row in source.v2_stats().items()
                if Path(path).name == "process.jsonl"
            )
            self.assertEqual(process["combat_log_begin"], 3)
            self.assertEqual(process["combat_log_end"], 1)
            self.assertEqual(process["combat_log_end_reasons"], {"combat_ended": 1})
            self.assertEqual(process["error_level_records"], 0)

    def test_reset_anchors_flush_without_fabricating_a_crash(self) -> None:
        # 0.41.0 writes the combat's RESET before its last DEPLOY_END
        with tempfile.TemporaryDirectory() as tmp:
            session = copy_session(Path(tmp), keep=("process", BATTLE_A))
            _snapshots, failures, deploys = split(replay(session).poll())
            self.assertEqual([f.reason for f in failures if f.reason == "CRASH"], [])
            self.assertEqual([deploy.turn for deploy in deploys], [1, 2, 3])
            self.assertTrue(all(deploy.log_range is not None for deploy in deploys))


class SnapshotEmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.session = copy_session(Path(self._tmp.name), keep=("process", BATTLE_A))
        self.source = replay(self.session, mod_version="0.41.0")
        self.snapshots, self.failures, self.deploys = split(self.source.poll())

    def test_each_answer_binds_to_its_request_turn(self) -> None:
        self.assertEqual(
            [snapshot.battle_turn for snapshot in self.snapshots], [1, 2, 3, 5]
        )
        first = self.snapshots[0]
        self.assertEqual(first.provenance.source_file, combat_name(BATTLE_A))
        self.assertEqual(first.provenance.mod_version, "0.41.0")
        self.assertIsNone(first.state_hash)
        self.assertEqual(first.predicted.hp_loss, 4)
        self.assertEqual(first.predicted.hp_end, 44)
        self.assertEqual(first.budget.elapsed_ms, 13862)
        self.assertEqual(first.budget.nodes_expanded, 11016)
        self.assertEqual(first.budget.peak_memory_mb, 13997)
        self.assertEqual(first.provenance.reader, "logtail")

    def test_reused_answer_keeps_the_full_route_and_the_new_turn(self) -> None:
        # the v1 "re-bind a reused package to its first action turn" rule would
        # label every answer of this battle turn 1, because v2 blocks always
        # carry the whole battle-length route starting at turn 1.  The producer's
        # own Turn fields run this plan to turn 2 (ROUTE_ACTION index 3 is the
        # EndTurn with "Turn": 2, and the RESULT ACTION lines agree), so the point
        # is that a reused answer keeps the whole route while reporting its own
        # battle turn -- not that the route is a single step.
        by_turn = {snapshot.battle_turn: snapshot for snapshot in self.snapshots}
        self.assertEqual([step.turn for step in by_turn[1].route], [1, 2])
        self.assertEqual([step.turn for step in by_turn[2].route], [1, 2])
        self.assertEqual(actions_of(by_turn[2]), actions_of(by_turn[1]))
        self.assertIsNone(by_turn[2].budget.elapsed_ms)
        self.assertIsNone(by_turn[2].budget.nodes_expanded)

    def test_empty_route_is_emitted_and_counted(self) -> None:
        empty = [snapshot for snapshot in self.snapshots if not snapshot.route]
        self.assertEqual([snapshot.battle_turn for snapshot in empty], [5])
        self.assertEqual(summed_stats(self.source)["empty_routes"], 1)

    def test_capture_time_is_the_readers_clock_not_the_producers(self) -> None:
        for snapshot in self.snapshots:
            self.assertTrue(snapshot.provenance.captured_at_utc.endswith("+00:00"))
            self.assertLess(snapshot.provenance.captured_at_utc[:4], "2100")

    def test_turn_four_answer_never_appears_as_a_snapshot(self) -> None:
        self.assertNotIn(4, [snapshot.battle_turn for snapshot in self.snapshots])
        self.assertEqual(
            [(f.reason, f.battle_turn) for f in self.failures if f.battle_turn == 4],
            [("TIMEOUT", 4)],
        )


class EvidenceChannelTests(unittest.TestCase):
    def test_route_is_taken_from_the_trace_only_by_the_answer_that_owns_it(self) -> None:
        # Measured on the 10 real 0.41.0 combats on this machine that contain RESULT
        # records: every one of them emits its ROUTE_ACTION records in a single burst
        # inside the window of the first non-reused RESULT, and zero records inside
        # every later window (those later answers all carry reused=True plus a
        # SEARCH_REUSED line).  So "every answer has a trace" is not a producer
        # behaviour and must not be asserted -- but the flip side is the part that
        # matters for acceptance: an answer may cite a trace only when its own window
        # carried one, even when an earlier answer traced an identical route.
        with tempfile.TemporaryDirectory() as tmp:
            session_a = copy_session(Path(tmp) / "a", keep=("process", BATTLE_A))
            source = replay(session_a)
            snapshots, _failures, _deploys = split(source.poll())
            by_turn = {snapshot.battle_turn: snapshot for snapshot in snapshots}
            owned = [action.note
                     for step in by_turn[1].route for action in step.actions]
            self.assertTrue(owned)
            self.assertTrue(
                all(str(note).startswith(f"evidence:{TRACE}#") for note in owned)
            )
            self.assertEqual(sorted(int(str(note).split("#")[1]) for note in owned),
                             list(range(len(owned))))

            # turn 2 scraped the very same four-action route turn 1 traced, from a
            # window with no evidence records of its own.  It keeps the route and
            # must not inherit the trace id.
            self.assertEqual(actions_of(by_turn[2]), actions_of(by_turn[1]))
            reused = [action.note
                      for step in by_turn[2].route for action in step.actions]
            self.assertEqual(reused, [None] * len(reused))
            later = [action.note
                     for step in by_turn[3].route for action in step.actions]
            self.assertEqual(later, [None] * len(later))

            self.assertEqual(summed_stats(source)["evidence_adopted"], 1)
            self.assertEqual(summed_stats(source)["answers_without_replay_validation"], 3)
            potion = [
                action
                for step in by_turn[1].route
                for action in step.actions
                if action.kind == "potion"
            ]
            self.assertEqual([action.card_id for action in potion], ["SKILL_POTION"])

    def test_two_identical_traces_leave_the_scraped_route_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_b = copy_session(Path(tmp) / "b", keep=("process", BATTLE_B))
            source_b = replay(session_b)
            snapshots, failures, deploys = split(source_b.poll())
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(failures, [])  # the re-armed request is not a miss
            self.assertEqual(
                [(step.turn, [a.comparable() for a in step.actions])
                 for step in snapshots[0].route],
                [(1, [("play", "FEEL_NO_PAIN", None), ("play", "BASH", 0),
                      ("end_turn", None, None)])],
            )
            self.assertEqual(summed_stats(source_b)["evidence_ambiguous"], 1)
            self.assertEqual(summed_stats(source_b)["evidence_adopted"], 0)
            self.assertEqual(len(deploys), 1)

    def test_evidence_never_upgrades_execution_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = copy_session(Path(tmp), keep=("process", BATTLE_A))
            source = replay(session)
            deploys = [event.deploy for event in source.poll() if event.deploy is not None]
            expected = {
                1: [("play", "FEEL_NO_PAIN", None), ("play", "BASH", 0)],
                2: [("potion", "SKILL_POTION", None)],
                3: [("play", "DEFEND", None)],
            }
            self.assertEqual(
                {deploy.turn: [(a.kind, a.card_id, a.target_index) for a in deploy.actions]
                 for deploy in deploys},
                expected,
            )
            # the deploy record is the mod's own producer log, nothing else
            self.assertTrue(
                all(deploy.log_range.grammar == GRAMMAR_V2 for deploy in deploys)
            )
            self.assertGreater(summed_stats(source)["route_actions"], 0)
            self.assertEqual(summed_stats(source)["route_health"], 1)

    def test_diverged_replay_suppresses_the_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = copy_session(Path(tmp), keep=("process", BATTLE_D))
            source = replay(session)
            snapshots, failures, deploys = split(source.poll())
            self.assertEqual((snapshots, deploys), ([], []))
            self.assertEqual([failure.reason for failure in failures], ["NO_ROUTE"])
            self.assertIn("did not reproduce the published route", failures[0].detail or "")
            self.assertIn(BATTLE_D[:8], failures[0].detail or "")

    def test_route_health_is_counted_but_never_substituted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = copy_session(Path(tmp), keep=("process", BATTLE_A))
            source = replay(session)
            snapshots, _failures, _deploys = split(source.poll())
            self.assertEqual(summed_stats(source)["route_health"], 1)
            by_turn = {snapshot.battle_turn: snapshot for snapshot in snapshots}
            # TURN_OUTCOME, not ROUTE_HEALTH, is the player-side per-turn loss.
            # The producer reports the outcome pair for every searched turn
            # inside the RESULT record itself (this fixture searched two turns:
            # turn=1 hp_lost=4 and turn=2 hp_lost=0), so the snapshot covers
            # both. Expecting only turn 1 would drop a value the mod did emit.
            self.assertEqual(
                [step.predicted_hp_lost for step in by_turn[1].route], [4, 0]
            )

    def test_unusable_route_action_is_counted_not_invented(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = copy_session(Path(tmp), keep=("process", BATTLE_A))
            combat = session / combat_name(BATTLE_A)
            with combat.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(
                    json.dumps(
                        {
                            "Time": 1789739000000,
                            "Level": "info",
                            "Message": '[CombatSolver/Evidence] ROUTE_ACTION '
                            '{"traceId":"t1","index":0,"action":{"Kind":"Reveal",'
                            '"Turn":9}}',
                        }
                    )
                    + "\n"
                )
            source = replay(session)
            _snapshots, _failures, _deploys = split(source.poll())
            self.assertEqual(summed_stats(source)["unusable_route_actions"], 1)


class DeployBindingTests(unittest.TestCase):
    def test_ranges_address_the_real_file_and_the_same_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = copy_session(Path(tmp))
            deploys = [
                event.deploy for event in replay(session).poll() if event.deploy is not None
            ]
            self.assertEqual(len(deploys), 4)
            for deploy in deploys:
                with self.subTest(source=Path(deploy.log_range.source_path).name,
                                  turn=deploy.turn):
                    self.assertIsInstance(deploy, DeployRecord)
                    verified, payload, marker_events = verify_log_range(
                        deploy.log_range.to_json(), allowed_root=session
                    )
                    self.assertEqual(verified, deploy.log_range)
                    self.assertEqual(verified.grammar, GRAMMAR_V2)
                    disk = Path(verified.source_path).read_bytes()
                    self.assertEqual(disk[verified.byte_start : verified.byte_end], payload)
                    self.assertTrue(
                        verified.byte_start == 0
                        or disk[verified.byte_start - 1 : verified.byte_start] == b"\n"
                    )
                    self.assertTrue(payload.endswith(b"\n"))
                    self.assertEqual(
                        verified.sha256, hashlib.sha256(payload).hexdigest().upper()
                    )
                    counts, rescanned = scan_markers(payload, grammar=GRAMMAR_V2)
                    self.assertEqual(counts, verified.marker_counts)
                    self.assertEqual(rescanned, marker_events)
                    self.assertEqual(verified.marker_counts["DEPLOY_START"], 1)
                    self.assertEqual(verified.marker_counts["DEPLOY_END"], 1)
                    self.assertEqual(
                        verified.marker_counts["DEPLOY_ACTION"], len(deploy.actions)
                    )
                    validate_deploy_grammar(
                        marker_events, turn=deploy.turn, grammar=GRAMMAR_V2
                    )
                    _validate_deploy_action_alignment(
                        marker_events,
                        [action.to_json() for action in deploy.actions],
                        battle_id="fixture",
                        turn=deploy.turn,
                    )

    def test_v1_validation_rejects_a_v2_range(self) -> None:
        # the reuse announcement must not be quietly accepted as a SEARCH_REQUEST
        with tempfile.TemporaryDirectory() as tmp:
            session = copy_session(Path(tmp), keep=("process", BATTLE_A))
            deploys = [
                event.deploy for event in replay(session).poll() if event.deploy is not None
            ]
            reused = next(deploy for deploy in deploys if deploy.turn == 2)
            _verified, _payload, events = verify_log_range(
                reused.log_range.to_json(), allowed_root=session
            )
            self.assertEqual([marker for marker, _tokens in events][0], "SEARCH_REUSED")
            with self.assertRaises(LogRangeError):
                validate_deploy_grammar(events, turn=2, grammar=GRAMMAR_V1)
            validate_deploy_grammar(events, turn=2, grammar=GRAMMAR_V2)
            with self.assertRaises(LogRangeError):
                validate_deploy_grammar(events, turn=3, grammar=GRAMMAR_V2)

    def test_tampering_with_a_v2_range_still_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = copy_session(Path(tmp), keep=("process", BATTLE_A))
            deploys = [
                event.deploy for event in replay(session).poll() if event.deploy is not None
            ]
            original = deploys[0].log_range
            combat = session / combat_name(BATTLE_A)
            payload = combat.read_bytes()
            tampered = payload.replace(b"card=BASH", b"card=FEEL_THE_BURN", 1)
            self.assertNotEqual(tampered, payload)
            combat.write_bytes(tampered)
            with self.assertRaises(LogRangeError):
                verify_log_range(original.to_json(), allowed_root=session)

    def test_range_schema_keeps_the_two_marker_sets_apart(self) -> None:
        v2_counts = {name: 0 for name in MARKER_EVENTS_V2}
        v1_counts = {name: 0 for name in MARKER_EVENTS}
        v2 = LogRange("combat-x.jsonl", 0, 10, "A" * 64, v2_counts, GRAMMAR_V2)
        self.assertEqual(v2.to_json()["grammar"], GRAMMAR_V2)
        self.assertEqual(
            set(LogRange.from_json(v2.to_json()).marker_counts), set(MARKER_EVENTS_V2)
        )
        historical = {
            "source_path": "godot.log",
            "byte_start": 0,
            "byte_end": 10,
            "sha256": "A" * 64,
            "marker_counts": v1_counts,
        }
        loaded = LogRange.from_json(historical)
        self.assertEqual(loaded.grammar, GRAMMAR_V1)
        self.assertEqual(set(loaded.marker_counts), set(MARKER_EVENTS))
        with self.assertRaises(LogRangeError):
            LogRange.from_json(dict(historical, grammar=GRAMMAR_V2))
        with self.assertRaises(LogRangeError):
            LogRange.from_json(dict(v2.to_json(), grammar="v9"))
        with self.assertRaises(LogRangeError):
            capture_log_range(V1_DIR / "godot.log", 0, 10, grammar="v9")
        self.assertEqual(marker_events_for(GRAMMAR_V2), MARKER_EVENTS_V2)
        self.assertEqual(marker_events_for(GRAMMAR_V1), MARKER_EVENTS)

    def test_embedded_markers_are_counted_by_the_v2_scanner(self) -> None:
        line = json.dumps(
            {
                "Time": 1,
                "Level": "info",
                "Message": "[CombatSolver/Test] RESULT reused=False\r\n"
                "[CombatSolver/Test] DEPLOY_ACTION turn=1 card=BASH target_index=0",
            }
        ).encode("utf-8") + b"\n"
        v1_counts, _events = scan_markers(line)
        v2_counts, v2_events = scan_markers(line, grammar=GRAMMAR_V2)
        self.assertEqual(v1_counts["DEPLOY_ACTION"], 0)
        self.assertEqual(v2_counts["DEPLOY_ACTION"], 1)
        self.assertEqual(v2_events[0][1]["card"], "BASH")
        self.assertEqual(v2_events[0][1]["target_index"], "0")


class FailureTaxonomyTests(unittest.TestCase):
    def test_v2_emits_only_the_mapped_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = copy_session(Path(tmp))
            _snapshots, failures, _deploys = split(replay(session).poll())
            reasons = sorted({failure.reason for failure in failures})
            self.assertEqual(reasons, ["CRASH", "NO_ROUTE", "TIMEOUT"])
            for reason in V2_UNREACHABLE_REASONS:
                self.assertNotIn(reason, reasons)
            self.assertEqual(
                sorted(
                    (failure.battle_turn, failure.reason)
                    for failure in failures
                    if failure.reason != "READER_DOWN"
                ),
                [
                    (1, "CRASH"),
                    (1, "NO_ROUTE"),
                    (1, "NO_ROUTE"),
                    (3, "NO_ROUTE"),
                    (4, "TIMEOUT"),
                ],
            )

    def test_death_route_keeps_the_prediction_and_flags_the_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = copy_session(Path(tmp), keep=("process", BATTLE_A))
            snapshots, failures, deploys = split(replay(session).poll())
            doomed = [f for f in failures if "only_death_routes" in (f.detail or "")]
            self.assertEqual([f.battle_turn for f in doomed], [3])
            self.assertEqual(doomed[0].reason, "NO_ROUTE")
            self.assertIn("death_turn=3", doomed[0].detail or "")
            self.assertIn("final_hp=0", doomed[0].detail or "")
            kept = [snapshot for snapshot in snapshots if snapshot.battle_turn == 3]
            self.assertEqual(len(kept), 1)
            self.assertEqual(kept[0].predicted.hp_end, 0)
            self.assertEqual(kept[0].predicted.hp_loss, 81)
            self.assertEqual([deploy.turn for deploy in deploys if deploy.turn == 3], [3])

    def test_budget_marker_upgrades_an_unanswered_request_to_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = copy_session(Path(tmp), keep=("process", BATTLE_A))
            _snapshots, failures, _deploys = split(replay(session).poll())
            timeout = [f for f in failures if f.reason == "TIMEOUT"]
            self.assertEqual([f.battle_turn for f in timeout], [4])
            self.assertIn("budget exhausted", timeout[0].detail or "")

    def test_v1_failure_marker_in_a_v2_journal_is_reported_not_mapped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = copy_session(Path(tmp), keep=("process", BATTLE_A))
            combat = session / combat_name(BATTLE_A)
            with combat.open("a", encoding="utf-8", newline="\n") as handle:
                for marker in V1_ONLY_FAILURE_MARKERS:
                    handle.write(
                        json.dumps(
                            {
                                "Time": 1789738999999,
                                "Level": "error",
                                "Message": f"[CombatSolver/Test] {marker} generation=99",
                            }
                        )
                        + "\n"
                    )
            _snapshots, failures, _deploys = split(replay(session).poll())
            mapped = [f for f in failures if "no calibrated mapping" in (f.detail or "")]
            self.assertEqual(len(mapped), 2)
            self.assertEqual({f.reason for f in mapped}, {"PARSE_ERROR"})
            emitted = {f.reason for f in failures}
            self.assertNotIn("SEARCH_ERROR", emitted)
            self.assertNotIn("STALE_STATE", emitted)

    def test_unreachable_reasons_are_part_of_the_pinned_contract(self) -> None:
        self.assertEqual(V2_UNREACHABLE_REASONS, ("SEARCH_ERROR", "STALE_STATE"))
        for reason in V2_UNREACHABLE_REASONS:
            self.assertIn(reason, FAILURE_REASONS)

    def test_result_without_a_request_turn_is_an_error_not_a_turn_guess(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = copy_session(Path(tmp), keep=("process", BATTLE_A))
            combat = session / combat_name(BATTLE_A)
            with combat.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(
                    json.dumps(
                        {
                            "Time": 1789739000000,
                            "Level": "info",
                            "Message": "[CombatSolver/Test] RESULT reused=False "
                            "reused_from_turn=- searched_turns=1 "
                            "projected_battle_hp_lost=7 final_hp=41 "
                            "only_death_routes=False",
                        }
                    )
                    + "\n"
                )
            _snapshots, failures, _deploys = split(replay(session).poll())
            self.assertEqual(
                [
                    f.reason
                    for f in failures
                    if "without a turn-labelled request" in (f.detail or "")
                ],
                ["PARSE_ERROR"],
            )

    def test_identical_answer_republished_in_one_window_is_one_snapshot(self) -> None:
        # What is under test is that an echo stays invisible: the snapshot list and
        # the failure list must come out identical to the same session without the
        # appended record.  "No failures at all" is not the property -- this fixture
        # legitimately reports a death-route NO_ROUTE and a TIMEOUT on its own.
        with tempfile.TemporaryDirectory() as tmp:
            pristine = copy_session(Path(tmp) / "pristine", keep=("process", BATTLE_A))
            pristine_source = replay(pristine)
            pristine_snapshots, pristine_failures, _ = split(pristine_source.poll())
            pristine_key = (
                [snapshot.battle_turn for snapshot in pristine_snapshots],
                [(failure.reason, failure.battle_turn) for failure in pristine_failures],
            )
            self.assertEqual(pristine_key[1], [("NO_ROUTE", 3), ("TIMEOUT", 4)])

            session = copy_session(Path(tmp) / "echoed", keep=("process", BATTLE_A))
            combat = session / combat_name(BATTLE_A)
            records = combat.read_text(encoding="utf-8").splitlines(keepends=True)
            result_records = [
                line for line in records if '"[CombatSolver/Test] RESULT' in line
            ]
            with combat.open("a", encoding="utf-8", newline="\n") as handle:
                handle.writelines([result_records[0]])
            source = replay(session)
            snapshots, failures, _deploys = split(source.poll())
            self.assertEqual(
                (
                    [snapshot.battle_turn for snapshot in snapshots],
                    [(failure.reason, failure.battle_turn) for failure in failures],
                ),
                pristine_key,
            )
            self.assertEqual(
                [snapshot.battle_turn for snapshot in snapshots].count(1), 1
            )
            self.assertEqual(summed_stats(source)["suppressed_echoes"], 1)
            self.assertEqual(summed_stats(pristine_source)["suppressed_echoes"], 0)


class IncrementalPollTests(unittest.TestCase):
    def test_blocks_can_span_polls_and_ranges_stay_valid(self) -> None:
        # A live tail first meets every file at its EOF, so a real batch has to have
        # the reader running before the producer writes.  Split one combat across
        # three polls and require the accumulated stream to equal what a single
        # replay of the same bytes yields -- same snapshots, same failures, same
        # deploys, byte ranges still verifiable, identity still bound.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs" / "CombatSolver" / "live-session"
            root.mkdir(parents=True)
            process = root / "process.jsonl"
            combat = root / combat_name(BATTLE_A)
            process.write_text("", encoding="utf-8", newline="\n")
            combat.write_text("", encoding="utf-8", newline="\n")
            records = (V2_SESSION / combat_name(BATTLE_A)).read_text(
                encoding="utf-8"
            ).splitlines(keepends=True)
            begin = [
                line
                for line in (V2_SESSION / "process.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines(keepends=True)
                if f"COMBAT_LOG_BEGIN id={BATTLE_A}" in line
            ]
            deploy_start = next(i for i, line in enumerate(records) if "DEPLOY_START" in line)
            deploy_end = next(i for i, line in enumerate(records) if "DEPLOY_END" in line)

            source = LogTailSource(root.parent.parent)
            self.assertEqual(source.poll(), [])  # history is never replayed

            def append(lines: list[str]) -> None:
                with combat.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.writelines(lines)

            def append_process(lines: list[str]) -> None:
                with process.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.writelines(lines)

            seen: list = []

            append_process(begin)
            append(records[:deploy_start])
            polled = source.poll()
            seen.extend(polled)
            first = split(polled)
            self.assertEqual([snapshot.battle_turn for snapshot in first[0]], [1])
            self.assertEqual(first[2], [])  # nothing deployed yet

            append(records[deploy_start : deploy_end + 1])
            polled = source.poll()
            seen.extend(polled)
            second = split(polled)
            self.assertEqual(second[0], [])
            self.assertEqual(second[1], [])
            self.assertEqual([deploy.turn for deploy in second[2]], [1])
            self.assertEqual(second[2][0].battle_log_id, BATTLE_A)
            deploy = second[2][0]
            _verified, payload, marker_events = verify_log_range(
                deploy.log_range.to_json(), allowed_root=root.parent.parent
            )
            validate_deploy_grammar(marker_events, turn=1, grammar=GRAMMAR_V2)
            self.assertEqual(payload.count(b"DEPLOY_ACTION"), 2)

            append(records[deploy_end + 1 :])
            polled = source.poll()
            seen.extend(polled)
            third = split(polled)
            self.assertEqual([deploy.turn for deploy in third[2]], [2, 3])
            self.assertEqual(
                [failure.battle_turn for failure in third[1]], [3, 4]
            )
            self.assertEqual(
                [failure.reason for failure in third[1]], ["NO_ROUTE", "TIMEOUT"]
            )

            reference_session = copy_session(
                Path(tmp) / "reference", keep=("process", BATTLE_A)
            )
            reference = split(replay(reference_session).poll())
            live = split(seen)
            self.assertEqual(
                [snapshot.battle_turn for snapshot in live[0]],
                [snapshot.battle_turn for snapshot in reference[0]],
            )
            self.assertEqual(
                [(failure.reason, failure.battle_turn) for failure in live[1]],
                [(failure.reason, failure.battle_turn) for failure in reference[1]],
            )
            self.assertEqual(
                [deploy.turn for deploy in live[2]],
                [deploy.turn for deploy in reference[2]],
            )


class V1StillV1Tests(unittest.TestCase):
    """The historical container must keep producing the historical answers."""

    def setUp(self) -> None:
        self.snapshots, self.failures, self.deploys = split(
            replay(V1_DIR, mod_version="0.31.0").poll()
        )

    def test_v1_block_machine_is_unchanged(self) -> None:
        self.assertEqual([snapshot.battle_turn for snapshot in self.snapshots], [1, 2])
        first, second = self.snapshots
        self.assertEqual(
            [
                (step.turn, [action.comparable() for action in step.actions])
                for step in first.route
            ],
            [
                (1, [("play", "BASH", 0), ("play", "DEFEND", None)]),
                (2, [("end_turn", None, None)]),
            ],
        )
        self.assertEqual(first.predicted.hp_loss, 5)
        self.assertEqual(first.predicted.hp_end, 55)
        self.assertEqual(first.budget.elapsed_ms, 1119)
        self.assertEqual(first.budget.short_elapsed_ms, 1119)
        self.assertEqual(first.budget.peak_memory_mb, 464)
        self.assertEqual(first.budget.process_working_set_mb, 4096)
        self.assertEqual(first.route[0].predicted_hp_lost, 2)
        self.assertEqual(first.route[1].predicted_hp_lost, 3)
        self.assertIsNone(first.provenance.source_file)
        # a reused package re-binds to its first action turn and carries no cost
        self.assertEqual(second.battle_turn, 2)
        self.assertEqual(actions_of(second), [("play", "STRIKE", 0), ("end_turn", None, None)])
        self.assertIsNone(second.budget.elapsed_ms)
        self.assertIsNone(second.budget.nodes_expanded)

    def test_v1_failure_and_deploy_markers_keep_their_meaning(self) -> None:
        self.assertEqual(
            [(failure.reason, failure.battle_turn) for failure in self.failures],
            [("SEARCH_ERROR", 3)],
        )
        deploy = self.deploys[0]
        self.assertEqual(deploy.turn, 1)
        self.assertTrue(deploy.end_turn)
        self.assertIsNone(deploy.battle_log_id)
        self.assertEqual(
            [(action.kind, action.card_id) for action in deploy.actions],
            [("play", "BASH"), ("potion", "SKILL_POTION")],
        )
        self.assertEqual(deploy.log_range.grammar, GRAMMAR_V1)
        self.assertEqual(
            deploy.log_range.marker_counts,
            {
                "SEARCH_REQUEST": 1,
                "DEPLOY_START": 1,
                "DEPLOY_ACTION": 2,
                "DEPLOY_END": 1,
            },
        )
        _verified, payload, marker_events = verify_log_range(
            deploy.log_range.to_json(), allowed_root=V1_DIR
        )
        validate_deploy_grammar(marker_events, turn=1)
        # the range starts at the SEARCH_REQUEST that produced this deploy, so it
        # covers the whole v1 block: request, RESULT, forecast/outcomes, deploys
        self.assertEqual(len(payload.splitlines()), 14)
        self.assertIn(b"SEARCH_REQUEST generation=1", payload)

    def test_v2_only_markers_are_inert_in_v1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "logs"
            log_dir.mkdir()
            (log_dir / "godot.log").write_text(
                "[INFO] [CombatSolver] [CombatSolver/Test] SEARCH_REQUEST "
                "generation=1 turn=1\n"
                "[INFO] [CombatSolver] [CombatSolver/Test] SEARCH_REUSED "
                "from_turn=1 turn=1\n"
                "[INFO] [CombatSolver] [CombatSolver/Test] TURN_SETUP_SEARCH_START "
                "turn=1\n"
                '[CombatSolver/Evidence] ROUTE_ACTION {"traceId":"t","index":0,'
                '"action":{"Kind":"PlayCard","Turn":1,"CardId":"BASH","TargetIndex":0}}\n',
                encoding="utf-8",
            )
            snapshots, failures, deploys = split(replay(log_dir).poll())
            self.assertEqual((snapshots, deploys), ([], []))
            # the unanswered request still flushes exactly as v1 says it should
            self.assertEqual(
                [(failure.reason, failure.battle_turn) for failure in failures],
                [("NO_ROUTE", 1)],
            )


def replay_source_polls(log_dir: Path) -> list:
    source = LogTailSource(log_dir, replay=True)
    source.poll()
    return source.poll()


if __name__ == "__main__":
    unittest.main()
