"""Guards for per-batch mod attestation (combat_solver/modpin.py + supervisor)."""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from combat_solver.modpin import attest_lock_mods  # noqa: E402
from scripts.supervise_solver_batch import _moved_mods  # noqa: E402


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


class AttestationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.dll = self.root / "Solver.dll"
        self.dll.write_bytes(b"one")
        (self.root / "Solver.json").write_text(
            json.dumps({"id": "Solver", "version": "0.41.0"}), encoding="utf-8"
        )
        self.lock = self.root / "lock.json"
        self._write_lock(_sha(self.dll))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_lock(self, expected: str) -> None:
        self.lock.write_text(
            json.dumps(
                {"evaluation_environment": {"mod_dll_inventory": [
                    {"mod_id": "Solver", "path": str(self.dll), "sha256": expected}
                ]}}
            ),
            encoding="utf-8",
        )

    def test_matching_bytes_record_the_manifest_version(self) -> None:
        attestation = attest_lock_mods(self.lock)
        self.assertTrue(attestation.all_match_lock)
        self.assertEqual(attestation.records[0].version, "0.41.0")
        self.assertEqual(attestation.to_json()["drifted_from_lock"], [])

    def test_replaced_binary_is_drift_not_silence(self) -> None:
        self.dll.write_bytes(b"two")  # Workshop updated the mod under the lock
        attestation = attest_lock_mods(self.lock)
        self.assertFalse(attestation.all_match_lock)
        self.assertEqual(attestation.drifted, ["Solver"])

    def test_missing_file_fails_closed_as_an_error_record(self) -> None:
        self.dll.unlink()
        attestation = attest_lock_mods(self.lock)
        self.assertFalse(attestation.all_match_lock)
        self.assertEqual(attestation.unreadable, ["Solver"])
        self.assertEqual(attestation.records[0].error, "missing_file")

    def test_empty_inventory_is_not_attested(self) -> None:
        self.lock.write_text(json.dumps({"evaluation_environment": {}}), encoding="utf-8")
        self.assertFalse(attest_lock_mods(self.lock).all_match_lock)


class MovedDuringBatchTests(unittest.TestCase):
    def test_only_changed_or_vanishing_ids_are_reported(self) -> None:
        start = {"records": [
            {"mod_id": "A", "actual_sha256": "11"},
            {"mod_id": "B", "actual_sha256": "22"},
            {"mod_id": "C", "actual_sha256": "33"},
        ]}
        end = {"records": [
            {"mod_id": "A", "actual_sha256": "11"},
            {"mod_id": "B", "actual_sha256": "99"},
            {"mod_id": "D", "actual_sha256": "44"},
        ]}
        self.assertEqual(_moved_mods(start, end), ["B", "C", "D"])

    def test_unavailable_measurement_yields_no_verdict_not_a_clean_one(self) -> None:
        good = {"records": [{"mod_id": "A", "actual_sha256": "11"}]}
        for other in (None, {"error": "attestation failed"}, {}):
            self.assertEqual(_moved_mods(good, other), [], f"end={other!r}")
            self.assertEqual(_moved_mods(other, good), [], f"start={other!r}")


if __name__ == "__main__":
    unittest.main()
