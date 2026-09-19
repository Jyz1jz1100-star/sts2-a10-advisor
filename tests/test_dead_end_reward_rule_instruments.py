"""The dead-end and reward-screen instruments must fail loudly, not report a tidy number.

Three scripts grew out of the `empty_action_mask` census: the census itself, the boss reward-screen
rule probe, and the slice merger that reconciles a rule's converted seeds against a list some other
measurement wrote down earlier. Each one's whole value is a closure property -- located equals
recorded, a group counts only if it replays the loss, a converted set equals a pre-registered seed
list -- and a closure property that silently stops closing looks exactly like a passing result. These
tests exercise the arithmetic with synthetic rows, without loading the simulator.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CENSUS = _load("census_empty_mask_endings", "scripts/census_empty_mask_endings.py")
MERGE = _load("merge_reward_rule_slices", "scripts/merge_reward_rule_slices.py")
RULE = _load("probe_boss_reward_rule", "scripts/probe_boss_reward_rule.py")


def _entry(metrics_file: str, recorded: int) -> dict:
    return {"metrics_file": metrics_file, "recorded_empty_action_mask": recorded,
            "episodes": 10, "window_seeds": [1], "checkpoint_path": "x",
            "checkpoint_sha256_matches": True, "window_status": "resolved"}


def _row(seed: int, outcome: str, origin: str, *, short_circuit: bool = False,
         bases: list[int] | None = None, hp: int | None = 30) -> dict:
    return {"seed": seed, "outcome": outcome, "origin": origin, "phase": "map", "act": 2,
            "floor": 17, "hp": hp, "max_hp": 80, "alive": bool(hp),
            "engine_legal_bases": bases if bases is not None else [],
            "short_circuit": short_circuit, "rejections": 0, "steps": 5, "detail": None}


class CensusClosureTests(unittest.TestCase):
    def test_a_file_whose_count_changed_is_reported_as_a_mismatch(self) -> None:
        entries = [_entry("a.json", 2), _entry("b.json", 1)]
        rows = [_row(1, "empty_action_mask", "a.json"),
                _row(2, "death", "a.json"), _row(3, "win", "b.json")]
        payload = CENSUS.summarise(entries, rows, merged=False)
        agg = payload["aggregates"]
        self.assertEqual(agg["empty_action_mask_total"], 1)
        self.assertEqual(agg["per_file_mismatch_count"], 2)
        self.assertEqual(agg["recorded_total_in_rolled_files"], 3)
        self.assertEqual(
            sorted(row["metrics_file"] for row in agg["per_file_closure_mismatches"]),
            ["a.json", "b.json"])

    def test_an_exact_match_is_the_only_clean_verdict(self) -> None:
        entries = [_entry("a.json", 1)]
        rows = [_row(1, "empty_action_mask", "a.json"), _row(2, "death", "a.json")]
        agg = CENSUS.summarise(entries, rows, merged=False)["aggregates"]
        self.assertEqual(agg["per_file_mismatch_count"], 0)
        self.assertEqual(agg["empty_action_mask_total"], agg["recorded_total_in_rolled_files"])

    def test_the_labelling_layer_comes_from_the_sentinel_key_alone(self) -> None:
        # Only V2RunEnvWrapper.step() writes that key, so it is what distinguishes the two
        # interceptions; a summary that conflated them would restore a corrected mechanism claim.
        entries = [_entry("a.json", 2)]
        rows = [_row(1, "empty_action_mask", "a.json", short_circuit=False),
                _row(2, "empty_action_mask", "a.json", short_circuit=True)]
        agg = CENSUS.summarise(entries, rows, merged=False)["aggregates"]
        self.assertEqual(agg["labelling_layers_observed"],
                         {"v2_flat_env_sentinel_step": 1, "v2_run_wrapper_short_circuit": 1})

    def test_an_ordinary_death_is_not_counted_as_an_anomaly_but_an_unlabelled_end_is(self) -> None:
        entries = [_entry("a.json", 0)]
        rows = [_row(1, "death", "a.json"), _row(2, "step_cap", "a.json"),
                _row(3, "native_step_raised", "a.json", bases=None)]
        rows[2]["detail"] = "boom"
        payload = CENSUS.summarise(entries, rows, merged=False)
        self.assertEqual(payload["aggregates"]["roll_anomaly_total"], 2)
        self.assertEqual(payload["aggregates"]["empty_action_mask_total"], 0)
        self.assertEqual({row["outcome"] for row in payload["anomaly_rows"]},
                         {"step_cap", "native_step_raised"})

    def test_an_engine_mask_with_any_basis_is_not_counted_as_a_bare_empty_mask(self) -> None:
        # The flat env separates `empty_action_mask` from `rejected_to_exhaustion` on this exact
        # field, so the census must keep them apart rather than trust the label alone.
        entries = [_entry("a.json", 1)]
        rows = [_row(1, "empty_action_mask", "a.json", bases=[4, 9]),
                _row(2, "empty_action_mask", "a.json", bases=[])]
        agg = CENSUS.summarise(entries, rows, merged=False)["aggregates"]
        self.assertEqual(agg["dead_ends_with_no_engine_legal_basis"], 1)


class RewardRuleMergeTests(unittest.TestCase):
    def test_recorded_for_reads_each_groups_own_pre_registered_numbers(self) -> None:
        misexit = {
            "checkpoint": "cp1", "truncation_seeds": [3, 1, 2],
            "whole_partition": {"act1": {"boss_win": 3}, "act2": {"boss_win": 65}},
            "independent_window_checkpoint_split": {
                "truncation_seeds": [7, 5],
                "act1": {"boss_win": 0}, "act2": {"boss_win": 21}},
            "second_checkpoint_generality": {
                "checkpoint": "cp2", "truncation_seeds": [9],
                "act1": {"boss_win": 1}, "act2": {"boss_win": 20}},
        }
        self.assertEqual(MERGE.recorded_for(misexit, "whole_partition_promotion"),
                         ([1, 2, 3], 68))
        self.assertEqual(MERGE.recorded_for(misexit, "independent_window_checkpoint_split"),
                         ([5, 7], 21))
        self.assertEqual(MERGE.recorded_for(misexit, "second_checkpoint_generality"), ([9], 21))

    def test_the_committed_groups_really_do_differ(self) -> None:
        # Guards the parameterisation itself: if two groups ever resolve to the same list, the
        # "seed-for-seed agreement" claim would be comparing an instrument with itself.
        source = json.loads((ROOT / "docs/evidence/act2_boss_misexit_rate_20260919.json")
                            .read_text(encoding="utf-8"))
        promotion = MERGE.recorded_for(source, "whole_partition_promotion")
        window = MERGE.recorded_for(source, "independent_window_checkpoint_split")
        second = MERGE.recorded_for(source, "second_checkpoint_generality")
        self.assertNotEqual(promotion[0], window[0])
        self.assertNotEqual(window[0], second[0])
        self.assertEqual(len(promotion[0]), 21)
        self.assertEqual(len(window[0]), 6)
        self.assertEqual(len(second[0]), 7)

    def test_a_duplicated_seed_across_slices_stops_the_merge(self) -> None:
        # The real slices are disjoint, so the guard has to be shown with slices that are not:
        # a summed win count would otherwise double-count whichever seed appears twice.
        scratch = ROOT / "runtime" / "merge_guard_scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        template = {"aggregates": {"episodes": 2, "plain_wins": 1, "ruled_wins": 2,
                                   "screen_states_matched": 1, "truncations_plain": 1,
                                   "truncations_after_rule": 0, "unclassified_dead_ends_plain": 0,
                                   "unclassified_dead_ends_after_rule": 0},
                    "before_rule": {"illegal_actions": 0}, "after_rule": {"illegal_actions": 0,
                                                                          "checkpoint": "cp"},
                    "win_seed_join": {"converted_seeds": [2], "lost_seeds": [], "kept_seeds": [1]},
                    "seed_source": "slice", "seeds": [1, 2], "rule": {}}
        for index in (0, 1):
            (scratch / f"slice_{index}.json").write_text(json.dumps(template), encoding="utf-8")
        out = scratch / "should_not_be_written.json"
        argv = sys.argv
        sys.argv = ["merge_reward_rule_slices.py", "--slices",
                    "runtime/merge_guard_scratch/slice_*.json", "--out", str(out)]
        try:
            with self.assertRaises(SystemExit) as caught:
                MERGE.main()
            self.assertIn("double counted", str(caught.exception))
            self.assertFalse(out.exists())
        finally:
            sys.argv = argv
            out.unlink(missing_ok=True)
            for path in scratch.glob("slice_*.json"):
                path.unlink()
            scratch.rmdir()


class TruncationLedgerTests(unittest.TestCase):
    """The ledger must be able to fail, and must not close because a field was blank.

    `summarise` is the whole audit, so these feed it rows where the identity holds, where it does
    not, and where a term is missing rather than zero -- the last case being the one that would let a
    quiet counter masquerade as balanced books.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.ledger = _load("census_truncation_ledger", "scripts/census_truncation_ledger.py")

    def _row(self, *, truncations, reasons, boundary, wins, unclassified=0, current=True,
             fields=True, file="x.json", stage="act1"):
        accounted = sum(reasons.values()) + unclassified + max(0, boundary - wins)
        return {"accounted": accounted, "boundary_hits": boundary, "carries_no_truncations":
                truncations == 0, "current_schema": current, "dead_end_reasons": reasons,
                "file": file, "has_boundary_field": fields, "has_dead_end_reason_field": fields,
                "has_unclassified_field": fields, "residual": truncations - accounted,
                "schema_version": 3 if current else 1, "split": "promotion", "stage": stage,
                "truncations": truncations, "unclassified": unclassified, "wins": wins}

    def test_a_balanced_ledger_reports_no_residual_and_no_violations(self) -> None:
        rows = [self._row(truncations=52, reasons={"empty_action_mask": 50, "step_cap": 2},
                          boundary=134, wins=134),
                self._row(truncations=93, reasons={}, boundary=93, wins=0, stage="floor3")]
        agg = self.ledger.summarise(rows)["aggregates"]
        self.assertEqual(agg["current_schema_residual_total"], 0)
        self.assertEqual(agg["nonzero_residual_files"], 0)
        self.assertEqual(agg["current_schema_files_missing_any_identity_field"], 0)

    def test_truncations_that_no_category_explains_are_a_violation(self) -> None:
        rows = [self._row(truncations=10, reasons={"empty_action_mask": 4}, boundary=0, wins=0)]
        agg = self.ledger.summarise(rows)["aggregates"]
        self.assertEqual(agg["nonzero_residual_files"], 1)
        self.assertEqual(agg["current_schema_residual_total"], 6)
        self.assertEqual(agg["files_with_a_nonzero_residual"][0]["residual"], 6)

    def test_a_missing_field_is_counted_rather_than_read_as_zero(self) -> None:
        # Residual is zero here, yet the row must still be flagged: a file that never wrote the
        # boundary key cannot support the identity that subtracts it.
        rows = [self._row(truncations=0, reasons={}, boundary=0, wins=0, fields=False)]
        agg = self.ledger.summarise(rows)["aggregates"]
        self.assertEqual(agg["nonzero_residual_files"], 0)
        self.assertEqual(agg["current_schema_files_missing_any_identity_field"], 1)

    def test_legacy_files_are_split_between_untestable_and_vacuous(self) -> None:
        rows = [self._row(truncations=10, reasons={}, boundary=0, wins=0, current=False,
                          file="legacy-a.json"),
                self._row(truncations=0, reasons={}, boundary=0, wins=0, current=False,
                          file="legacy-b.json")]
        agg = self.ledger.summarise(rows)["aggregates"]
        self.assertEqual(agg["legacy_files_excluded"], 2)
        self.assertEqual(agg["legacy_files_missing_the_boundary_field"], 2)
        self.assertEqual(agg["legacy_files_with_truncations"], 1)
        self.assertEqual(agg["legacy_files_with_no_truncations_at_all"], 1)


class _StubModel:
    def __init__(self, action: int):
        self._action = action
        self.calls = 0

    def predict(self, observation, action_masks=None, deterministic=True):
        self.calls += 1
        return np.asarray([self._action], dtype=np.int64), None


class RewardScreenRuleTests(unittest.TestCase):
    def _observation(self, *, phase: str, node: int):
        from training.v2_observation import BLOCK_OFFSETS, PHASE_NAMES

        vector = np.zeros(1739, dtype=np.int32)
        vector[BLOCK_OFFSETS["phase_onehot"] + PHASE_NAMES.index(phase)] = 1
        vector[BLOCK_OFFSETS["current_node_type_onehot"] + node] = 1
        return vector, BLOCK_OFFSETS

    def test_it_overrides_only_at_a_boss_relic_screen(self) -> None:
        vector, offsets = self._observation(phase="relic_reward", node=6)
        mask = np.zeros(225, dtype=bool)
        mask[[3, 7, 21]] = True
        rule = RULE.RewardScreenRule(_StubModel(3), offsets, "highest_legal")
        action, _ = rule.predict(vector, action_masks=mask)
        self.assertEqual(int(action.item()), 21)
        self.assertEqual(rule.matched, 1)
        self.assertEqual(rule.overrides, [{"from": 3, "to": 21}])

        # A non-boss relic screen and a boss combat state must both be left completely alone.
        other, _ = self._observation(phase="relic_reward", node=3)
        combat, _ = self._observation(phase="combat", node=6)
        for sample in (other, combat):
            untouched, _ = rule.predict(sample, action_masks=mask)
            self.assertEqual(int(untouched.item()), 3)
        self.assertEqual(rule.matched, 1)

    def test_argmax_mode_never_touches_a_decision(self) -> None:
        vector, offsets = self._observation(phase="relic_reward", node=6)
        mask = np.zeros(225, dtype=bool)
        mask[[3, 21]] = True
        plain = RULE.RewardScreenRule(_StubModel(3), offsets, "argmax")
        action, _ = plain.predict(vector, action_masks=mask)
        self.assertEqual(int(action.item()), 3)
        self.assertEqual(plain.matched, 0)


if __name__ == "__main__":
    unittest.main()
