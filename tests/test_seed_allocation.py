"""Regression tests for batch-scoped fixed-seed allocation state."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bridge.seed_allocation import (
    SeedAllocationError,
    SeedAllocationExhausted,
    SeedLedger,
    load_seed_allocation,
)


class SeedLedgerTests(unittest.TestCase):
    @staticmethod
    def _allocation(root: Path):
        path = root / "seeds.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "allocation_kind": "run_seed",
                    "partition": {"name": "test", "start": 1600000000, "count": 2},
                    "seeds": [1600000000, 1600000001],
                }
            ),
            encoding="utf-8",
        )
        return load_seed_allocation(path)

    @staticmethod
    def _identity(seed: str, run_id: str) -> dict[str, str]:
        return {"seed": seed, "run_id": run_id}

    def test_implicit_allocation_sidecar_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            allocation = self._allocation(Path(temp))
            with self.assertRaisesRegex(SeedAllocationError, "batch-local"):
                SeedLedger(allocation)

    def test_batch_dir_default_is_local_and_batches_do_not_share_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            allocation = self._allocation(root)
            first = SeedLedger(allocation, batch_dir=root / "batch-a")
            second = SeedLedger(allocation, batch_dir=root / "batch-b")
            self.assertEqual(first.path, (root / "batch-a" / "seed_allocation.ledger.json").resolve())
            self.assertEqual(second.path, (root / "batch-b" / "seed_allocation.ledger.json").resolve())
            self.assertNotEqual(first.path, second.path)
            entry = first.reserve_next()
            first.finalize_started(entry, self._identity("1600000000", "run-a"))
            self.assertEqual(first.snapshot()["next_index"], 1)
            self.assertEqual(second.snapshot()["next_index"], 0)
            self.assertEqual(second.reserve_next().raw_seed, "1600000000")

    def test_batch_dir_rejects_nonlocal_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            allocation = self._allocation(root)
            with self.assertRaisesRegex(SeedAllocationError, "batch-local"):
                SeedLedger(allocation, root / "other.json", batch_dir=root / "batch")

    def test_active_reservation_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            allocation = self._allocation(root)
            ledger = SeedLedger(allocation, batch_dir=root / "batch")
            entry = ledger.reserve_next()
            with self.assertRaises(SeedAllocationError):
                ledger.observe_current_run(self._identity("1600000001", "wrong-run"))
            state = ledger.snapshot()
            self.assertEqual(state["next_index"], 0)
            self.assertIsNotNone(state["active"])
            self.assertEqual(state["active"]["index"], entry.index)

    def test_adopt_current_run_consumes_matching_next_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = SeedLedger(self._allocation(Path(temp)), batch_dir=Path(temp))
            entry = ledger.adopt_current_run(
                self._identity("1600000000", "orphan-run")
            )
            self.assertEqual(entry.raw_seed, "1600000000")
            snapshot = ledger.snapshot()
            self.assertIsNone(snapshot["active"])
            self.assertEqual(snapshot["next_index"], 1)
            self.assertEqual(snapshot["consumed"][0]["run_id"], "orphan-run")
            self.assertTrue(snapshot["consumed"][0]["adopted"])
            # the adopted entry is consumed: the next seed is 1600000001
            self.assertEqual(ledger.reserve_next().raw_seed, "1600000001")

    def test_adopt_current_run_refuses_foreign_seed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = SeedLedger(self._allocation(Path(temp)), batch_dir=Path(temp))
            with self.assertRaisesRegex(SeedAllocationError, "next allocation entry"):
                ledger.adopt_current_run(self._identity("1600000001", "run-b"))
            self.assertEqual(ledger.snapshot()["next_index"], 0)

    def test_adopt_current_run_refuses_already_consumed_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = SeedLedger(self._allocation(Path(temp)), batch_dir=Path(temp))
            entry = ledger.reserve_next()
            ledger.finalize_started(entry, self._identity("1600000000", "run-a"))
            # defensive: a run_id that is already in the ledger must never be
            # adopted again, even with a seed that matches the next entry
            with self.assertRaisesRegex(SeedAllocationError, "already recorded"):
                ledger.adopt_current_run(self._identity("1600000001", "run-a"))

    def test_adopt_current_run_refuses_when_reservation_is_open(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = SeedLedger(self._allocation(Path(temp)), batch_dir=Path(temp))
            ledger.reserve_next()
            with self.assertRaisesRegex(SeedAllocationError, "unresolved active"):
                ledger.adopt_current_run(self._identity("1600000000", "run-a"))

    def test_abort_active_reservation_rolls_back_for_reflight(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = SeedLedger(self._allocation(Path(temp)), batch_dir=Path(temp))
            entry = ledger.reserve_next()
            self.assertEqual(
                ledger.abort_active_reservation("run lost before save"),
                entry,
            )
            snapshot = ledger.snapshot()
            self.assertIsNone(snapshot["active"])
            self.assertEqual(snapshot["next_index"], 0)
            # the same seed is reserved again (never played a decision)
            self.assertEqual(ledger.reserve_next().raw_seed, "1600000000")

    def test_abort_requires_reason_and_noop_without_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = SeedLedger(self._allocation(Path(temp)), batch_dir=Path(temp))
            with self.assertRaisesRegex(SeedAllocationError, "reason"):
                ledger.abort_active_reservation("  ")
            self.assertIsNone(ledger.abort_active_reservation("nothing active"))

    def test_ordered_consumption_stops_at_exhaustion(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ledger = SeedLedger(self._allocation(root), batch_dir=root / "batch")
            for index in range(2):
                entry = ledger.reserve_next()
                self.assertEqual(entry.index, index)
                ledger.finalize_started(
                    entry, self._identity(entry.raw_seed, f"run-{index}")
                )
            with self.assertRaises(SeedAllocationExhausted):
                ledger.reserve_next()


class SeedMetadataTests(unittest.TestCase):
    def test_checked_in_allocation_distinguishes_candidate_and_installed_support(self) -> None:
        path = Path(__file__).parents[1] / "data" / "combat_solver" / "fixed_battle_seeds.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        injection = payload["seed_injection"]
        self.assertTrue(injection["installed_bridge_supported"])
        self.assertTrue(injection["candidate_bridge_supported"])
        self.assertNotIn("supported_by_bridge", injection)
        # The rule that made a fixed run valid, kept in the file itself so a reader cannot inherit
        # the flag without the condition.
        self.assertIn("authoritative", injection["note"])

    def test_installed_support_is_only_claimed_with_its_real_embark_attached(self) -> None:
        """A flipped capability flag names the batch and the artifact that proved it.

        The flag used to read false because nothing had ever requested a seed and read it back on the
        installed bridge; on 2026-10-07 a fixed-mode embark did exactly that. Claiming it without the
        three-way record would be the same kind of unverified statement -- so the fields are required
        here, and the evidence file has to exist. Regenerating the allocation with
        ``scripts/make_solver_comparison_seeds.py`` resets the flag to false on purpose, and this test
        goes red rather than quietly accepting a verdict nobody re-measured.
        """
        root = Path(__file__).parents[1]
        payload = json.loads((root / "data" / "combat_solver" / "fixed_battle_seeds.json")
                             .read_text(encoding="utf-8"))
        injection = payload["seed_injection"]
        self.assertTrue(injection["verified_on_batch"].startswith("ssb-"))
        self.assertTrue((root / injection["evidence"]).exists(), injection["evidence"])
        evidence = json.loads((root / injection["evidence"]).read_text(encoding="utf-8"))
        self.assertEqual(evidence["requested_seed"], evidence["authoritative_read_back"]["seed"])
        self.assertTrue(all(evidence["criteria"].values()), evidence["criteria"])
        self.assertEqual(evidence["seed_mode"], "fixed")


if __name__ == "__main__":
    unittest.main()
