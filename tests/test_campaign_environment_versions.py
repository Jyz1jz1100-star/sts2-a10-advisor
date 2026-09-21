"""A frozen environment keeps its meaning, and a fidelity gate cannot be passed under it.

The campaign environment is stamped `sts2sim-campaign-approx-v1`.  Two things have
to stay true afterwards: nobody may edit what that version *contains* while keeping
the name (history would become unreadable), and nobody may mark a fidelity gate
passed while the artifacts still carry the approximate version (a checkpoint or a
win rate trained on Underdocks re-skins would read as a content-verified result).
"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from training.campaign_content import (
    CAMPAIGN_CONTENT_COVERAGE,
    CAMPAIGN_ENVIRONMENT_VERSION,
    FROZEN_ENVIRONMENT_VERSIONS,
    EnvironmentVersionError,
    assert_content_declaration,
    assert_single_environment,
)
from scripts.verify_campaign_fidelity_gates import measure_gates, emulator_encounter_ids

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "verify_campaign_fidelity_gates.py"


class FrozenEnvironmentTests(unittest.TestCase):
    def test_the_frozen_digest_is_the_declaration_on_disk(self) -> None:
        """The pinned digest must recompute, or the freeze is a claim about nothing."""
        digest = assert_content_declaration(
            CAMPAIGN_ENVIRONMENT_VERSION, CAMPAIGN_CONTENT_COVERAGE
        )
        self.assertEqual(
            digest,
            FROZEN_ENVIRONMENT_VERSIONS[CAMPAIGN_ENVIRONMENT_VERSION]["content_sha256"],
        )

    def test_editing_frozen_content_under_the_same_name_raises(self) -> None:
        drifted = copy.deepcopy(CAMPAIGN_CONTENT_COVERAGE)
        drifted["stages"]["3"]["pools"] = "Glory weak/normal/elite/boss"  # type: ignore[index]
        with self.assertRaises(EnvironmentVersionError) as caught:
            assert_content_declaration(CAMPAIGN_ENVIRONMENT_VERSION, drifted)
        self.assertIn("publish a new environment_version", str(caught.exception))

    def test_changing_the_verdict_under_a_frozen_name_raises(self) -> None:
        drifted = copy.deepcopy(CAMPAIGN_CONTENT_COVERAGE)
        drifted["verdict"] = "content_verified"  # type: ignore[assignment]
        with self.assertRaises(EnvironmentVersionError) as caught:
            assert_content_declaration(CAMPAIGN_ENVIRONMENT_VERSION, drifted)
        self.assertIn("different environment", str(caught.exception))

    def test_an_unfrozen_version_may_declare_anything(self) -> None:
        digest = assert_content_declaration(
            "sts2sim-campaign-fidelity-v2", {"verdict": "content_verified"}
        )
        self.assertEqual(len(digest), 64)

    def test_combining_environments_is_refused(self) -> None:
        self.assertEqual(
            assert_single_environment(["sts2sim-campaign-approx-v1"] * 3),
            "sts2sim-campaign-approx-v1",
        )
        with self.assertRaises(EnvironmentVersionError):
            assert_single_environment(
                ["sts2sim-campaign-approx-v1", "sts2sim-campaign-fidelity-v2"]
            )
        # An unlabelled artifact is a label of its own: it cannot join a versioned
        # rollup just because the field is missing.
        with self.assertRaises(EnvironmentVersionError):
            assert_single_environment([CAMPAIGN_ENVIRONMENT_VERSION, None])


class FidelityHarnessTests(unittest.TestCase):
    """The harness must be reading the engine, not a regex that quietly stopped matching."""

    def setUp(self) -> None:
        self.gates = measure_gates(emulator_encounter_ids(), __import__(
            "scripts.verify_campaign_fidelity_gates", fromlist=["pool_tables"]
        ).pool_tables())

    def test_every_anchor_resolved_in_the_engine_source(self) -> None:
        self.assertTrue(
            self.gates["G5_reward_and_upgrade_distribution"]["engine"][
                "RollCardUpgrade_definition_found"
            ],
            "the upgrade-roll probe lost its definition; the gate verdict is meaningless",
        )
        self.assertIn(
            "return false",
            self.gates["G5_reward_and_upgrade_distribution"]["engine"][
                "RollCardUpgrade_body"
            ],
        )
        self.assertIn(
            "UnderdocksBossEncounters",
            self.gates["G3_second_boss_structure"]["engine"]["paired_final_act_boss_body"],
        )

    def test_all_six_gates_are_named_and_none_passes_under_the_approximate_version(
        self
    ) -> None:
        self.assertEqual(
            sorted(self.gates),
            [
                "G1_act_pools",
                "G2_ancients",
                "G3_second_boss_structure",
                "G4_map_shape",
                "G5_reward_and_upgrade_distribution",
                "G6_no_boss_relic_reward",
            ],
        )
        passed = [name for name, gate in self.gates.items() if gate.get("passed")]
        self.assertEqual(
            passed, [], f"{passed} claims a hard fidelity gate while the artifacts are "
            f"stamped {CAMPAIGN_ENVIRONMENT_VERSION}; that version must be retired first"
        )

    def test_the_harness_exits_nonzero_until_the_gates_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "gates.json"
            proc = subprocess.run(
                [sys.executable, str(HARNESS), "--out", str(out)],
                cwd=ROOT, capture_output=True, text=True, check=False,
            )
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertFalse(payload["hard_gate_passed"])
            self.assertEqual(payload["environment_version"], CAMPAIGN_ENVIRONMENT_VERSION)


if __name__ == "__main__":
    unittest.main()
