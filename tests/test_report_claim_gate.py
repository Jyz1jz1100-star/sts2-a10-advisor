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


class AuditVerdictShapeTests(unittest.TestCase):
    """The audit table's verdict column is a closed list, not a count of good news.

    `the_other_six_are_marked_achieved_with_limits` required six rows to contain 「达成」
    and one to contain the literal "第一幕达成". When the Act-1 wins failed to reproduce on
    fidelity-v5 the honest restatement ("achieved once, on an engine that no longer exists")
    made that check fail -- a gate that turns a downgrade red is not protecting findings.
    These cases are the differential for the replacement.
    """

    def _verdicts(self) -> list:
        return ["**未达成，且不可表示**",
                "**第一幕曾在 v4 引擎达成；10-07 在 v5 引擎上逐种子重放 0/9 复现**",
                "**达成，但一半是结构性的**", "**达成，并且现在是被恒等式撑着**",
                "**达成**", "**部分达成**", "**达成**",
                "**达成，并且链现在延伸到引擎**", "**达成，并加强为结构性理由**"]

    def test_the_current_table_scores_true(self) -> None:
        verdicts = self._verdicts()
        self.assertNotIn(False, list(V.evaluate_audit_verdicts(verdicts).values()))

    def test_a_clause_that_stops_declaring_its_verdict_goes_red(self) -> None:
        verdicts = self._verdicts()
        verdicts[4] = "这一行改成了散文，没有判定词"
        self.assertFalse(
            V.evaluate_audit_verdicts(verdicts)[
                "every_clause_carries_a_verdict_from_the_closed_list"])

    def test_dropping_the_engine_version_from_the_act1_clause_goes_red(self) -> None:
        verdicts = self._verdicts()
        verdicts[1] = "**第一幕达成；第二幕按引擎判据不是 17 层**"
        self.assertFalse(
            V.evaluate_audit_verdicts(verdicts)["the_act1_clause_names_both_engine_versions"],
            "a bare 达成 must not read as current once v5 refused it")

    def test_the_act1_to_3_clause_being_quietly_greened_goes_red(self) -> None:
        verdicts = self._verdicts()
        verdicts[0] = "**达成**"
        self.assertFalse(
            V.evaluate_audit_verdicts(verdicts)["act_1_to_3_clause_still_says_not_achieved"])

    def test_losing_a_clause_goes_red(self) -> None:
        self.assertFalse(V.evaluate_audit_verdicts(self._verdicts()[:-1])["all_nine_clauses_listed"])

    def test_the_warm_start_clause_being_greened_goes_red(self) -> None:
        verdicts = self._verdicts()
        verdicts[5] = "**达成**"
        self.assertFalse(V.evaluate_audit_verdicts(verdicts)["warm_start_ladder_still_says_partly"])


class DigestFieldShapeTests(unittest.TestCase):
    """A field named sha256 has to be one, or say how short it is.

    The manifest harvests digests with a 64-hex regex, so the 61-character value that
    `act2_boss_misexit_rate_20260919.json` shipped matched nothing: every hash check stayed
    green, and the first thing to notice was a downstream script refusing to roll against a
    digest that named no file. These cases keep that class from coming back, and keep the
    gate from pretending the 16-hex prefix convention is the same defect.
    """

    FULL = "a" * 64
    PREFIX = "a" * 16
    DROPPED = "a" * 61

    def _split(self, tree) -> dict:
        return V.classify_digest_fields(tree)

    def test_a_sixty_one_hex_digest_is_malformed(self) -> None:
        split = self._split({"checkpoint_sha256": self.DROPPED})
        self.assertEqual(["/checkpoint_sha256 (61 hex chars)"], split["malformed"])

    def test_a_full_digest_and_a_declared_prefix_are_both_clean(self) -> None:
        split = self._split({"checkpoint_sha256": self.FULL,
                             "checkpoint_sha256_first16": self.PREFIX})
        self.assertEqual([], split["malformed"])
        self.assertEqual([], split["abbreviated"])
        self.assertEqual(["/checkpoint_sha256"], split["full"])

    def test_a_bare_prefix_under_a_sha256_key_is_counted_not_failed(self) -> None:
        split = self._split({"rows": [{"checkpoint_sha256": self.PREFIX}]})
        self.assertEqual([], split["malformed"])
        self.assertEqual(["/rows[0]/checkpoint_sha256"], split["abbreviated"])

    def test_non_hex_and_unrelated_keys_are_ignored(self) -> None:
        split = self._split({"model_name": "a" * 61, "checkpoint_sha256": "zz" * 30})
        self.assertEqual([], split["malformed"])

    def test_the_committed_bundle_carries_no_malformed_digest(self) -> None:
        offenders = []
        for path in sorted((ROOT / "docs/evidence").glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            offenders += [f"{path.name}{item}"
                          for item in V.classify_digest_fields(payload)["malformed"]]
        self.assertEqual([], offenders, "a digest field that is not a sha256")


class LiveWinReplayAvailabilityTests(unittest.TestCase):
    """The live replay must read "cannot check" when its inputs are gone, never green.

    The replay itself is deliberately not run here (it loads five checkpoints). What is
    pinned is the absence path, because a count pinned at zero would otherwise be satisfied
    by a machine that never measured anything.
    """

    def test_missing_checkpoints_are_named_and_the_replay_is_not_runnable(self) -> None:
        ledger = {"rows": [{"seeds": [1, 2],
                            "checkpoint": "runtime/definitely-absent/step_1.zip",
                            "config": "runtime/definitely-absent.toml"}]}
        result = V.live_win_ledger_replay(ledger)
        self.assertFalse(result["runnable"])
        self.assertIsNone(result["reproduced"])
        self.assertTrue(result["missing"], "absence has to be named, not counted as zero")
        self.assertEqual(2, result["recorded_wins"])

    def test_a_ledger_without_named_wins_is_not_runnable(self) -> None:
        result = V.live_win_ledger_replay({"rows": []})
        self.assertFalse(result["runnable"])
        self.assertEqual(0, result["recorded_wins"])


class PhaseActionShareTests(unittest.TestCase):
    """The responsibility audit has to be falsifiable in both directions.

    80.2% of the steps a campaign arm used to train on were combat decisions the real client hands
    to a third-party solver, so the trainer was optimising a layer the product does not ship.  That
    was carried as a declared false check; ``training/frozen_combat_env.py`` clears it, which makes
    the opposite failure the dangerous one -- an arm that reports ``combat == 0`` because the fights
    went missing rather than because they moved.  Every arithmetic check here therefore has to be
    able to fail for a reason that is not "the file was edited to look aligned".
    """

    ARTIFACT = {
        "total_steps": 100,
        "steps_by_phase": {"combat": 80, "map": 20},
        "step_share_by_phase": {"combat": 0.8, "map": 0.2},
        "combat_step_share": 0.8,
        "out_of_combat_step_share": 0.2,
        "per_episode": [{"seed": 1, "phases": {"combat": 80, "map": 20}}],
    }

    def test_the_recorded_split_is_not_yet_aligned(self) -> None:
        verdicts = V.evaluate_phase_share(dict(self.ARTIFACT), True, [])
        self.assertFalse(verdicts["trained_actions_are_all_out_of_combat"])
        self.assertTrue(verdicts["per_episode_rows_recompute_the_recorded_shares"])

    def test_an_aligned_arm_would_clear_it(self) -> None:
        artifact = {
            "total_steps": 20,
            "steps_by_phase": {"map": 20},
            "step_share_by_phase": {"map": 1.0},
            "combat_step_share": 0.0,
            "per_episode": [{"seed": 1, "phases": {"map": 20}}],
        }
        verdicts = V.evaluate_phase_share(artifact, True, [])
        self.assertTrue(verdicts["trained_actions_are_all_out_of_combat"])
        self.assertTrue(verdicts["combat_share_matches_the_rows"])

    def test_a_tampered_row_breaks_the_recomputed_shares(self) -> None:
        artifact = {**self.ARTIFACT,
                    "per_episode": [{"seed": 1, "phases": {"combat": 40, "map": 60}}]}
        verdicts = V.evaluate_phase_share(artifact, True, [])
        self.assertFalse(verdicts["per_episode_rows_recompute_the_recorded_shares"])
        self.assertFalse(verdicts["combat_share_matches_the_rows"])

    def test_a_missing_checkpoint_is_named_instead_of_reading_clean(self) -> None:
        verdicts = V.evaluate_phase_share(dict(self.ARTIFACT), False, ["x/step.zip"])
        self.assertFalse(verdicts["checkpoint_digest_matches_the_file"])

    def test_an_empty_audit_cannot_report_green(self) -> None:
        artifact = {**self.ARTIFACT, "total_steps": 0, "steps_by_phase": {},
                    "step_share_by_phase": {}, "combat_step_share": 0.0, "per_episode": []}
        verdicts = V.evaluate_phase_share(artifact, True, [])
        self.assertFalse(verdicts["recorded_total_steps_matches_the_rows"])
        self.assertFalse(verdicts["trained_actions_are_all_out_of_combat"])


FROZEN = {
    "episodes": 2,
    "combat_executor": "frozen",
    "checkpoint_sha256": "a" * 64,
    "total_steps": 40,
    "absorbed_combat_steps": 160,
    "executor_step_capped_transitions": 0,
    "per_episode": [{"seed": 1, "steps": 20, "phases": {"map": 20}},
                    {"seed": 2, "steps": 20, "phases": {"map": 20}}],
}
PRE_G1 = {
    "episodes": 2,
    "combat_executor": "agent",
    "checkpoint_sha256": "a" * 64,
    "total_steps": 200,
    "combat_step_share": 0.8,
    "per_episode": [{"seed": 1, "steps": 100, "phases": {"combat": 80, "map": 20}},
                    {"seed": 2, "steps": 100, "phases": {"combat": 80, "map": 20}}],
}


class PhaseActionPairTests(unittest.TestCase):
    """The two arms have to differ in exactly one thing: who plays combat.

    A frozen arm on its own can always be made to look aligned by deleting fights, changing seeds,
    or rerunning against other weights.  Read against the pre-G1 artifact -- same checkpoint, same
    seeds, same episode budget -- the only surviving explanation for ``combat == 0`` is that the
    transitions moved rather than vanished.
    """

    def test_the_clean_pair_holds(self) -> None:
        verdicts = V.evaluate_phase_pair(dict(FROZEN), dict(PRE_G1))
        self.assertTrue(all(verdicts.values()), str(verdicts))

    def test_an_absorbed_count_of_zero_is_not_alignment(self) -> None:
        frozen = {**FROZEN, "absorbed_combat_steps": 0}
        verdicts = V.evaluate_phase_pair(frozen, dict(PRE_G1))
        self.assertFalse(verdicts["absorbed_combat_carries_the_difference"])

    def test_a_wrapper_that_also_changed_the_fights_does_not_pass(self) -> None:
        # Fewer absorbed steps than the pre-G1 arm spent in combat means the battles themselves
        # went differently, so the two streams are not one variable apart.
        frozen = {**FROZEN, "absorbed_combat_steps": 159}
        verdicts = V.evaluate_phase_pair(frozen, dict(PRE_G1))
        self.assertFalse(verdicts["absorbed_combat_carries_the_difference"])

    def test_different_seed_lists_break_the_pairing(self) -> None:
        frozen = {**FROZEN, "per_episode": [{"seed": 7, "steps": 20, "phases": {"map": 20}},
                                              {"seed": 8, "steps": 20, "phases": {"map": 20}}]}
        verdicts = V.evaluate_phase_pair(frozen, dict(PRE_G1))
        self.assertFalse(verdicts["the_two_arms_ran_on_the_same_seeds"])

    def test_a_second_checkpoint_breaks_the_pairing(self) -> None:
        frozen = {**FROZEN, "checkpoint_sha256": "b" * 64}
        verdicts = V.evaluate_phase_pair(frozen, dict(PRE_G1))
        self.assertFalse(verdicts["the_two_arms_share_one_frozen_checkpoint"])

    def test_a_rewritten_pre_g1_deficit_is_caught(self) -> None:
        pre = {**PRE_G1, "combat_step_share": 0.05}
        verdicts = V.evaluate_phase_pair(dict(FROZEN), pre)
        self.assertFalse(verdicts["the_pre_g1_deficit_still_recomputes"])

    def test_a_missing_frozen_executor_declaration_is_caught(self) -> None:
        frozen = {**FROZEN, "combat_executor": "agent"}
        verdicts = V.evaluate_phase_pair(frozen, dict(PRE_G1))
        self.assertFalse(verdicts["the_frozen_arm_is_the_one_declaring_a_frozen_executor"])

    def test_an_executor_that_had_to_be_stopped_is_not_a_clean_run(self) -> None:
        frozen = {**FROZEN, "executor_step_capped_transitions": 1}
        verdicts = V.evaluate_phase_pair(frozen, dict(PRE_G1))
        self.assertFalse(verdicts["no_transition_had_to_be_capped_by_the_executor"])

    def test_a_shifted_out_of_combat_stream_is_caught(self) -> None:
        frozen = {**FROZEN,
                  "per_episode": [{"seed": 1, "steps": 21, "phases": {"map": 21}},
                                  {"seed": 2, "steps": 19, "phases": {"event": 19}}]}
        verdicts = V.evaluate_phase_pair(frozen, dict(PRE_G1))
        self.assertFalse(verdicts["the_out_of_combat_stream_is_unchanged"])


LIVE_VICTORY_RUN = {
    "acts": {"1": {"min_floor": 1, "max_floor": 17, "first_ts": "2026-10-07T08:14:05+00:00"},
             "2": {"min_floor": 18, "max_floor": 33, "first_ts": "2026-10-07T08:20:03+00:00"},
             "3": {"min_floor": 34, "max_floor": 49, "first_ts": "2026-10-07T08:25:40+00:00"}},
    "ancients": [{"event_id": "NEOW", "act": 1, "expected_act": 1},
                 {"event_id": "PAEL", "act": 2, "expected_act": 2},
                 {"event_id": "VAKUU", "act": 3, "expected_act": 3}],
    "boss_nodes": [{"act": 1, "floor": 17, "entities": ["LAGAVULIN_MATRIARCH_0"]},
                   {"act": 2, "floor": 33, "entities": ["CRUSHER_0", "ROCKET_0"]},
                   {"act": 3, "floor": 48, "entities": ["AEONGLASS_0"]},
                   {"act": 3, "floor": 49, "entities": ["TEST_SUBJECT_0"]}],
    "victory_flag": True,
    "hp_at_game_over": 0,
}
LIVE_VICTORY = {
    "victories": 1,
    "completed_runs": 7,
    "execution_owner": "combat_solver_full_auto",
    "live_choice_policy_version": "conservative-visible-v2",
    "acceptance": {"available": True, "acceptance_claim": False,
                   "acceptance_blockers": ["observational_mode"],
                   "solver_at_end": {"actual_sha256": "8" * 64, "matches_lock": False}},
    "victory_run": LIVE_VICTORY_RUN,
}


class LiveVictoryShapeTests(unittest.TestCase):
    """The delivery goal is a shape, so the checks that define it have to be breakable.

    Each case here corresponds to a way the trace could be read generously instead of exactly:
    quoting one win as a rate, calling an act-3 double boss a single one, or letting the act-2 pair
    collapse to whoever was still alive in the last frame -- which is what the extractor first did,
    and why that case is pinned.
    """

    def test_the_recorded_shape_holds(self) -> None:
        verdicts = V.evaluate_live_victory(dict(LIVE_VICTORY))
        self.assertTrue(all(verdicts.values()), str(verdicts))

    def test_a_lone_win_quoted_as_its_own_denominator_fails(self) -> None:
        artifact = {**LIVE_VICTORY, "completed_runs": 1}
        self.assertFalse(V.evaluate_live_victory(artifact)[
            "the_victory_is_reported_with_its_denominator"])

    def test_a_missing_ending_hp_is_not_an_improvement(self) -> None:
        run = {k: v for k, v in LIVE_VICTORY_RUN.items() if k != "hp_at_game_over"}
        self.assertFalse(V.evaluate_live_victory({**LIVE_VICTORY, "victory_run": run})[
            "the_ending_hp_is_disclosed"])

    def test_an_act_2_boss_reduced_to_its_survivor_fails(self) -> None:
        run = dict(LIVE_VICTORY_RUN)
        run["boss_nodes"] = [dict(node, entities=(["CRUSHER_0"] if node["act"] == 2
                                                   else node["entities"]))
                             for node in LIVE_VICTORY_RUN["boss_nodes"]]
        self.assertFalse(V.evaluate_live_victory({**LIVE_VICTORY, "victory_run": run})[
            "the_act_2_boss_was_a_pair_fought_together"])

    def test_a_final_act_with_one_boss_encounter_fails(self) -> None:
        nodes = LIVE_VICTORY_RUN["boss_nodes"]
        run = dict(LIVE_VICTORY_RUN, boss_nodes=nodes[:1] + nodes[1:2] + nodes[2:3])
        verdicts = V.evaluate_live_victory({**LIVE_VICTORY, "victory_run": run})
        self.assertFalse(verdicts["the_final_act_fought_two_separate_boss_encounters"])

    def test_an_ancient_attributed_to_the_wrong_act_fails(self) -> None:
        run = dict(LIVE_VICTORY_RUN)
        run["ancients"] = [dict(a, expected_act=(2 if a["event_id"] == "NEOW"
                                                 else a["expected_act"]))
                           for a in LIVE_VICTORY_RUN["ancients"]]
        self.assertFalse(V.evaluate_live_victory({**LIVE_VICTORY, "victory_run": run})[
            "no_ancient_is_claimed_for_the_wrong_act"])

    def test_a_loss_cannot_be_recorded_as_the_goal(self) -> None:
        run = dict(LIVE_VICTORY_RUN, victory_flag=False)
        self.assertFalse(V.evaluate_live_victory({**LIVE_VICTORY, "victory_run": run})[
            "the_client_itself_flagged_the_terminal_state_as_victory"])

    def test_posting_combat_actions_ourselves_breaks_the_claim(self) -> None:
        artifact = {**LIVE_VICTORY, "execution_owner": "advisor_replay"}
        self.assertFalse(V.evaluate_live_victory(artifact)[
            "combat_was_owned_by_the_solver_not_by_us"])

    def test_an_unversioned_policy_cannot_take_credit(self) -> None:
        artifact = {**LIVE_VICTORY, "live_choice_policy_version": None}
        self.assertFalse(V.evaluate_live_victory(artifact)[
            "the_deciding_policy_version_is_recorded"])

    def test_a_victory_without_its_batch_verdict_is_incomplete(self) -> None:
        artifact = {**LIVE_VICTORY, "acceptance": {"available": True,
                                                    "acceptance_claim": False}}
        self.assertFalse(V.evaluate_live_victory(artifact)[
            "the_batch_verdict_and_its_blockers_are_recorded"])

    def test_a_solver_named_only_by_version_string_is_not_identified(self) -> None:
        acceptance = dict(LIVE_VICTORY["acceptance"],
                          solver_at_end={"mod_manifest_version": "0.50.1"})
        self.assertFalse(V.evaluate_live_victory({**LIVE_VICTORY, "acceptance": acceptance})[
            "the_solver_build_is_identified_by_hash"])


if __name__ == "__main__":
    unittest.main()
