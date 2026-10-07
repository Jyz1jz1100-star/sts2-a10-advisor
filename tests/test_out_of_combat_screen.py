"""The G4 screen's arithmetic is the pre-registration, so it gets tested like one.

Nothing in this file touches the simulator: it tests the split, the paired statistic, and the
verdict mapping -- the three places where a silent bug would turn an undetermined screen into a
reported pass.  The pre-registered constants (delta, baseline, metric, salt) are pinned here too,
because the whole point of writing them down before the first look is that they cannot move after
it, and a test that fails when someone moves one is the only thing that makes that promise real.
"""
from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "screen_out_of_combat_intervention",
    ROOT / "scripts" / "screen_out_of_combat_intervention.py")
S = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(S)


class RegisteredConstantsTests(unittest.TestCase):
    def test_the_registered_values_are_the_ones_in_the_gates_doc(self) -> None:
        self.assertEqual(S.METRIC, "act1_boss_arrival")
        self.assertEqual(S.DELTA, 0.015)
        self.assertAlmostEqual(S.BASELINE_ARRIVAL, 309 / 10_000, places=12)
        self.assertEqual(S.ACT1_BOSS_FLOOR, 17)
        self.assertEqual(S.SPLIT_SALT, "g4-out-of-combat-screen-20261007")

    def test_the_amendment_is_recorded_in_the_artifact_shape(self) -> None:
        # The arms were re-defined before any look; the artifact has to say so, with the reason,
        # or a reader later sees two arms nobody registered.
        source = (ROOT / "scripts" / "screen_out_of_combat_intervention.py").read_text(
            encoding="utf-8")
        self.assertIn("arm_amendment", source)
        self.assertIn("criteria_unchanged", source)


class HoldoutSplitTests(unittest.TestCase):
    def test_the_split_is_deterministic(self) -> None:
        self.assertEqual(S.holdout_of(2008010007), S.holdout_of(2008010007))

    def test_the_two_halves_are_complementary_and_neither_is_empty(self) -> None:
        seeds = list(range(2008010000, 2008012000))
        halves = [S.holdout_of(seed) for seed in seeds]
        self.assertEqual(set(halves), {"screen", "sealed"})
        screen = halves.count("screen")
        self.assertTrue(0.45 * len(seeds) <= screen <= 0.55 * len(seeds),
                        f"split ratio {screen / len(seeds):.3f} is not a half")

    def test_the_salt_moves_the_split_so_it_is_not_seed_arithmetic(self) -> None:
        seeds = list(range(2008010000, 2008010200))
        self.assertNotEqual([S.holdout_of(s) for s in seeds],
                            [S.holdout_of(s, "other-salt") for s in seeds])


class PairedStatisticTests(unittest.TestCase):
    def test_the_paired_difference_counts_the_discordant_cells(self) -> None:
        pairs = [(True, False)] * 60 + [(False, True)] * 10 + [(True, True)] * 5 \
            + [(False, False)] * 925
        stats = S.mcnemar_paired(pairs)
        self.assertEqual(1000, stats["pairs"])
        self.assertEqual((60, 10, 70), (stats["trained_only"], stats["positional_only"],
                                        stats["discordant"]))
        self.assertAlmostEqual(0.05, stats["paired_arrival_difference"], places=6)
        self.assertTrue(stats["lower_bound_clears_delta"])

    def test_a_small_edge_does_not_clear_the_delta(self) -> None:
        # +1.5pp is the bar: an interval that excludes zero but straddles the delta is not a pass.
        pairs = [(True, False)] * 25 + [(False, True)] * 10 + [(False, False)] * 965
        stats = S.mcnemar_paired(pairs)
        self.assertAlmostEqual(0.015, stats["paired_arrival_difference"], places=6)
        self.assertFalse(stats["lower_bound_clears_delta"])
        self.assertTrue(stats["interval_excludes_zero_from_below"])

    def test_a_negative_edge_says_stop_rather_than_nothing(self) -> None:
        pairs = [(False, True)] * 60 + [(True, False)] * 5 + [(False, False)] * 935
        stats = S.mcnemar_paired(pairs)
        self.assertTrue(stats["upper_bound_below_zero"])

    def test_perfect_agreement_reports_no_information(self) -> None:
        pairs = [(False, False)] * 500 + [(True, True)] * 500
        stats = S.mcnemar_paired(pairs)
        self.assertEqual(0, stats["discordant"])
        self.assertFalse(stats["lower_bound_clears_delta"])
        self.assertEqual("undetermined: no discordant pair, so the screen has no information yet",
                         S.verdict(stats, 1000))

    def test_an_empty_sample_cannot_produce_a_pass(self) -> None:
        stats = S.mcnemar_paired([])
        self.assertEqual(0.0, stats["paired_arrival_difference"])
        self.assertFalse(stats["lower_bound_clears_delta"])


class VerdictTests(unittest.TestCase):
    def _stats(self, **kw):
        base = {"pairs": 1000, "trained_only": 0, "positional_only": 0, "discordant": 1,
                "paired_arrival_difference": 0.0,
                "wilson_95_on_discordant_share": [0.0, 0.0],
                "normal_95_on_paired_difference": [-0.01, 0.02],
                "delta": 0.015, "baseline_arrival": 0.0309,
                "lower_bound_clears_delta": False,
                "interval_excludes_zero_from_below": False, "upper_bound_below_zero": False}
        base.update(kw)
        return base

    def test_a_clearing_lower_bound_is_the_only_pass(self) -> None:
        self.assertTrue(S.verdict(self._stats(lower_bound_clears_delta=True), 1000)
                        .startswith("pass"))

    def test_a_wholly_negative_interval_stops_the_route(self) -> None:
        self.assertTrue(S.verdict(self._stats(upper_bound_below_zero=True), 1000)
                        .startswith("stop"))

    def test_a_crossing_interval_is_undetermined_not_optimistic(self) -> None:
        self.assertTrue(S.verdict(self._stats(), 1000).startswith("undetermined"))

    def test_fewer_pairs_than_registered_is_said_out_loud(self) -> None:
        verdict = S.verdict(self._stats(), 400)
        self.assertIn("undetermined", verdict)


class RolloutShapeTests(unittest.TestCase):
    def test_the_positional_arm_takes_the_lowest_legal_index(self) -> None:
        mask = [False] * 12
        mask[7] = True
        mask[9] = True
        self.assertEqual(7, S._positional(None, mask))

    def test_an_empty_mask_does_not_index_out_of_range(self) -> None:
        self.assertEqual(0, S._positional(None, [False] * 12))

    def test_rows_carry_the_fields_the_claim_needs(self) -> None:
        # The screen's artifact is the only record of the pairing, so the row keys are part of its
        # contract; renaming one silently would let a future reader rebuild the pairs wrong.
        required = {"seed", "arm", "holdout", "generated_act", "floors_by_act",
                    "reached_act1_boss", "agent_steps", "absorbed_combat_steps", "agent_phases"}
        source = (ROOT / "scripts" / "screen_out_of_combat_intervention.py").read_text(
            encoding="utf-8")
        for key in required:
            self.assertIn(f'"{key}"', source, f"row field {key} disappeared")


if __name__ == "__main__":
    unittest.main()
