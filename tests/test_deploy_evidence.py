"""Fail-closed durable deploy evidence adapter tests."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from combat_solver.evidence import EvidenceIntegrityError, adapt_comparison_evidence
from scripts.assess_full_run import TraceInput, main as assess_main, merge_comparison_evidence
from scripts.supervise_solver_batch import assessor_artifact_paths


def _record(
    *,
    battle_id: str = "b-1",
    run_id: str | None = "run-1",
    seed: int | None = 17,
    source: str = "deploy_log",
    decision_id: str | None = "decision-1",
    turn: int = 1,
    actions: list[dict] | None = None,
) -> dict:
    return {
        "battle_id": battle_id,
        "run_id": run_id,
        "seed": seed,
        "act": 1,
        "floor": 2,
        "hp_start": 80,
        "hp_end": 72,
        "outcome": "win",
        "turns": [
            {
                "turn": turn,
                "decision_id": decision_id,
                "executed": {
                    "turn": turn,
                    "actions": actions or [{"kind": "play", "card_id": "STRIKE"}],
                    "ambiguous": False,
                    "source": source,
                },
            }
        ],
    }


def _write_journal(directory: Path, rows: list[dict]) -> None:
    (directory / "battles.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


class DeployEvidenceTests(unittest.TestCase):
    def test_deploy_source_is_bound_and_hashes_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = _record()
            row = {"battle_id": "b-1", "run_id": "run-1", "seed": 17, "record_checkpoint": record}
            _write_journal(root, [row])
            result = adapt_comparison_evidence(root)

        self.assertTrue(result["valid"])
        self.assertEqual(result["counts"]["deploy_log_turns"], 1)
        event = result["events"][0]
        self.assertEqual(event["raw"]["executed"]["source"], "deploy_log")
        self.assertEqual(event["raw"]["run_id"], "run-1")
        self.assertEqual(event["raw"]["seed"], 17)
        self.assertTrue(result["source"]["source_files"]["battles_jsonl"]["hash_verified"])
        self.assertEqual(
            result["source"]["source_hashes"]["battles_jsonl"],
            result["source"]["source_files"]["battles_jsonl"]["sha256"],
        )

    def test_inferred_turn_is_never_promoted_and_exact_duplicates_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deployed = _record()
            inferred = _record(battle_id="b-2", decision_id="decision-2", source="inferred")
            _write_journal(root, [{"battle_id": "b-1", "record_checkpoint": deployed}] * 2 + [
                {"battle_id": "b-2", "record_checkpoint": inferred}
            ])
            result = adapt_comparison_evidence(root)

        self.assertEqual(result["counts"]["journal_duplicates_deduped"], 1)
        self.assertEqual(result["counts"]["inferred_turns"], 1)
        self.assertEqual(result["counts"]["deploy_log_turns"], 1)
        self.assertEqual(len(result["events"]), 1)

    def test_missing_binding_and_conflicting_duplicate_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = _record(run_id=None)
            _write_journal(root, [{"battle_id": "b-1", "record_checkpoint": missing}])
            with self.assertRaises(EvidenceIntegrityError):
                adapt_comparison_evidence(root)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _record(battle_id="b-1")
            second = _record(battle_id="b-2", actions=[{"kind": "play", "card_id": "BASH"}])
            _write_journal(
                root,
                [
                    {"battle_id": "b-1", "record_checkpoint": first},
                    {"battle_id": "b-2", "record_checkpoint": second},
                ],
            )
            with self.assertRaises(EvidenceIntegrityError):
                adapt_comparison_evidence(root)

    def test_conflicting_explicit_decision_bindings_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = _record()
            record["turns"][0]["executed"]["decision_id"] = "OTHER-DECISION"
            _write_journal(root, [{"battle_id": "b-1", "record_checkpoint": record}])

            with self.assertRaisesRegex(
                EvidenceIntegrityError, "conflicting explicit decision_id bindings"
            ):
                adapt_comparison_evidence(root)

    def test_stale_snapshot_hash_does_not_override_explicit_decision_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = _record()
            record["turns"][0]["state_hash"] = "stale-route-anchor"
            record["turns"][0]["snapshot"] = {"state_hash": "reused-route-snapshot"}
            _write_journal(root, [{"battle_id": "b-1", "record_checkpoint": record}])
            result = adapt_comparison_evidence(root)

        self.assertTrue(result["valid"])
        self.assertEqual(result["events"][0]["decision_id"], "decision-1")

    def test_invalid_executed_turn_values_fail_closed(self) -> None:
        for invalid_turn in (True, 0, -1, "1"):
            with self.subTest(invalid_turn=invalid_turn), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                record = _record()
                record["turns"][0]["executed"]["turn"] = invalid_turn
                _write_journal(root, [{"battle_id": "b-1", "record_checkpoint": record}])

                with self.assertRaisesRegex(EvidenceIntegrityError, "invalid executed turn"):
                    adapt_comparison_evidence(root)

    def test_assessor_merge_dedupes_same_event_and_manifest_lists_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = _record()
            _write_journal(root, [{"battle_id": "b-1", "record_checkpoint": record}])
            trace = TraceInput(path=root / "trace.jsonl")
            trace.events.append(
                {
                    "event_type": "run_identity",
                    "raw": {"run_id": "run-1", "seed": 17},
                }
            )
            evidence = merge_comparison_evidence(trace, root)
            merge_comparison_evidence(trace, evidence)
            paths = assessor_artifact_paths(root / "supervisor", root / "comparison")

        self.assertEqual(len(trace.events), 2)
        self.assertEqual(trace.events[1]["raw"]["executed"]["source"], "deploy_log")
        self.assertIn("record_checkpoint", paths)
        self.assertEqual(paths["record_checkpoint"], paths["records_checkpoint"])
        self.assertTrue(paths["trace"].endswith("autoplay_trace.jsonl"))

    def test_assessor_rejects_unmatched_run_seed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = _record()
            _write_journal(root, [{"battle_id": "b-1", "record_checkpoint": record}])
            trace = TraceInput(path=root / "trace.jsonl")
            with self.assertRaises(EvidenceIntegrityError):
                merge_comparison_evidence(trace, root)

    def test_checkpoint_is_parsed_from_the_same_bytes_that_are_hashed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = _record()
            row = {"battle_id": "b-1", "run_id": "run-1", "seed": 17}
            _write_journal(root, [row])
            checkpoint = root / "records_checkpoint.json"
            initial = {"schema_version": 1, "records": [record]}
            checkpoint.write_text(json.dumps(initial), encoding="utf-8")
            original_read_bytes = Path.read_bytes
            reads = iter((root / "battles.jsonl", checkpoint, root / "manifest.json"))

            def read_and_mutate() -> bytes:
                path = next(reads)
                payload = original_read_bytes(path)
                if path == checkpoint:
                    # Simulate a writer replacing the checkpoint immediately
                    # after the adapter obtains its byte snapshot.
                    path.write_text(json.dumps({"schema_version": 1, "records": []}), encoding="utf-8")
                return payload

            with patch("combat_solver.evidence.Path.read_bytes", side_effect=read_and_mutate):
                result = adapt_comparison_evidence(root)

        self.assertEqual(result["counts"]["deploy_log_turns"], 1)
        self.assertEqual(
            result["source"]["source_hashes"]["records_checkpoint"],
            result["source"]["source_files"]["records_checkpoint"]["sha256"],
        )

    def test_cli_maps_comparison_batch_to_one_matching_trace(self) -> None:
        def trace_payload(run_id: str, seed: int) -> list[dict]:
            return [
                {"event_type": "session", "raw": {"model_id": "trained-test"}},
                {"event_type": "run_identity", "raw": {"run_id": run_id, "seed": seed}},
            ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trace_a = root / "trace-a.jsonl"
            trace_b = root / "trace-b.jsonl"
            trace_a.write_text(
                "".join(json.dumps(event) + "\n" for event in trace_payload("run-1", 17)),
                encoding="utf-8",
            )
            trace_b.write_text(
                "".join(json.dumps(event) + "\n" for event in trace_payload("run-2", 18)),
                encoding="utf-8",
            )
            comparison = root / "comparison"
            comparison.mkdir()
            _write_journal(
                comparison,
                [{"battle_id": "b-1", "record_checkpoint": _record()}],
            )
            output = root / "report.json"
            result_code = assess_main(
                [
                    "--trace",
                    str(trace_a),
                    str(trace_b),
                    "--comparison-dir",
                    str(comparison),
                    "--out",
                    str(output),
                    "--cohort",
                    "assisted",
                    "--seed-mode",
                    "observational",
                ]
            )
            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result_code, 1)  # incomplete fixture, but mapping succeeded
        self.assertEqual(len(report["execution_evidence"]), 1)
        self.assertEqual(
            report["execution_evidence"][0]["source_files"]["battles_jsonl"]["path"],
            str(comparison / "battles.jsonl"),
        )


if __name__ == "__main__":
    unittest.main()
