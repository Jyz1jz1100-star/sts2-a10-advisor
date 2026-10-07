"""Tests for scripts/train_dashboard.py: the log reader, its derived numbers, its gate read.

The panel exists to be trusted when nobody is looking at the terminal, so these tests aim at the
ways a monitor lies: dropping a field it does not recognise, turning an unreadable file into a
zero, reporting an ETA it did not measure as a logged estimate, and scoring a gate clause against
a metric the artifact never carried.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT))

import train_dashboard as D  # noqa: E402
from training.config import PromotionConfig  # noqa: E402

SAMPLE = """------------------------------
| time/              |       |
|    fps             | 91    |
|    iterations      | 1     |
|    time_elapsed    | 269   |
|    total_timesteps | 24576 |
------------------------------
-----------------------------------------
| time/                   |             |
|    fps                  | 88          |
|    iterations           | 2           |
|    time_elapsed         | 554         |
|    total_timesteps      | 49152       |
| train/                  |             |
|    approx_kl            | 0.008354948 |
|    clip_fraction        | 0.0756      |
|    entropy_loss         | -0.839      |
|    value_loss           | 110         |
-----------------------------------------
"""


def metrics_payload(**over) -> dict:
    payload = {
        "schema_version": 7, "generated_at": "2026-10-07T16:27:19+00:00",
        "stage": "full_run", "split": "checkpoint", "scope": "simulator_full_run",
        "experimental": False, "checkpoint": "x.zip", "checkpoint_sha256": "abc",
        "deterministic": True, "seed_count": 200, "seed_sha256": "def",
        "episodes": 200, "wins": 120, "win_rate": 0.6,
        "wilson_95_low": 0.53, "wilson_95_high": 0.67,
        "truncations": 0, "truncation_rate": 0.0, "defect_truncation_rate": 0.0,
        "illegal_actions": 0, "mean_steps": 30.0, "mean_return": 12.0,
        "mean_final_floor": 8.1, "max_final_floor": 17,
        "boundary_rate": 1.0, "boundary_wilson_95_low": 0.98, "boundary_hits": 200,
        "unclassified_dead_ends": 0, "dead_end_reasons": {},
    }
    payload.update(over)
    return payload


class ConsoleDumpTests(unittest.TestCase):
    def test_blocks_are_kept_per_iteration_with_typed_values(self) -> None:
        parsed = D.parse_console_dump(SAMPLE)
        self.assertEqual(len(parsed["blocks"]), 2)
        self.assertEqual(parsed["rows_skipped"], 0)
        first, second = parsed["blocks"]
        self.assertEqual(first["time"]["total_timesteps"], 24576)
        self.assertIsInstance(first["time"]["total_timesteps"], int)
        self.assertEqual(second["time"]["time_elapsed"], 554)
        self.assertEqual(second["train"]["approx_kl"], 0.008354948)
        self.assertEqual(second["train"]["entropy_loss"], -0.839)

    def test_unknown_sections_survive_rather_than_being_dropped(self) -> None:
        text = SAMPLE + "------------------------------\n| custom/ |\n" \
                        "|    absorbed_combat_steps | 6364 |\n" \
                        "------------------------------\n"
        parsed = D.parse_console_dump(text)
        self.assertEqual(len(parsed["blocks"]), 3)
        self.assertEqual(parsed["blocks"][-1]["custom"]["absorbed_combat_steps"], 6364)
        self.assertEqual(parsed["rows_skipped"], 0)

    def test_non_numeric_value_is_kept_as_text(self) -> None:
        parsed = D.parse_console_dump(
            "-----------\n| custom/ |\n|    scope | simulator_full_run |\n-----------\n")
        self.assertEqual(parsed["blocks"][0]["custom"]["scope"], "simulator_full_run")

    def test_partial_trailing_block_is_reported_not_half_read(self) -> None:
        parsed = D.parse_console_dump(SAMPLE + "| time/ |  |\n|    fps | 93 |\n")
        self.assertEqual(len(parsed["blocks"]), 2)
        self.assertTrue(parsed["trailing_block_open"])

    def test_malformed_row_is_counted_rather_than_silently_ignored(self) -> None:
        parsed = D.parse_console_dump("-----------\n| a | b | c |\n-----------\n")
        self.assertEqual(parsed["rows_skipped"], 1)
        self.assertEqual(parsed["blocks"], [])

    def test_only_blocks_naming_a_timestep_count_become_progress_points(self) -> None:
        blocks = [{"time": {"fps": 90}}, {"time": {"total_timesteps": 100, "fps": 91}}]
        series = D.series_from_blocks(blocks)
        self.assertEqual(len(series), 1)
        self.assertEqual(series[0]["total_timesteps"], 100)


class ProgressTests(unittest.TestCase):
    @staticmethod
    def _points(pairs: list[tuple[int, int, int]]) -> list[dict]:
        return [{"iterations": i, "total_timesteps": t, "time_elapsed": e, "fps": f}
                for i, t, e, f in pairs]

    def test_position_and_eta_are_derived_from_the_log_s_own_fields(self) -> None:
        points = self._points([(20, 491520, 5134, 95), (21, 516096, 5400, 95)])
        out = D.progress(points, {"timesteps": 1500000, "checkpoint_every_steps": 500000})
        self.assertEqual(out["total_timesteps"], 516096)
        self.assertAlmostEqual(out["percent_of_target"], 34.41)
        self.assertAlmostEqual(out["fps_mean_stated_run"], round(516096 / 5400, 2))
        self.assertEqual(out["remaining_steps"], 1500000 - 516096)
        self.assertEqual(out["next_checkpoint_at"], 1000000)
        self.assertTrue(any("extrapolation" in note for note in out["notes"]), out["notes"])

    def test_steps_per_iteration_is_read_not_assumed(self) -> None:
        points = self._points([(21, 516096, 5400, 95)])
        out = D.progress(points, {"timesteps": 1500000, "n_steps": 2048, "parallel_envs": 12})
        self.assertEqual(out["steps_per_iteration"], 24576)
        self.assertEqual(out["iterations_logged"], 21)
        without = D.progress(points, {"timesteps": 1500000})
        self.assertIsNone(without["steps_per_iteration"])

    def test_missing_target_leaves_percent_undrawn_instead_of_zero(self) -> None:
        out = D.progress(self._points([(1, 24576, 269, 91)]), {})
        self.assertIsNone(out["target_timesteps"])
        self.assertIsNone(out["percent_of_target"])
        self.assertIsNone(out["eta_s_from_mean_fps"])

    def test_no_points_yields_no_zeros(self) -> None:
        out = D.progress([], {"timesteps": 1500000})
        for key in ("total_timesteps", "percent_of_target", "fps_last", "remaining_steps",
                    "eta_s_from_mean_fps", "next_checkpoint_at"):
            self.assertIsNone(out[key], key)
        self.assertTrue(out["notes"])

    def test_no_next_checkpoint_is_announced_once_the_ceiling_is_reached(self) -> None:
        out = D.progress(self._points([(59, 1465920, 15000, 95),
                                       (60, 1500000, 15400, 95)]),
                         {"timesteps": 1500000, "checkpoint_every_steps": 500000})
        self.assertIsNone(out["next_checkpoint_at"])
        self.assertEqual(out["remaining_steps"], 0)

    def test_recent_rate_needs_two_stamps_before_it_claims_one(self) -> None:
        out = D.progress(self._points([(1, 24576, 269, 91)]), {"timesteps": 1500000})
        self.assertIsNone(out["fps_recent"])


class LivenessTests(unittest.TestCase):
    def test_median_period_and_stale_threshold_come_from_the_log(self) -> None:
        points = D.series_from_blocks(
            [{"time": {"total_timesteps": i * 24576, "time_elapsed": i * 210, "fps": 95}}
             for i in range(1, 6)])
        self.assertEqual(D.iteration_period(points), 210)

    def test_writing_and_stale_are_split_on_the_measured_period(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "arm.log"
            log.write_text(SAMPLE, encoding="utf-8")
            points = D.series_from_blocks(D.parse_console_dump(SAMPLE)["blocks"])
            now = log.stat().st_mtime
            fresh = D.liveness(log, points, now + 60)
            self.assertEqual(fresh["state"], "writing")
            stale = D.liveness(log, points, now + fresh["stale_threshold_s"] + 1)
            self.assertEqual(stale["state"], "stale")
            self.assertIn("log silent", stale["reason"])

    def test_a_first_iteration_cannot_be_called_stale_by_an_unmeasurable_period(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "arm.log"
            log.write_text(SAMPLE.replace("|    time_elapsed    | 269   |", "|    x | 1 |"),
                           encoding="utf-8")
            out = D.liveness(log, [], log.stat().st_mtime + 400)
            self.assertEqual(out["stale_threshold_s"], 900.0)
            self.assertEqual(out["state"], "writing")
            self.assertIn("fallback", out["threshold_basis"])
            self.assertIsNone(out["median_iteration_period_s"])

    def test_no_log_path_is_unknown_rather_than_stalled(self) -> None:
        out = D.liveness(None, [], time.time())
        self.assertEqual(out["state"], "unknown")


class ConfigAndDiscoveryTests(unittest.TestCase):
    TOML = """version = 1
[target]
character = "IRONCLAD"
ascension = 10
[runtime]
run_dir = "runtime/whatever"
[curriculum]
stages = ["full_run"]
[algorithm]
n_steps = 2048
[stages.full_run]
timesteps = 1500000
parallel_envs = 12
checkpoint_every_steps = 500000
combat_executor = "frozen"
combat_executor_checkpoint = "../runtime/x/step_1.zip"
min_episodes = 500
min_win_rate = 0.20
max_illegal_actions = 0
"""

    def test_stage_table_is_read_from_the_named_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "arm.toml"
            path.write_text(self.TOML, encoding="utf-8")
            out = D.load_stage_config(path)
            self.assertTrue(out["readable"])
            self.assertEqual(out["stage_name"], "full_run")
            self.assertEqual(out["timesteps"], 1500000)
            self.assertEqual(out["combat_executor"], "frozen")
            self.assertEqual(out["n_steps"], 2048)
            self.assertEqual(out["promotion"]["min_win_rate"], 0.2)
            self.assertEqual(out["run_dir"], "runtime/whatever")

    def test_unreadable_config_says_so_instead_of_reading_as_unconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "arm.toml"
            path.write_text("this is not = toml =", encoding="utf-8")
            out = D.load_stage_config(path)
            self.assertFalse(out["readable"])
            self.assertIn("TOMLDecodeError", out["detail"])

    def test_run_root_selection_prefers_the_newest_id_and_names_what_it_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "runtime" / "arm"
            for name in ("v2curriculum-20260923T061352Z", "v2curriculum-20261007T145934Z"):
                (base / name).mkdir(parents=True)
                (base / name / "plan.json").write_text("{}", encoding="utf-8")
            (base / "notarun").mkdir()
            out = D.find_run_root(None, {"run_dir": str(base)})
            self.assertTrue(out[0].name.endswith("20261007T145934Z"))
            self.assertIn("newest of 2", out[1])
            self.assertIn("20260923T061352Z", out[1])

    def test_missing_run_dir_is_a_located_absence_not_a_none_quietly_passed_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = D.find_run_root(None, {"run_dir": str(Path(tmp) / "arm")})
            self.assertIsNone(out[0])
            self.assertIn("does not exist", out[1])


class GateScoringTests(unittest.TestCase):
    def test_verdict_is_produced_by_the_promotion_code_not_a_local_reimplementation(self) -> None:
        requirements = PromotionConfig(min_episodes=500, min_win_rate=0.20,
                                       min_wilson_lower=0.18, max_truncation_rate=0.05,
                                       max_illegal_actions=0, min_boundary_rate=0.0)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "step_000000500004.json"
            path.write_text(json.dumps(metrics_payload(wins=0, win_rate=0.0,
                                                       wilson_95_low=0.0)),
                           encoding="utf-8")
            row = D.score_metrics(path, requirements)
            self.assertEqual(row["status"], "scored")
            self.assertFalse(row["promoted"])
            self.assertTrue(any("win_rate" in reason for reason in row["reasons"]))
            self.assertTrue(any("episodes 200 < required 500" in r for r in row["reasons"]))

    def test_a_clause_without_its_metric_is_reported_as_unscoreable_not_as_zero(self) -> None:
        payload = metrics_payload()
        del payload["boundary_rate"]
        del payload["defect_truncation_rate"]
        requirements = PromotionConfig(min_episodes=500, min_win_rate=0.20,
                                       min_wilson_lower=0.18, max_truncation_rate=0.05,
                                       max_illegal_actions=0, min_boundary_rate=0.9)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            row = D.score_metrics(path, requirements)
            self.assertFalse(row["promoted"])
            joined = " ".join(row["reasons"])
            self.assertIn("was not recorded", joined)
            self.assertIn("boundary_rate", joined)
            self.assertIn("boundary_rate", row["keys_absent_from_file"])

    def test_a_passing_evaluation_promotes_through_the_same_call(self) -> None:
        requirements = PromotionConfig(min_episodes=200, min_win_rate=0.20,
                                       min_wilson_lower=0.18, max_truncation_rate=0.05,
                                       max_illegal_actions=0, min_boundary_rate=0.0)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.json"
            path.write_text(json.dumps(metrics_payload()), encoding="utf-8")
            row = D.score_metrics(path, requirements)
            self.assertTrue(row["promoted"], row["reasons"])

    def test_a_corrupt_payload_is_a_finding_not_a_crash(self) -> None:
        requirements = PromotionConfig(500, 0.20, 0.18, 0.05, 0)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.json"
            path.write_text("{\"schema_version\": 7}", encoding="utf-8")
            row = D.score_metrics(path, requirements)
            self.assertEqual(row["status"], "not-scoreable")
            self.assertIn("TypeError", row["detail"])


class StageScanTests(unittest.TestCase):
    def test_checkpoint_without_metrics_is_named_as_an_inference(self) -> None:
        requirements = PromotionConfig(500, 0.20, 0.18, 0.05, 0)
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "full_run"
            (stage / "checkpoints").mkdir(parents=True)
            (stage / "checkpoints" / "step_000000500004.zip").write_bytes(b"zip")
            out = D.scan_stage_dir(stage, requirements)
            self.assertEqual(len(out["checkpoints"]), 1)
            self.assertEqual(out["metrics"], [])
            self.assertTrue(any("in progress" in line for line in out["inference"]))

    def test_missing_stage_directory_is_located(self) -> None:
        out = D.scan_stage_dir(Path(tempfile.gettempdir()) / "no-such-stage-xyz",
                               PromotionConfig(500, 0.2, 0.18, 0.05, 0))
        self.assertFalse(out["exists"])
        self.assertTrue(out["inference"])


class IdentityTests(unittest.TestCase):
    def test_a_plan_that_omits_the_executor_says_it_cannot_prove_the_arm(self) -> None:
        plan = {"character": "IRONCLAD", "stages": [{"scope": "simulator_full_run"}]}
        out = D.arm_identity(plan, None, {"combat_executor": "frozen", "path": "c.toml",
                                         "combat_executor_checkpoint": "../x/step.zip"})
        self.assertFalse(out["plan_records_executor"])
        self.assertTrue(any("cannot prove it is the G1 arm" in w for w in out["warnings"]))

    def test_a_plan_that_records_the_executor_raises_no_warning(self) -> None:
        plan = {"stages": [{"combat_executor": "frozen"}]}
        out = D.arm_identity(plan, None, {"combat_executor": "frozen"})
        self.assertTrue(out["plan_records_executor"])
        self.assertEqual(out["warnings"], [])

    def test_unreadable_plan_falls_back_to_the_config_and_says_so(self) -> None:
        out = D.arm_identity(None, "plan.json: no such file", {"combat_executor": "frozen"})
        self.assertTrue(any("plan.json unread" in w for w in out["warnings"]))


class BuildStateTests(unittest.TestCase):
    def test_empty_log_produces_no_invented_position(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "arm.log"
            log.write_text("", encoding="utf-8")
            state = D.build_state(None, "not resolved", log, {"stage_name": "full_run"},
                                  None, "no run root resolved", {"state": "ok", "matches": []},
                                  time.time())
            self.assertEqual(state["log"]["blocks_parsed"], 0)
            self.assertIsNone(state["progress"]["total_timesteps"])
            self.assertEqual(state["series"], [])
            self.assertTrue(state["blind_spots"])
            self.assertIn("no run root", state["stage"]["inference"][0])


class RoutingTests(unittest.TestCase):
    def test_a_stray_suffix_on_the_page_url_still_serves_the_panel(self) -> None:
        # A markdown-wrapped link hands the browser "/**"; a monitor answering 404 to that reads
        # as "the batch died", which is a claim about the run and not true.
        for path in ("/", "/index.html", "/**", "/api/state", "/api/state?x=1", "/api/state/"):
            self.assertNotEqual(D.route_for(path), "missing", path)
        self.assertEqual(D.route_for("/**"), "page")
        self.assertEqual(D.route_for("/api/state?x=1"), "state")

    def test_the_api_namespace_can_still_say_no_such_endpoint(self) -> None:
        for path in ("/api", "/api/nothing", "/api/state/extra"):
            self.assertEqual(D.route_for(path), "missing", path)


class EmbeddedScriptTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is not on PATH")
    def test_the_page_script_parses(self) -> None:
        """A syntax error in the embedded page leaves the panel blank with a 200 OK and a healthy
        /api/state, so no Python test and no HTTP probe can see it -- parse it with node instead."""
        script = D.HTML.split("<script>", 1)[1].split("</script>", 1)[0]
        self.assertTrue(script.strip())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "page.js"
            path.write_text(script, encoding="utf-8")
            done = subprocess.run([shutil.which("node"), "--check", str(path)],
                                  capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
