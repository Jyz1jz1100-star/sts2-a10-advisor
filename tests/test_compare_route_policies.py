from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping

from advisor_core.contracts import Candidate, Recommendation
from advisor_core.live_candidate_codec import LIVE_BUILD
from advisor_core.policy_live import validated_wire_action
from advisor_core.route_planner import RoutePlannerPolicy
from scripts.compare_route_policies import (
    RouteComparisonError,
    compare_trace_files,
    main,
    stable_state_hash,
)


def map_state(*, hp: int = 64, message: str | None = None) -> dict[str, Any]:
    state: dict[str, Any] = {
        "state_type": "map",
        "run": {
            "run_id": "modded:profile1:private-run",
            "act": 1,
            "floor": 1,
            "ascension": 10,
        },
        "player": {
            "character_id": "IRONCLAD",
            "character": "铁甲战士",
            "hp": hp,
            "max_hp": 80,
            "draw_pile": [
                {"id": "STRIKE_IRONCLAD"},
                {"id": "DEFEND_IRONCLAD"},
            ],
            "discard_pile": [],
        },
        "map": {
            "current_position": {"col": 3, "row": 0, "type": "Ancient"},
            "next_options": [
                {
                    "index": 0,
                    "col": 1,
                    "row": 1,
                    "type": "Monster",
                    "leads_to": [{"col": 1, "row": 2, "type": "RestSite"}],
                },
                {
                    "index": 1,
                    "col": 6,
                    "row": 1,
                    "type": "RestSite",
                    "leads_to": [{"col": 6, "row": 2, "type": "Elite"}],
                },
            ],
            "nodes": [
                {"col": 3, "row": 0, "type": "Ancient", "children": [[1, 1], [6, 1]]}
            ],
            "visited": [{"col": 3, "row": 0, "type": "Ancient"}],
        },
    }
    if message is not None:
        state["message"] = message
    return state


class FakeRoutePlanner:
    model_id = "route-lookahead-v1"

    def recommend(self, state: dict[str, Any]) -> Recommendation:
        primary = Candidate(
            action={"type": "map_choose_node", "index": 0},
            label="route option 0",
            score=8.25,
            facts=("future score",),
            metrics={
                "immediate_score": 3.0,
                "future_score": 5.25,
                "total_score": 8.25,
                "lookahead_depth": 4.0,
                "known_path_nodes": 3.0,
            },
        )
        alternative = Candidate(
            action={"type": "map_choose_node", "index": 1},
            label="route option 1",
            score=4.0,
        )
        return Recommendation(
            phase="map",
            primary=primary,
            alternatives=(alternative,),
            model_id=self.model_id,
            game_build="public-beta-v0.111.0",
            search_nodes=3,
        )

    @staticmethod
    def wire_action_for(
        state: Mapping[str, Any], action: Mapping[str, Any]
    ) -> dict[str, Any]:
        return validated_wire_action(state, action)


def write_events(path: Path, events: list[Any]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for event in events:
            if isinstance(event, str):
                handle.write(event + "\n")
            else:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")


class RouteComparisonTests(unittest.TestCase):
    def test_stable_hash_ignores_transient_message_and_hidden_pile_order(self) -> None:
        first = map_state(message="poll 1")
        second = map_state(message="poll 2")
        second["player"]["draw_pile"] = list(reversed(second["player"]["draw_pile"]))
        self.assertEqual(stable_state_hash(first), stable_state_hash(second))

    def test_streams_states_drops_duplicates_and_reports_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "raw.jsonl"
            output = root / "comparison.jsonl"
            first = map_state(message="first poll")
            duplicate = map_state(message="second poll")
            changed = map_state(hp=30, message="changed state")
            events = [
                {"event_type": "session", "session_id": "session-private", "raw": {}},
                {"event_type": "state", "session_id": "session-private", "sequence": 1,
                 "decision_id": "d1", "raw": first},
                {"event_type": "state", "session_id": "session-private", "sequence": 2,
                 "decision_id": "d1", "raw": first},
                {"event_type": "state", "session_id": "session-private", "sequence": 3,
                 "decision_id": "d2", "raw": duplicate},
                # This result is deliberately after the state.  It must not
                # reach either policy or appear as a strategy feature.
                {"event_type": "result", "session_id": "session-private", "sequence": 4,
                 "decision_id": "d1", "raw": {"status": "terminal", "win": True}},
                {"event_type": "state", "session_id": "session-private", "sequence": 5,
                 "decision_id": "d3", "raw": changed},
                {"event_type": "state", "session_id": "session-private", "sequence": 6,
                 "decision_id": "d3", "raw": changed},
            ]
            write_events(source, events)
            report = compare_trace_files(
                [source], output=output, route_policy=FakeRoutePlanner()
            )
            self.assertEqual(report.counters["map_events"], 5)
            self.assertEqual(report.counters["unique_map_states"], 2)
            self.assertEqual(report.counters["duplicates_dropped"], 3)
            self.assertEqual(report.counters["comparisons"], 2)
            self.assertEqual(report.counters["divergences"], 2)

            rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(row["kind"] == "comparison" for row in rows))
            self.assertTrue(all(row["divergence"]["disagrees"] for row in rows))
            self.assertEqual(rows[0]["route"]["score_decomposition"]["metrics"]["future_score"], 5.25)
            blob = output.read_text(encoding="utf-8")
            self.assertNotIn("private-run", blob)
            self.assertNotIn("terminal", blob)
            self.assertNotIn("win", blob)
            self.assertNotIn("result", blob)
            summary = json.loads(
                (output.with_suffix(output.suffix + ".summary.json")).read_text(encoding="utf-8")
            )
            self.assertFalse(summary["claims"]["uses_future_outcomes"])
            self.assertFalse(summary["claims"]["is_win_rate_evaluation"])

    def test_malformed_and_invalid_map_sources_are_retained_as_exclusions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "bad.jsonl"
            output = root / "comparison.jsonl"
            write_events(
                source,
                [
                    "{not-json",
                    17,
                    {"event_type": [], "raw": {"private": "ignored"}},
                    {"event_type": "state", "sequence": 3, "raw": {"state_type": "map"}},
                    {"event_type": "state", "sequence": 4, "raw": {"state_type": "monster"}},
                ],
            )
            report = compare_trace_files(
                [source], output=output, route_policy=FakeRoutePlanner()
            )
            self.assertEqual(report.exclusions["malformed_json"], 1)
            self.assertEqual(report.exclusions["event_not_object"], 1)
            self.assertEqual(report.exclusions["invalid_map_state"], 1)
            self.assertEqual(report.counters["ignored_other"], 1)
            self.assertEqual(report.counters["non_map_monster"], 1)
            rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["status"] for row in rows], ["excluded", "excluded", "excluded"])
            self.assertEqual(
                {row["error"]["reason"] for row in rows},
                {"malformed_json", "event_not_object", "invalid_map_state"},
            )

    def test_optional_identity_gates_and_output_cannot_replace_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "raw.jsonl"
            write_events(source, [{"event_type": "state", "raw": map_state()}])
            with self.assertRaises(RouteComparisonError):
                compare_trace_files([source], output=source, route_policy=FakeRoutePlanner())
            output = root / "gated.jsonl"
            report = compare_trace_files(
                [source],
                output=output,
                route_policy=FakeRoutePlanner(),
                expected_character="SILENT",
                expected_ascension=9,
            )
            self.assertEqual(report.exclusions["character_mismatch"], 1)
            self.assertEqual(report.counters["comparisons"], 0)

    def test_all_output_targets_are_new_and_input_bytes_stay_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "raw.jsonl"
            write_events(source, [{"event_type": "state", "raw": map_state()}])
            original = source.read_bytes()

            existing_output = root / "existing.jsonl"
            existing_output.write_text("keep\n", encoding="utf-8")
            with self.assertRaises(RouteComparisonError):
                compare_trace_files(
                    [source], output=existing_output, route_policy=FakeRoutePlanner()
                )
            self.assertEqual(existing_output.read_text(encoding="utf-8"), "keep\n")
            self.assertEqual(source.read_bytes(), original)

            output = root / "comparison.jsonl"
            sidecar = output.with_suffix(output.suffix + ".summary.json")
            sidecar.write_text("keep-summary\n", encoding="utf-8")
            with self.assertRaises(RouteComparisonError):
                compare_trace_files(
                    [source], output=output, route_policy=FakeRoutePlanner()
                )
            self.assertFalse(output.exists())
            self.assertEqual(sidecar.read_text(encoding="utf-8"), "keep-summary\n")
            self.assertEqual(source.read_bytes(), original)

            cli_output = root / "cli-comparison.jsonl"
            with self.assertRaises(RouteComparisonError):
                main(
                    [
                        str(source),
                        "--out",
                        str(cli_output),
                        "--summary",
                        str(source),
                    ]
                )
            self.assertFalse(cli_output.exists())
            self.assertFalse(
                cli_output.with_suffix(cli_output.suffix + ".summary.json").exists()
            )
            self.assertEqual(source.read_bytes(), original)

    def test_session_fallback_and_file_boundary_prevent_cross_file_deduplication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.jsonl"
            second = root / "second.jsonl"
            first_state = map_state()
            first_state["run"].pop("run_id")
            second_state = json.loads(json.dumps(first_state))
            write_events(
                first,
                [
                    {
                        "event_type": "session",
                        "session_id": "session-local",
                        "raw": {"run_id": "run-local"},
                    },
                    {"event_type": "state", "decision_id": "d1", "raw": first_state},
                ],
            )
            # There is no run/session identity in this file.  The same visible
            # state must still be a separate source boundary.
            write_events(
                second,
                [{"event_type": "state", "decision_id": "d1", "raw": second_state}],
            )
            output = root / "comparison.jsonl"
            report = compare_trace_files(
                [first, second], output=output, route_policy=FakeRoutePlanner()
            )
            self.assertEqual(report.counters["unique_map_states"], 2)
            self.assertEqual(report.counters["duplicates_dropped"], 0)
            rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertNotEqual(rows[0]["run_key"], rows[1]["run_key"])

    def test_same_decision_with_changed_state_is_an_explicit_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "conflict.jsonl"
            first = map_state(hp=64)
            second = map_state(hp=20)
            write_events(
                source,
                [
                    {"event_type": "state", "decision_id": "same", "raw": first},
                    {"event_type": "state", "decision_id": "same", "raw": second},
                ],
            )
            output = root / "comparison.jsonl"
            report = compare_trace_files(
                [source], output=output, route_policy=FakeRoutePlanner()
            )
            self.assertEqual(report.errors["decision_state_conflict"], 1)
            self.assertEqual(report.exclusions["decision_state_conflict"], 1)
            self.assertEqual(report.counters["decision_conflicts"], 1)
            self.assertEqual(report.counters["duplicates_dropped"], 0)
            rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["kind"] for row in rows], ["comparison", "excluded"])
            self.assertEqual(rows[1]["error"]["reason"], "decision_state_conflict")

    def test_outer_envelope_build_cannot_hide_nested_build_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "wrong-build.jsonl"
            nested = map_state()
            nested["build"] = LIVE_BUILD
            write_events(
                source,
                [
                    {
                        "event_type": "state",
                        "raw": {"build": "untrusted-old-build", "state": nested},
                    }
                ],
            )
            output = root / "comparison.jsonl"
            report = compare_trace_files(
                [source], output=output, route_policy=FakeRoutePlanner()
            )
            self.assertEqual(report.exclusions["invalid_map_state"], 1)
            self.assertEqual(report.counters["comparisons"], 0)
            rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[0]["error"]["reason"], "invalid_map_state")

    def test_summary_records_route_parameters_input_hash_and_git_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "raw.jsonl"
            state = map_state()
            state["map"]["nodes"] = []
            write_events(source, [{"event_type": "state", "raw": state}])
            expected_hash = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
            output = root / "comparison.jsonl"
            policy = RoutePlannerPolicy(
                max_nodes=7, max_depth=2, future_discount=0.5
            )
            compare_trace_files([source], output=output, route_policy=policy)
            summary = json.loads(
                output.with_suffix(output.suffix + ".summary.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                summary["route_parameters"],
                {"max_depth": 2, "max_nodes": 7, "future_discount": 0.5},
            )
            self.assertEqual(summary["input_sha256"][0]["sha256"], expected_hash)
            self.assertIn("git_commit", summary["git_provenance"])
            self.assertIn("git_dirty", summary["git_provenance"])


if __name__ == "__main__":
    unittest.main()
