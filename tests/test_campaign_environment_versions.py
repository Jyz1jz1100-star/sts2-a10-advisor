"""A frozen environment keeps its meaning, and a gate cannot pass on text alone.

The campaign is stamped ``sts2sim-campaign-fidelity-v3``; before it, every artifact carried
``sts2sim-campaign-approx-v1``, and fidelity v2 froze a declaration that G2 was still open.
Three things have to stay true: nobody may
edit what a frozen version *contains* while keeping its name (history would become
unreadable), the retired approximate declaration has to stay readable as what it was,
and a hard fidelity gate may not close on a probe that only read source text -- a
transition gate needs the engine to have walked it.
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
    CAMPAIGN_CONTENT_COVERAGE_APPROX_V1,
    CAMPAIGN_CONTENT_COVERAGE_FIDELITY_V2,
    FROZEN_ENVIRONMENT_DECLARATIONS,
    CAMPAIGN_ENVIRONMENT_VERSION,
    FROZEN_ENVIRONMENT_DECLARATIONS,
    FROZEN_ENVIRONMENT_VERSIONS,
    EnvironmentVersionError,
    _canonical_digest,
    assert_content_declaration,
    assert_single_environment,
)
from scripts.verify_campaign_fidelity_gates import (
    EMULATOR,
    REAL_POOLS,
    SCENARIO_TESTS,
    apply_dynamic_sample,
    emulator_encounter_ids,
    measure_gates,
    pool_tables,
)

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "verify_campaign_fidelity_gates.py"

APPROX_V1 = "sts2sim-campaign-approx-v1"


def _ids_for(ids: dict[str, int], act: str) -> list[int]:
    """One encounter per tier, by name, from the same table the harness reads."""
    pool = REAL_POOLS[act]
    return [ids[pool[tier][0]] for tier in ("weak", "normal", "elite") if pool[tier][0] in ids]


def _one_id(ids: dict[str, int], name: str) -> int:
    return ids[name]


class FrozenEnvironmentTests(unittest.TestCase):
    def test_every_frozen_digest_is_the_declaration_on_disk(self) -> None:
        """Each pinned digest must recompute, or the freeze is a claim about nothing."""
        self.assertEqual(
            set(FROZEN_ENVIRONMENT_VERSIONS), set(FROZEN_ENVIRONMENT_DECLARATIONS)
        )
        for version, entry in FROZEN_ENVIRONMENT_VERSIONS.items():
            with self.subTest(version=version):
                declaration = FROZEN_ENVIRONMENT_DECLARATIONS[version]
                self.assertEqual(
                    _canonical_digest(declaration), entry["content_sha256"]
                )
                self.assertEqual(declaration["environment_version"], version)
                self.assertEqual(
                    assert_content_declaration(version, declaration),
                    entry["content_sha256"],
                )

    def test_the_live_pair_is_the_version_new_artifacts_get_stamped_with(self) -> None:
        self.assertEqual(
            CAMPAIGN_ENVIRONMENT_VERSION,
            CAMPAIGN_CONTENT_COVERAGE["environment_version"],
        )
        self.assertEqual(
            assert_content_declaration(
                CAMPAIGN_ENVIRONMENT_VERSION, CAMPAIGN_CONTENT_COVERAGE
            ),
            FROZEN_ENVIRONMENT_VERSIONS[CAMPAIGN_ENVIRONMENT_VERSION]["content_sha256"],
        )

    def test_the_retired_fidelity_version_stays_readable_as_what_it_was(self) -> None:
        """v2 said "no per-act Ancient"; that has to stay true of v2's label."""
        entry = FROZEN_ENVIRONMENT_VERSIONS["sts2sim-campaign-fidelity-v2"]
        v2 = FROZEN_ENVIRONMENT_DECLARATIONS["sts2sim-campaign-fidelity-v2"]
        self.assertEqual(entry["gates_closed"],
                         ["G6_no_boss_relic_reward", "G1_act_pools", "G3_second_boss_structure"])
        self.assertEqual(v2["gates_open"],
                         ["G2_ancients", "G4_map_shape", "G5_reward_and_upgrade_distribution"])
        self.assertIn("a per-act Ancient or a real Act 2/Act 3 event pool",
                      v2["must_not_be_quoted_as"])
        self.assertNotEqual(entry["content_sha256"],
                           FROZEN_ENVIRONMENT_VERSIONS[CAMPAIGN_ENVIRONMENT_VERSION]["content_sha256"])

    def test_the_retired_approximate_version_stays_readable_as_approximate(self) -> None:
        """fidelity-v2 supersedes approx-v1 without rewriting it.

        An artifact stamped approx-v1 has to keep meaning "stages 2 and 3 are re-skinned
        Act 1", or the pre-fidelity win rates read as fidelity results.
        """
        entry = FROZEN_ENVIRONMENT_VERSIONS[APPROX_V1]
        self.assertEqual(entry["verdict"], "approximate")
        self.assertEqual(entry["gates_closed"], [])
        self.assertEqual(CAMPAIGN_CONTENT_COVERAGE_APPROX_V1["result_tier"],
                         "simulator_three_act_approx")
        self.assertFalse(CAMPAIGN_CONTENT_COVERAGE_APPROX_V1["stages"]["2"]["matches_real_game_act"])
        self.assertFalse(CAMPAIGN_CONTENT_COVERAGE_APPROX_V1["stages"]["3"]["matches_real_game_act"])
        self.assertNotEqual(entry["content_sha256"],
                            FROZEN_ENVIRONMENT_VERSIONS[CAMPAIGN_ENVIRONMENT_VERSION]["content_sha256"])

    def test_the_published_version_closes_four_gates_and_claims_no_more(self) -> None:
        self.assertEqual(
            sorted(CAMPAIGN_CONTENT_COVERAGE["gates_closed"]),
            sorted(
                [
                    "G6_no_boss_relic_reward",
                    "G1_act_pools",
                    "G3_second_boss_structure",
                    "G2_ancients",
                ]
            ),
        )
        self.assertEqual(
            sorted(CAMPAIGN_CONTENT_COVERAGE["gates_open"]),
            ["G4_map_shape", "G5_reward_and_upgrade_distribution"],
        )
        # Closing three gates is not closing the environment: the verdict stays
        # approximate until every hard gate is shut.
        self.assertEqual(CAMPAIGN_CONTENT_COVERAGE["verdict"], "approximate")
        self.assertNotEqual(
            CAMPAIGN_CONTENT_COVERAGE["result_tier"],
            "simulator_three_act_content_verified",
        )

    def test_editing_frozen_content_under_the_same_name_raises(self) -> None:
        drifted = copy.deepcopy(CAMPAIGN_CONTENT_COVERAGE)
        drifted["stages"]["3"]["event_pools"] = "Glory events"  # type: ignore[index]
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
            "sts2sim-campaign-fidelity-v4", {"verdict": "content_verified"}
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
        self.ids = emulator_encounter_ids()
        self.gates = measure_gates(self.ids, pool_tables())

    def test_every_anchor_resolved_in_the_engine_source(self) -> None:
        """A probe whose regex stopped matching reads as a verdict, so pin them."""
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
        second_boss = self.gates["G3_second_boss_structure"]
        self.assertIn(
            "IsGloryAct", second_boss["engine"]["generate_second_boss_body"]
        )
        self.assertIn(
            "MapBossRow + 1", second_boss["engine"]["generate_second_boss_body"]
        )
        self.assertIn(
            "SecondBossCoord", second_boss["engine"]["open_second_boss_row_body"]
        )
        self.assertTrue(second_boss["engine"]["dealt_at_act_generation_excluding_the_first"])
        self.assertTrue(second_boss["engine"]["reached_through_a_map_row_past_the_boss"])
        self.assertFalse(second_boss["engine"]["paired_combat_still_started_on_the_spot"])
        self.assertTrue(self.gates["G6_no_boss_relic_reward"]["engine"]["definition_found"])
        self.assertEqual(
            self.gates["G6_no_boss_relic_reward"]["engine"]["post_combat_relic_grant"],
            "state.CurrentNodeType is RunConstants.NodeElite",
        )

    def test_scenario_test_names_live_in_the_engine_suite(self) -> None:
        suite = (
            EMULATOR
            / "src"
            / "Sts2Emulator.Tests"
            / "RunEngineTests.cs"
        ).read_text(encoding="utf-8")
        for names in SCENARIO_TESTS.values():
            for name in names:
                self.assertIn(name, suite, f"{name} was renamed or deleted")

    def test_all_six_gates_are_named_and_none_passes_on_source_text_alone(self) -> None:
        """G1 needs a walk, G3 and G6 need the engine to run its own scenario."""
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
        self.assertEqual(
            [name for name, gate in self.gates.items() if gate.get("passed")],
            [],
            f"{[n for n, g in self.gates.items() if g.get('passed')]} passed with no "
            "dynamic sample and no engine scenario evidence",
        )
        self.assertFalse(
            self.gates["G3_second_boss_structure"]["scenario"]["measured"]
        )
        self.assertFalse(
            self.gates["G6_no_boss_relic_reward"]["scenario"]["measured"]
        )

    def test_the_engine_scenario_closes_g3_and_g6_and_only_those(self) -> None:
        scenario = {
            "measured": True,
            "evidence": {"note": "synthetic fixture: the runner's own verdict"},
            "G3_second_boss_structure": True,
            "G6_no_boss_relic_reward": True,
        }
        gates = measure_gates(self.ids, pool_tables(), scenario)
        self.assertTrue(gates["G3_second_boss_structure"]["passed"])
        self.assertTrue(gates["G6_no_boss_relic_reward"]["passed"])
        self.assertEqual(
            [
                name
                for name in ("G1_act_pools", "G2_ancients", "G4_map_shape",
                             "G5_reward_and_upgrade_distribution")
                if gates[name]["passed"]
            ],
            [],
        )

    def test_a_scenario_that_did_not_run_leaves_the_gates_open(self) -> None:
        scenario = {"measured": False, "reason": "dotnet unavailable"}
        gates = measure_gates(self.ids, pool_tables(), scenario)
        self.assertFalse(gates["G3_second_boss_structure"]["passed"])
        self.assertFalse(gates["G6_no_boss_relic_reward"]["passed"])

    def test_g1_needs_both_legs_and_reports_an_act_nobody_reached(self) -> None:
        """A played walk covers what it survived into; the generator covers the rest."""
        fought = {
            "measured": True,
            "drawn_by_act": {
                "act_1": _ids_for(self.ids, "act_1_overgrowth"),
                "act_2": _ids_for(self.ids, "act_2_hive"),
                "act_3": _ids_for(self.ids, "act_3_glory"),
            },
            "drawn_counts": {"act_1": 3, "act_2": 3, "act_3": 3},
        }
        gates = apply_dynamic_sample(
            measure_gates(self.ids, pool_tables()), self.ids, fought
        )
        self.assertTrue(gates["G1_act_pools"]["dynamic_leg_passed"])
        self.assertFalse(
            gates["G1_act_pools"]["passed"],
            "a walk is not enough on its own: the generator leg has to draw every act too",
        )
        self.assertEqual(gates["G1_act_pools"]["act_2_in_pool"], [3, 3])

        with_pool_leg = apply_dynamic_sample(
            measure_gates(
                self.ids,
                pool_tables(),
                {"measured": True, "G1_act_pools": True, "evidence": {}},
            ),
            self.ids,
            fought,
        )
        self.assertTrue(with_pool_leg["G1_act_pools"]["passed"])

        # The policy on disk clears Act 1 for a couple of seeds in ten thousand, so an
        # empty Act 3 is the normal shape of this sample. It has to be named.
        shallow = {
            "measured": True,
            "drawn_by_act": {
                "act_1": _ids_for(self.ids, "act_1_overgrowth"),
                "act_2": _ids_for(self.ids, "act_2_hive"),
                "act_3": [],
            },
        }
        gates = apply_dynamic_sample(
            measure_gates(
                self.ids, pool_tables(), {"measured": True, "G1_act_pools": True}
            ),
            self.ids,
            shallow,
        )
        self.assertEqual(gates["G1_act_pools"]["unreached_acts"], ["act_3"])
        self.assertTrue(gates["G1_act_pools"]["passed"])

        contaminated = {
            "measured": True,
            "drawn_by_act": {
                **shallow["drawn_by_act"],
                "act_2": shallow["drawn_by_act"]["act_2"][:1]
                + [_one_id(self.ids, "seapunk")],
            },
        }
        gates = apply_dynamic_sample(
            measure_gates(
                self.ids, pool_tables(), {"measured": True, "G1_act_pools": True}
            ),
            self.ids,
            contaminated,
        )
        self.assertEqual(
            gates["G1_act_pools"]["act_2_offside"], [_one_id(self.ids, "seapunk")]
        )
        self.assertFalse(gates["G1_act_pools"]["dynamic_leg_passed"])
        self.assertFalse(gates["G1_act_pools"]["passed"])

        # An act nobody walked into is not an act with clean draws.
        empty = {"measured": True, "drawn_by_act": {"act_1": [], "act_2": [], "act_3": []}}
        gates = apply_dynamic_sample(
            measure_gates(self.ids, pool_tables()), self.ids, empty
        )
        self.assertFalse(gates["G1_act_pools"]["passed"])
        self.assertIn("no in-combat encounter", str(gates["G1_act_pools"]))
        unmeasured = apply_dynamic_sample(
            measure_gates(self.ids, pool_tables()), self.ids,
            {"measured": False, "reason": "no checkpoint"},
        )
        self.assertFalse(unmeasured["G1_act_pools"]["passed"])

    def test_the_harness_exits_nonzero_until_the_gates_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "gates.json"
            proc = subprocess.run(
                [sys.executable, str(HARNESS), "--no-engine-scenario", "--out", str(out)],
                cwd=ROOT, capture_output=True, text=True, check=False,
            )
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertFalse(payload["hard_gate_passed"])
            self.assertFalse(payload["published_gates_passed"])
            self.assertEqual(payload["environment_version"], CAMPAIGN_ENVIRONMENT_VERSION)


if __name__ == "__main__":
    unittest.main()
