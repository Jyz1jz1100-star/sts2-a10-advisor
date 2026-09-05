"""Regression tests for comparison-batch seed and lifecycle integrity."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from combat_solver.compare import BattleRecord
from scripts.run_solver_comparison import (
    BatchIntegrityError,
    RunIdentityAudit,
    SeedAudit,
    SeedContractError,
    _next_battle_sequence,
    _partition_seed_key,
    invalid_record_seeds,
    load_battle_rows,
    load_records_checkpoint,
    recover_records_from_journal,
    read_verified_compendium,
    resolve_gate_config,
    _serialize_record,
    validate_record_seeds,
    validate_seed_payload,
    write_records_checkpoint,
)


class SeedContractTests(unittest.TestCase):
    def test_allocation_count_is_seed_count_not_battle_count(self) -> None:
        partition, allowed = validate_seed_payload(
            {
                "partition": {"name": "p", "start": 1_600_000_000, "count": 2},
                "seeds": [1_600_000_000, 1_600_000_001],
                "expected_battles": 18,
            }
        )
        self.assertEqual(partition["count"], 2)
        self.assertEqual(allowed, {1_600_000_000, 1_600_000_001})

    def test_allocation_count_mismatch_is_rejected(self) -> None:
        with self.assertRaises(BatchIntegrityError):
            validate_seed_payload(
                {
                    "partition": {"name": "p", "start": 1, "count": 2},
                    "seeds": [1],
                }
            )

    def test_fixed_mode_fails_closed_on_missing_bridge_seed(self) -> None:
        audit = SeedAudit("fixed", {"name": "p"}, {7})
        with self.assertRaises(SeedContractError):
            audit.observe({"state_type": "monster", "run": {"floor": 1, "seed": None}})
        self.assertFalse(audit.fixed_seed_verified)
        self.assertEqual(audit.missing_observations, 1)

    def test_observational_mode_records_missing_seed_without_fixed_claim(self) -> None:
        audit = SeedAudit("observational", {"name": "p"}, {7})
        self.assertIsNone(
            audit.observe({"state_type": "monster", "run": {"floor": 1, "seed": None}})
        )
        self.assertFalse(audit.fixed_seed_verified)
        self.assertEqual(audit.to_json()["missing_observations"], 1)

    def test_fixed_mode_accepts_only_registered_integer_seed(self) -> None:
        audit = SeedAudit("fixed", {"name": "p"}, {7})
        self.assertEqual(
            audit.observe({"state_type": "monster", "run": {"floor": 1, "seed": 7}}),
            7,
        )
        self.assertTrue(audit.fixed_seed_verified)
        with self.assertRaises(SeedContractError):
            audit.observe({"state_type": "monster", "run": {"floor": 1, "seed": 8}})

    def test_fixed_mode_accepts_canonical_decimal_seed_string(self) -> None:
        audit = SeedAudit("fixed", {"name": "p"}, {7})
        self.assertEqual(
            audit.observe({"state_type": "monster", "run": {"floor": 1, "seed": "007"}}),
            7,
        )
        self.assertTrue(audit.fixed_seed_verified)

    def test_alphanumeric_save_seed_is_not_coerced_to_numeric(self) -> None:
        self.assertIsNone(_partition_seed_key("2450ZAR9EF"))
        audit = SeedAudit("fixed", {"name": "p"}, {7})
        with self.assertRaises(SeedContractError):
            audit.observe(
                {"state_type": "monster", "run": {"floor": 1, "seed": "2450ZAR9EF"}}
            )
        self.assertFalse(audit.fixed_seed_verified)

    def test_restored_out_of_partition_seed_cannot_inherit_verified_audit(self) -> None:
        audit = SeedAudit("fixed", {"name": "p"}, {7})
        audit.observe({"state_type": "monster", "run": {"floor": 1, "seed": 7}})
        poisoned = BattleRecord(
            battle_id="csb-poisoned",
            seed=8,
            act=1,
            floor=1,
            enemies=(),
            hp_start=80,
            hp_end=80,
            outcome="unknown",
        )
        self.assertTrue(audit.fixed_seed_verified)
        self.assertEqual(
            invalid_record_seeds([poisoned], {7}),
            [{"battle_id": "csb-poisoned", "seed": 8}],
        )
        with self.assertRaises(SeedContractError):
            validate_record_seeds([poisoned], {7})


class RunIdentityAuditTests(unittest.TestCase):
    def _state(self, **overrides):
        run = {
            "act": 1,
            "floor": 1,
            "ascension": 10,
            "run_id": "local:profile1:run-1",
            "seed": "2450ZAR9EF",
        }
        run.update(overrides.pop("run", {}))
        state = {
            "state_type": "monster",
            "run": run,
            "player": {"character_id": "CHARACTER.IRONCLAD", "character": "铁甲战士"},
        }
        state.update(overrides)
        return state

    def _compendium(self, **overrides):
        current = {
            "is_in_progress": True,
            "game_mode": "standard",
            "ascension": 10,
            "run_id": "local:profile1:run-1",
            "seed": "2450ZAR9EF",
        }
        current.update(overrides)
        return {"current_run": current}

    def test_active_state_is_recorded_and_round_trips(self) -> None:
        audit = RunIdentityAudit()
        identity = audit.observe(self._state(), self._compendium())
        self.assertEqual(identity["character"], "IRONCLAD")
        self.assertTrue(audit.verified)
        payload = audit.to_json()
        self.assertTrue(payload["verified"])
        self.assertEqual(payload["observations"], 1)
        self.assertEqual(payload["required"]["game_mode"], "standard")
        restored = RunIdentityAudit.from_json(payload)
        self.assertTrue(restored.verified)
        self.assertEqual(restored.to_json(), payload)

    def test_unknown_compendium_identity_fails_closed(self) -> None:
        audit = RunIdentityAudit()
        with self.assertRaises(BatchIntegrityError):
            audit.observe(self._state(), self._compendium(game_mode=None))
        self.assertFalse(audit.verified)
        self.assertIn("game_mode", audit.to_json()["missing_fields"])

    def test_wrong_character_ascension_or_multiplayer_fails_closed(self) -> None:
        for state, compendium in (
            (self._state(player={"character_id": "CHARACTER.SILENT"}), self._compendium()),
            (self._state(run={"ascension": 9}), self._compendium()),
            (self._state(players=[{}, {}]), self._compendium()),
            (self._state(), self._compendium(is_multiplayer=True)),
        ):
            with self.subTest(state=state, compendium=compendium):
                with self.assertRaises(BatchIntegrityError):
                    RunIdentityAudit().observe(state, compendium)

    def test_non_active_state_does_not_claim_identity(self) -> None:
        audit = RunIdentityAudit()
        self.assertIsNone(
            audit.observe({"state_type": "menu", "run": {}}, self._compendium())
        )
        self.assertFalse(audit.verified)


class CompendiumReadTests(unittest.TestCase):
    class Clock:
        def __init__(self) -> None:
            self.now = 0.0

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.now += seconds

    @staticmethod
    def _complete() -> dict:
        return {
            "current_run": {
                "is_in_progress": True,
                "game_mode": "standard",
                "ascension": 10,
                "run_id": "run-1",
                "seed": "2450ZAR9EF",
            }
        }

    def test_partial_save_identity_is_retried_before_being_cached(self) -> None:
        clock = self.Clock()
        responses = [
            {
                "current_run": {
                    "is_in_progress": True,
                    "save_scope": "profile1",
                    "limitation": "current_run.save not visible yet",
                }
            },
            self._complete(),
        ]

        def reader():
            return responses.pop(0)

        result = read_verified_compendium(
            reader,
            timeout_seconds=2,
            retry_seconds=0.5,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        self.assertEqual(result, self._complete())
        self.assertEqual(clock.now, 0.5)

    def test_persistently_partial_save_fails_after_bounded_timeout(self) -> None:
        clock = self.Clock()
        partial = {
            "current_run": {
                "is_in_progress": True,
                "save_scope": "profile1",
                "limitation": "current_run.save not visible yet",
            }
        }
        with self.assertRaises(BatchIntegrityError):
            read_verified_compendium(
                lambda: partial,
                timeout_seconds=0.6,
                retry_seconds=0.25,
                monotonic=clock.monotonic,
                sleep=clock.sleep,
            )
        self.assertGreaterEqual(clock.now, 0.6)


class BatchJournalTests(unittest.TestCase):
    def test_existing_duplicate_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "battles.jsonl"
            row = {"battle_id": "csb-0001-f1"}
            path.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n", encoding="utf-8")
            with self.assertRaises(BatchIntegrityError):
                load_battle_rows(path)

    def test_existing_duplicate_sample_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "battles.jsonl"
            rows = [
                {"battle_id": "csb-0001-f1", "sample_key": "state-hash:x"},
                {"battle_id": "csb-0002-f2", "sample_key": "state-hash:x"},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            with self.assertRaises(BatchIntegrityError):
                load_battle_rows(path)

    def test_resume_sequence_starts_after_highest_committed_id(self) -> None:
        self.assertEqual(
            _next_battle_sequence(
                {"csb-0001-f1", "csb-0007-f3", "other-0099"}, "csb"
            ),
            7,
        )

    def test_full_record_checkpoint_round_trips(self) -> None:
        record = BattleRecord(
            battle_id="csb-0001-f1",
            seed=7,
            act=1,
            floor=1,
            enemies=("CULTIST_0",),
            hp_start=80,
            hp_end=75,
            outcome="win",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "records_checkpoint.json"
            write_records_checkpoint(path, [record])
            restored = load_records_checkpoint(path)
        self.assertEqual(restored[0].battle_id, record.battle_id)
        self.assertEqual(restored[0].seed, 7)
        self.assertEqual(restored[0].actual_hp_loss, 5)

    def test_crash_window_recovers_journal_row_ahead_of_checkpoint(self) -> None:
        # Simulate the process dying after battles.jsonl was fsynced but before
        # records_checkpoint.json was replaced.
        record = BattleRecord(
            battle_id="csb-0002-f1",
            seed=8,
            act=1,
            floor=1,
            enemies=("CULTIST_0",),
            hp_start=80,
            hp_end=70,
            outcome="win",
        )
        row = record.to_json()
        row["record_checkpoint"] = _serialize_record(record)
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "battles.jsonl"
            journal.write_text(json.dumps(row) + "\n", encoding="utf-8")
            rows, ids, _keys, _unkeyed = load_battle_rows(journal)
        recovered, legacy = recover_records_from_journal(rows, [])
        self.assertFalse(legacy)
        self.assertEqual(ids, {"csb-0002-f1"})
        self.assertEqual([r.battle_id for r in recovered], [record.battle_id])
        self.assertEqual(recovered[0].actual_hp_loss, 10)

    def test_checkpoint_seed_poisoning_is_blocked_after_recovery(self) -> None:
        audit = SeedAudit("fixed", {"name": "p"}, {7})
        audit.observe({"state_type": "monster", "run": {"floor": 1, "seed": 7}})
        poisoned = BattleRecord(
            battle_id="csb-poisoned",
            seed=8,
            act=1,
            floor=1,
            enemies=(),
            hp_start=80,
            hp_end=80,
            outcome="unknown",
        )
        row = poisoned.to_json()
        row["record_checkpoint"] = _serialize_record(poisoned)
        recovered, _legacy = recover_records_from_journal([row], [])
        self.assertTrue(audit.fixed_seed_verified)
        with self.assertRaises(SeedContractError):
            validate_record_seeds(recovered, {7})


class GateConfigurationTests(unittest.TestCase):
    def test_automated_gates_are_applied_only_to_automated_batches(self) -> None:
        config = {
            "gates": {"failure_rate_upper95": 0.1, "nullable_gate": None},
            "automated_gates": {
                "automated_inferred_turns": 0.0,
                "automated_ambiguous_turns": 0.0,
                "ignored_none": None,
            },
        }
        automated, automated_provenance = resolve_gate_config(config, True)
        manual, manual_provenance = resolve_gate_config(config, False)

        self.assertEqual(
            automated,
            {
                "failure_rate_upper95": 0.1,
                "automated_inferred_turns": 0.0,
                "automated_ambiguous_turns": 0.0,
            },
        )
        self.assertEqual(manual, {"failure_rate_upper95": 0.1})
        self.assertTrue(automated_provenance["automated_section_applied"])
        self.assertEqual(automated_provenance["ignored_automated_gate_config"], {})
        self.assertFalse(manual_provenance["automated_section_applied"])
        self.assertEqual(
            manual_provenance["ignored_automated_gate_config"],
            {"automated_inferred_turns": 0.0, "automated_ambiguous_turns": 0.0},
        )
        self.assertEqual(
            automated_provenance["applied_gate_sources"]["automated_inferred_turns"],
            "automated_gates",
        )


if __name__ == "__main__":
    unittest.main()
