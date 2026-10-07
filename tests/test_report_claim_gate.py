"""The claim verifier must not bless a check that came out False.

Expectations are snapshots compared for equality, so before this gate a pinned ``false``
read exactly like a passing run -- "40/40 scored claims match the disk" with a failed check
inside it.  These tests lock the gate down, lock the committed expectations to no false
checks, and lock a claim that cannot run on this interpreter to be an error the tally names
rather than a mismatch it hides.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "verify_report_claims", ROOT / "scripts" / "verify_report_claims.py")
V = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(V)


class ReportClaimGateTests(unittest.TestCase):
    def _capture_with(self, claim_result, pinned: dict, extra: dict | None = None,
                      raiser: bool = False) -> tuple[int, str]:
        """Drive main() over one synthetic claim, restoring the module afterwards."""
        original = dict(V.CLAIMS)
        argv = list(sys.argv)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "expectations.json"
            expectations = {"demo": pinned}
            expectations.update(extra or {})
            path.write_text(json.dumps(expectations), encoding="utf-8")

            def claim() -> dict:
                if raiser:
                    raise ModuleNotFoundError("No module named 'numpy'")
                return claim_result

            V.CLAIMS.clear()
            V.CLAIMS["demo"] = (claim, "demo claim")
            sys.argv = ["verify_report_claims.py", "--expect", str(path), "--quiet"]
            buffer = io.StringIO()
            try:
                with contextlib.redirect_stdout(buffer):
                    code = V.main()
                return code, buffer.getvalue()
            finally:
                sys.argv = argv
                V.CLAIMS.clear()
                V.CLAIMS.update(original)

    def _run_with(self, claim_result: dict, pinned: dict, extra: dict | None = None) -> int:
        return self._capture_with(claim_result, pinned, extra)[0]

    def test_a_pinned_false_check_is_reported_as_drift(self) -> None:
        result = {"holds": False, "other": True}
        self.assertEqual(1, self._run_with(result, result))

    def test_a_pinned_true_check_still_passes(self) -> None:
        result = {"holds": True, "other": True}
        self.assertEqual(0, self._run_with(result, result))

    def test_a_claim_that_cannot_run_is_named_and_never_reads_as_clean(self) -> None:
        # Running this harness on the contract interpreter used to print "39/40 claims match
        # the disk" for an import error, which reads like the report drifted from the files.
        code, output = self._capture_with({}, {}, raiser=True)
        self.assertEqual(1, code, "an unrunnable claim must still fail the run")
        self.assertIn("were not scored: demo", output)
        self.assertNotIn("1/0", output)
        self.assertIn("0/0 scored claims match the disk (1 registered)", output)

    def test_a_declared_false_check_is_allowed_through(self) -> None:
        # An author may legitimately pin a refutation; that has to be declared, not implicit.
        code = self._run_with({"holds": False}, {"holds": False},
                              extra={"_expected_false_checks": {"demo": ["holds"]}})
        self.assertEqual(0, code)

    def test_committed_expectations_contain_no_false_checks(self) -> None:
        expectations = json.loads(
            (ROOT / "docs" / "evidence" / "act1_report_expectations.json").read_text(
                encoding="utf-8"))
        deliberate = expectations.get("_expected_false_checks", {})
        false: dict[str, list[str]] = {}
        for name, checks in expectations.items():
            if not isinstance(checks, dict) or name.startswith("_"):
                continue
            allowed = set(deliberate.get(name, ()))
            bad = sorted(key for key, value in checks.items()
                         if value is False and key not in allowed)
            if bad:
                false[name] = bad
        self.assertEqual({}, false, "a false check is pinned as if it were a passing one")

    def test_every_registered_claim_is_pinned(self) -> None:
        expectations = json.loads(
            (ROOT / "docs" / "evidence" / "act1_report_expectations.json").read_text(
                encoding="utf-8"))
        unpinned = sorted(set(V.CLAIMS) - set(expectations))
        self.assertEqual([], unpinned, "an unpinned claim reports UNPINNED and counts as drift")


class DeadEndVocabularyVerdictTests(unittest.TestCase):
    """The restated step_cap claims have to be able to fail.

    `vocabulary_is_exactly_three_labels` and `step_cap_is_act1_stage_only` were frozen
    literals and went red the moment the campaign arm produced new facts -- a census that
    is really a snapshot. They were replaced by a label-set check plus counts and stages
    derived from the artifact and from ``config/``. This class is the differential that
    says the replacement is not just a softer sentence: each way the corpus can drift now
    is handed to the scorer deliberately, and each one still goes red.
    """

    ARTIFACT = {
        "vocabulary": {"empty_action_mask": 50, "native_rejection": 861, "step_cap": 6},
        "by_reason_and_stage": {
            "step_cap": {"act1/checkpoint": 2, "act1/promotion": 1,
                         "full_run/checkpoint": 1, "full_run/promotion": 2},
        },
        "step_cap_files": [{"reasons": {"step_cap": 1}} for _ in range(6)],
    }
    HORIZONS = {"act1": [1600], "full_run": [4800], "combat": [80]}
    BASE_VOCABULARY = {"empty_action_mask": 50, "native_rejection": 861, "step_cap": 6}
    BASE_CELLS = {"act1/checkpoint": 2, "act1/promotion": 1,
                  "full_run/checkpoint": 1, "full_run/promotion": 2}

    def _verdicts(self, **overrides) -> dict:
        kwargs = {"vocabulary": dict(self.BASE_VOCABULARY),
                  "cap_cells": dict(self.BASE_CELLS),
                  "cap_stages": {"act1", "full_run"},
                  "unclassified": 0,
                  "extra_labels": {"curriculum_truncated": 6},
                  "unaccounted": [],
                  "artifact": self.ARTIFACT,
                  "horizons": self.HORIZONS,
                  "legacy_native": 861,
                  "current_native": 0}
        kwargs.update(overrides)
        return V.evaluate_vocabulary_verdicts(**kwargs)

    def test_the_healthy_census_scores_every_check_true(self) -> None:
        verdicts = self._verdicts()
        self.assertNotIn(False, list(verdicts.values()), json.dumps(verdicts, indent=1))

    def test_a_fourth_label_goes_red_without_touching_the_counts(self) -> None:
        verdicts = self._verdicts(
            vocabulary={**self.BASE_VOCABULARY, "wedged_at_chest": 1},
            artifact={**self.ARTIFACT,
                      "vocabulary": {**self.ARTIFACT["vocabulary"], "wedged_at_chest": 1}})
        self.assertFalse(verdicts["vocabulary_labels_are_exactly_the_three_named"])
        self.assertTrue(verdicts["step_cap_cells_match_the_artifact"])

    def test_a_step_cap_in_a_stage_with_no_configured_horizon_goes_red(self) -> None:
        # A stage the configs never gave a max_episode_steps cannot produce step_cap at
        # all, so seeing one there is a labelling defect, not a bigger corpus.
        verdicts = self._verdicts(
            cap_cells={**self.BASE_CELLS, "glory/checkpoint": 1},
            cap_stages={"act1", "full_run", "glory"},
            artifact={**self.ARTIFACT, "vocabulary": {**self.ARTIFACT["vocabulary"]},
                      "by_reason_and_stage": {
                          **self.ARTIFACT["by_reason_and_stage"],
                          "step_cap": {**self.BASE_CELLS, "glory/checkpoint": 1}},
                      "step_cap_files": self.ARTIFACT["step_cap_files"] + [
                          {"reasons": {"step_cap": 1}}]},
            vocabulary={**self.BASE_VOCABULARY, "step_cap": 7})
        self.assertFalse(
            verdicts["step_cap_appears_only_where_a_horizon_is_configured"])
        # ...and the artifact tie keeps up, so the two checks cannot disagree in silence.
        self.assertTrue(verdicts["step_cap_cells_match_the_artifact"])

    def test_a_cell_count_the_artifact_does_not_claim_goes_red(self) -> None:
        verdicts = self._verdicts(cap_cells={**self.BASE_CELLS, "act1/promotion": 4},
                                  vocabulary={**self.BASE_VOCABULARY, "step_cap": 9})
        self.assertFalse(verdicts["step_cap_cells_match_the_artifact"])

    def test_an_empty_step_cap_population_is_red_not_vacuously_green(self) -> None:
        # The old "act1 stage only" check would have passed on a census that recorded no
        # step_cap at all, exactly the broken-probe reading the project already bans.
        verdicts = self._verdicts(
            vocabulary={"empty_action_mask": 50, "native_rejection": 861},
            cap_cells={}, cap_stages=set(),
            artifact={**self.ARTIFACT, "vocabulary": {"empty_action_mask": 50,
                                                      "native_rejection": 861},
                      "by_reason_and_stage": {"step_cap": {}}, "step_cap_files": []})
        self.assertFalse(
            verdicts["step_cap_appears_only_where_a_horizon_is_configured"])
        self.assertFalse(verdicts["acts_too_slowly_is_labelled"])

    def test_growing_the_corpus_moves_counts_without_breaking_the_label_claim(self) -> None:
        # This is the change that made the two frozen literals fail: one more campaign
        # episode hitting its horizon. The label set is untouched, the counts follow the
        # artifact, and nothing here needs editing.
        grown_cells = {**self.BASE_CELLS, "full_run/promotion": 3}
        grown = {**self.ARTIFACT,
                 "vocabulary": {**self.ARTIFACT["vocabulary"], "step_cap": 7},
                 "by_reason_and_stage": {"step_cap": grown_cells},
                 "step_cap_files": self.ARTIFACT["step_cap_files"] + [
                     {"reasons": {"step_cap": 1}}]}
        verdicts = self._verdicts(vocabulary={**self.BASE_VOCABULARY, "step_cap": 7},
                                  cap_cells=grown_cells, artifact=grown)
        self.assertNotIn(False, list(verdicts.values()), json.dumps(verdicts, indent=1))

    def test_the_configured_horizons_are_read_from_config_not_a_literal(self) -> None:
        horizons = V._configured_stage_horizons()
        self.assertIn("full_run", horizons)
        self.assertIn("act1", horizons)
        self.assertTrue(all(steps for values in horizons.values() for steps in values))


if __name__ == "__main__":
    unittest.main()
