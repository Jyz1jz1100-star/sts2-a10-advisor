"""The mod gate has to tell "the operator auto-updated it" from "it isn't there".

`verify_solver_inventory` used to answer both with one abort, which contradicted
README's promise that CombatSolver drift is reported rather than vetoed -- and
the comparison track legitimately needs the pin, because there the solver *is*
the variable. These pin the split: a drifted mod may block a comparison and must
only be recorded on the acceptance path, while a missing or unreadable mod blocks
both, because a batch that cannot name its executor has no provenance at all.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from bridge.trace_controller import VersionLock, VersionLockError
from scripts.run_solver_comparison import verify_solver_inventory


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


class InventoryGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.solver = root / "CombatSolver.dll"
        self.solver.write_bytes(b"solver-bytes-v2")
        self.bridge = root / "STS2_MCP.dll"
        self.bridge.write_bytes(b"bridge-bytes")
        self.lock_path = root / "combat_solver.lock.json"
        self.lock_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "game": {},
                    "bridge": {"version": "0.4.0", "base_url": "http://127.0.0.1:15526"},
                    "evaluation_environment": {
                        "allowed_mod_ids": [],
                        "live_mod_roots": [],
                        "mod_dll_inventory": [
                            {"mod_id": "CombatSolver", "required": True,
                             "path": str(self.solver),
                             "sha256": "0" * 64},  # deliberately not what is on disk
                            {"mod_id": "STS2_MCP", "required": True,
                             "path": str(self.bridge),
                             "sha256": _digest(self.bridge)},
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )
        self.lock = VersionLock.load(self.lock_path)
        self.addCleanup(self.tmp.cleanup)

    def test_strict_still_refuses_a_comparison_run_that_drifted(self) -> None:
        with self.assertRaises(VersionLockError) as caught:
            verify_solver_inventory(self.lock, mod_gate="strict")
        message = str(caught.exception)
        self.assertIn("CombatSolver", message)
        # The old text blamed an unfilled lock; the real cause is a moved byte.
        self.assertIn("differ from the pin", message)
        self.assertNotIn("at installation time", message)

    def test_attest_records_the_drift_and_continues(self) -> None:
        result = verify_solver_inventory(self.lock, mod_gate="attest")
        self.assertEqual(result["mod_gate"], "attest")
        self.assertEqual(result["drifted_from_lock"], ["CombatSolver"])
        by_id = {row["mod_id"]: row for row in result["inventory"]}
        self.assertTrue(by_id["CombatSolver"]["ok"], "drift must not stop acceptance")
        self.assertFalse(by_id["CombatSolver"]["matches_lock"])
        self.assertTrue(by_id["STS2_MCP"]["matches_lock"])
        self.assertEqual(by_id["CombatSolver"]["sha256_observed"], _digest(self.solver))

    def test_a_missing_mod_refuses_both_tracks(self) -> None:
        gone = Path(self.tmp.name) / "absent.dll"
        data = json.loads(self.lock_path.read_text(encoding="utf-8"))
        data["evaluation_environment"]["mod_dll_inventory"].append(
            {"mod_id": "RegentFX", "required": True, "path": str(gone), "sha256": "1" * 64}
        )
        self.lock_path.write_text(json.dumps(data), encoding="utf-8")
        lock = VersionLock.load(self.lock_path)
        for gate in ("strict", "attest"):
            with self.subTest(mod_gate=gate):
                with self.assertRaises(VersionLockError) as caught:
                    verify_solver_inventory(lock, mod_gate=gate)
                self.assertIn("missing or unreadable", str(caught.exception))
                self.assertIn("RegentFX", str(caught.exception))

    def test_attest_drift_becomes_a_blocker_not_a_pass(self) -> None:
        """The verdict must keep saying so; drift may not read as acceptance-grade."""
        result = verify_solver_inventory(self.lock, mod_gate="attest")
        self.assertTrue(result["drifted_from_lock"])


class SupervisorTrackTests(unittest.TestCase):
    def _config(self, track: str):
        from scripts.supervise_solver_batch import SupervisorConfig
        root = Path(tempfile.mkdtemp())
        return SupervisorConfig(
            batch_id="ssb-20260921T010101Z-abc12345",
            mode="observational",
            output_root=root,
            game_log_dir=root / "game-logs",
            max_battles=2,
            max_runs=2,
            max_actions=4,
            allow_actions=True,
            track=track,
        )

    def test_track_selects_the_child_gate(self) -> None:
        from scripts.supervise_solver_batch import build_component_commands
        for track, gate in (("acceptance", "attest"), ("comparison", "strict")):
            with self.subTest(track=track):
                config = self._config(track)
                commands = build_component_commands(config)
                comparison = commands["comparison"]
                self.assertEqual(comparison[comparison.index("--mod-gate") + 1], gate)

    def test_an_unknown_track_is_refused_before_anything_starts(self) -> None:
        from scripts.supervise_solver_batch import SupervisorConfigurationError
        with self.assertRaises(SupervisorConfigurationError):
            self._config("whatever")


if __name__ == "__main__":
    unittest.main()
