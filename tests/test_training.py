from __future__ import annotations

import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from training.config import PromotionConfig, load_training_config
from training.evaluation import evaluate_policy
from training.campaign_content import (
    CAMPAIGN_CONTENT_COVERAGE,
    CAMPAIGN_ENVIRONMENT_VERSION,
    RESULT_TIERS,
)
from training.metrics import EpisodeMetric, atomic_write_json, summarize_episodes
from training.promotion import decide_promotion
from training.seeds import SeedPartition, SeedPartitions, SeedStream


class SeedTests(unittest.TestCase):
    def test_partitions_reject_overlap(self) -> None:
        with self.assertRaises(ValueError):
            SeedPartitions(
                [
                    SeedPartition("train", 0, 100),
                    SeedPartition("evaluation", 99, 10),
                ]
            )

    def test_worker_shards_are_disjoint(self) -> None:
        partition = SeedPartition("train", 1000, 101)
        shards = [partition.shard(index, 4) for index in range(4)]
        observed = [set(shard.seeds()) for shard in shards]
        self.assertEqual(sum(len(seeds) for seeds in observed), 101)
        self.assertEqual(len(set.union(*observed)), 101)
        for left_index, left in enumerate(observed):
            for right in observed[left_index + 1 :]:
                self.assertTrue(left.isdisjoint(right))

    def test_seed_stream_is_reproducible_without_replacement(self) -> None:
        partition = SeedPartition("train", 50, 17)
        left = SeedStream(partition, "combat:worker:0")
        right = SeedStream(partition, "combat:worker:0")
        left_seeds = [left.next() for _ in range(partition.count)]
        right_seeds = [right.next() for _ in range(partition.count)]
        self.assertEqual(left_seeds, right_seeds)
        self.assertEqual(len(set(left_seeds)), partition.count)
        with self.assertRaises(RuntimeError):
            left.next()


class MetricsTests(unittest.TestCase):
    def _metrics(self, wins: int, games: int, *, final_floor: int = 12):
        episodes = [
            EpisodeMetric(
                seed=30_000_000 + index,
                won=index < wins,
                terminated=True,
                truncated=False,
                steps=20,
                episode_return=1.0 if index < wins else -1.0,
                illegal_actions=0,
                final_floor=final_floor,
            )
            for index in range(games)
        ]
        return summarize_episodes(
            episodes,
            stage="act1",
            split="promotion",
            scope="simulator_act1",
            checkpoint="checkpoint.zip",
            deterministic=True,
        )

    def test_histogram_separates_shallow_mean_from_bimodal_depth(self) -> None:
        # The reason the field exists: these two shapes share a mean and a max, so
        # without the distribution nobody can say whether the Act 1 wall is mid-run
        # attrition or the boss fight.
        bimodal = [
            EpisodeMetric(seed=40_000_000 + index, won=False, terminated=True,
                          truncated=False, steps=20, episode_return=-1.0,
                          illegal_actions=0, final_floor=floor)
            for index, floor in enumerate([6, 6, 16, 16])
        ]
        metrics = summarize_episodes(
            bimodal, stage="act1", split="promotion", scope="simulator_act1",
            checkpoint="c.zip", deterministic=True,
        )
        self.assertEqual(metrics.mean_final_floor, 11.0)
        self.assertEqual(metrics.max_final_floor, 16)
        self.assertEqual(metrics.final_floor_histogram, {6: 2, 16: 2})

    def test_episodes_without_a_floor_leave_the_histogram_absent_not_zero(self) -> None:
        # An empty histogram means "no episode reported a floor", never "zero runs
        # died on every floor"; the two must not be conflated downstream.
        episodes = [
            EpisodeMetric(seed=41_000_000, won=False, terminated=True, truncated=True,
                          steps=99, episode_return=0.0, illegal_actions=0,
                          final_floor=None)
        ]
        metrics = summarize_episodes(
            episodes, stage="act1", split="promotion", scope="simulator_act1",
            checkpoint="c.zip", deterministic=True,
        )
        self.assertEqual(metrics.final_floor_histogram, {})
        self.assertIsNone(metrics.mean_final_floor)
        self.assertIsNone(metrics.max_final_floor)

    def test_by_act_splits_the_mixed_act_population(self) -> None:
        # An act1-stage evaluation spans both emulator acts (the generator picks the
        # act per seed), so the aggregate alone cannot say which act a win happened
        # in. by_act is what makes the claim attributable.
        episodes = [
            EpisodeMetric(seed=42_000_000 + index, won=index == 0, terminated=True,
                          truncated=False, steps=20,
                          episode_return=1.0 if index == 0 else -1.0,
                          illegal_actions=0, final_floor=floor, act=act)
            for index, (act, floor) in enumerate([(1, 17), (1, 6), (2, 9), (2, 11)])
        ]
        metrics = summarize_episodes(
            episodes, stage="act1", split="promotion", scope="simulator_act1",
            checkpoint="c.zip", deterministic=True,
        )
        self.assertEqual(metrics.by_act["1"]["episodes"], 2)
        self.assertEqual(metrics.by_act["1"]["wins"], 1)
        self.assertEqual(metrics.by_act["1"]["max_final_floor"], 17)
        self.assertEqual(metrics.by_act["2"]["win_rate"], 0.0)
        self.assertEqual(metrics.by_act["2"]["mean_final_floor"], 10.0)
        # the aggregate cannot name a win; these lists can
        self.assertEqual(metrics.winning_seeds, [episodes[0].seed])
        self.assertEqual(metrics.by_act["1"]["winning_seeds"], [episodes[0].seed])
        self.assertEqual(metrics.by_act["2"]["winning_seeds"], [])
        self.assertEqual(metrics.win_rate, 0.25)  # the mixed number says nothing of this

    def test_by_act_is_absent_rather_than_zero_when_act_is_unknown(self) -> None:
        episodes = [
            EpisodeMetric(seed=43_000_000, won=True, terminated=True, truncated=False,
                          steps=10, episode_return=1.0, illegal_actions=0,
                          final_floor=17, act=None)
        ]
        metrics = summarize_episodes(
            episodes, stage="act1", split="promotion", scope="simulator_act1",
            checkpoint="c.zip", deterministic=True,
        )
        self.assertEqual(metrics.by_act, {})

    def test_promotion_requires_all_gates(self) -> None:
        requirements = PromotionConfig(
            min_episodes=200,
            min_win_rate=0.50,
            min_wilson_lower=0.40,
            max_truncation_rate=0.01,
            max_illegal_actions=0,
            min_mean_floor=10.0,
        )
        passed = decide_promotion(self._metrics(120, 200), requirements)
        failed = decide_promotion(self._metrics(80, 200), requirements)
        self.assertTrue(passed.promoted)
        self.assertFalse(failed.promoted)
        self.assertTrue(any("win_rate" in reason for reason in failed.reasons))

    def test_metrics_include_seed_digest_and_wilson_interval(self) -> None:
        metrics = self._metrics(100, 200)
        # Deliberately a literal, not METRICS_SCHEMA_VERSION: bumping the schema
        # has to be a decision someone makes here. 4 adds final_floor_histogram,
        # 5 adds by_act (an act1 stage spans both emulator acts per seed), 6 adds
        # winning_seeds so a single reviewable win can be named and re-run.
        self.assertEqual(metrics.schema_version, 6)

    def test_boundary_wilson_and_hp_metrics(self) -> None:
        """Review item 4: boundary Wilson lower bound + final HP fraction."""

        episodes = [
            EpisodeMetric(
                seed=50_000_000 + index,
                won=False,
                terminated=False,
                truncated=True,
                steps=30,
                episode_return=0.5,
                illegal_actions=0,
                final_floor=6,
                boundary_reached=index < 465,  # 465/500 boundary hits
                dead_end_reason=(None if index < 465 else "step_cap"),
                final_hp_fraction=0.5,
            )
            for index in range(500)
        ]
        metrics = summarize_episodes(
            episodes,
            stage="floor6",
            split="promotion",
            scope="simulator_act1",
            checkpoint="fixture.zip",
            deterministic=True,
        )
        self.assertEqual(metrics.boundary_hits, 465)
        self.assertAlmostEqual(metrics.boundary_rate, 0.93)
        # Wilson lower of 465/500 must clear 0.90 but not 0.91.
        self.assertGreaterEqual(metrics.boundary_wilson_95_low, 0.90)
        self.assertAlmostEqual(metrics.mean_final_hp_fraction, 0.5)
        # The gate: a 0.92 point pass fails the Wilson>=0.90 requirement.
        from training.wilson import wilson_interval

        low_460, _ = wilson_interval(460, 500)
        self.assertLess(low_460, 0.90)
        self.assertGreaterEqual(metrics.boundary_wilson_95_low, low_460)

    def test_boundary_wilson_gate_rejects_thin_margin(self) -> None:
        metrics = self._metrics(120, 200, final_floor=6)
        requirements = PromotionConfig(
            min_episodes=200,
            min_win_rate=0.0,
            min_wilson_lower=0.0,
            max_truncation_rate=1.0,
            max_illegal_actions=0,
            min_boundary_rate=0.0,
            min_boundary_wilson_lower=0.65,
        )
        decision = decide_promotion(metrics, requirements)
        self.assertFalse(decision.promoted)
        self.assertTrue(
            any("boundary_wilson_95_low" in reason for reason in decision.reasons)
        )

    def test_boundary_wilson_gate_passes_strong_boundary(self) -> None:
        # 465/500 boundary hits: point 0.93, Wilson low ~0.9045.
        episodes = [
            EpisodeMetric(
                seed=51_000_000 + index,
                won=False,
                terminated=False,
                truncated=True,
                steps=10,
                episode_return=1.0,
                illegal_actions=0,
                final_floor=6,
                boundary_reached=index < 465,
                dead_end_reason=(None if index < 465 else "step_cap"),
            )
            for index in range(500)
        ]
        metrics = summarize_episodes(
            episodes,
            stage="floor6",
            split="promotion",
            scope="simulator_act1",
            checkpoint="fixture.zip",
            deterministic=True,
        )
        requirements = PromotionConfig(
            min_episodes=500,
            min_win_rate=0.0,
            min_wilson_lower=0.0,
            max_truncation_rate=1.0,
            max_illegal_actions=0,
            min_boundary_rate=0.93,
            min_boundary_wilson_lower=0.90,
        )
        decision = decide_promotion(metrics, requirements)
        self.assertTrue(decision.promoted, decision.reasons)
        self.assertGreaterEqual(
            decision.observed["boundary_wilson_95_low"], 0.90
        )

    def test_defect_truncation_rate_never_goes_negative_with_terminal_wins(self) -> None:
        """A won episode also sets boundary_reached; the defect rate counts
        only *truncated* non-boundary endings, so it must not subtract wins."""

        episodes = [
            EpisodeMetric(
                seed=40_000_000 + index,
                won=index < 5,  # terminal wins: boundary_reached without truncation
                terminated=index < 5,
                truncated=index >= 5,
                steps=10,
                episode_return=1.0,
                illegal_actions=0,
                final_floor=16,
                boundary_reached=index < 8,  # 5 wins + 3 boundary truncations
                dead_end_reason=(None if index < 8 else "native_rejection"),
            )
            for index in range(10)
        ]
        metrics = summarize_episodes(
            episodes,
            stage="act1",
            split="promotion",
            scope="simulator_act1",
            checkpoint="fixture.zip",
            deterministic=True,
        )
        self.assertEqual(metrics.truncations, 5)
        self.assertEqual(metrics.boundary_rate, 0.8)
        self.assertEqual(metrics.defect_truncation_rate, 0.2)
        self.assertGreaterEqual(metrics.defect_truncation_rate, 0.0)
        self.assertEqual(metrics.dead_end_reasons, {"native_rejection": 2})
        self.assertEqual(metrics.unclassified_dead_ends, 0)
        self.assertEqual(len(metrics.seed_sha256), 64)
        self.assertLess(metrics.wilson_95_low, 0.5)
        self.assertGreater(metrics.wilson_95_high, 0.5)


class CampaignLabelTests(unittest.TestCase):
    """A three-act simulator number must arrive wearing its own approximation.

    Campaign mode walks the Act 1 pools three times, so an unlabelled three-act
    result reads as real three-act coverage.  The label belongs on the artifact
    rather than in whoever cites it, and the single-act path must stay byte-stable
    so the pre-campaign population remains comparable.
    """

    def test_campaign_metrics_carry_environment_version_and_coverage(self) -> None:
        metrics = evaluate_policy(
            FakePolicy(),
            env_factory=FakeEnvironment,
            seeds=[100],
            stage="act1",
            split="three_act_probe",
            scope="simulator_three_act",
            checkpoint="fixture.zip",
            campaign=True,
        )
        payload = metrics.to_dict()
        self.assertEqual(payload["environment_version"], CAMPAIGN_ENVIRONMENT_VERSION)
        coverage = payload["content_coverage"]
        self.assertEqual(coverage["verdict"], "approximate")
        self.assertEqual(
            coverage["result_tier"], "simulator_three_act_pools_ancients_and_pair_boss"
        )

    def test_the_declaration_names_the_acts_the_shipped_game_actually_uses(self) -> None:
        # Guards against the campaign quietly being re-described as real coverage: the
        # pools are Hive and Glory from fidelity-v2 on, the events are still not, and
        # neither fact may be dropped from the declaration.
        progression = CAMPAIGN_CONTENT_COVERAGE["real_progression"]
        self.assertEqual(progression["act_2"]["model"], "Hive")
        self.assertEqual(progression["act_3"]["model"], "Glory")
        self.assertEqual(progression["act_3"]["second_boss_at_ascension"], 10)
        stages = CAMPAIGN_CONTENT_COVERAGE["stages"]
        for stage in ("1", "2", "3"):
            self.assertTrue(
                stages[stage]["encounter_pools_match_real_game_act"],
                f"stage {stage} no longer says which act its encounters come from",
            )
        self.assertTrue(stages["1"]["event_pools_match_real_game_act"])
        self.assertFalse(stages["2"]["event_pools_match_real_game_act"])
        self.assertFalse(stages["3"]["event_pools_match_real_game_act"])

    def test_the_declaration_says_which_ancient_each_act_meets(self) -> None:
        # G2's whole content is per-act Ancients, so a declaration that stops naming them
        # has quietly reopened the gate -- and act 2 may never read as a clean pass, because
        # the engine draws only two of the build's three candidates.
        stages = CAMPAIGN_CONTENT_COVERAGE["stages"]
        self.assertEqual(stages["1"]["ancient_matches_real_game_act"], True)
        self.assertEqual(stages["3"]["ancient_matches_real_game_act"], True)
        self.assertEqual(stages["2"]["ancient_matches_real_game_act"], "partial")
        self.assertIn("Orobas", stages["2"]["ancient_note"])

    def test_single_act_metrics_keep_the_pre_campaign_shape(self) -> None:
        metrics = evaluate_policy(
            FakePolicy(),
            env_factory=FakeEnvironment,
            seeds=[100, 101],
            stage="combat",
            split="checkpoint",
            scope="simulator_combat",
            checkpoint="fixture.zip",
        ).to_dict()
        self.assertIsNone(metrics["environment_version"])
        self.assertIsNone(metrics["content_coverage"])
        self.assertEqual(metrics["campaign_clears"], 0)

    def test_the_result_tiers_are_named_and_kept_apart(self) -> None:
        tiers = RESULT_TIERS
        self.assertEqual(
            set(tiers),
            {
                "simulator_single_act",
                "simulator_three_act_approx",
                "simulator_three_act_pools_and_pair_boss",
                "simulator_three_act_pools_ancients_and_pair_boss",
                "simulator_three_act_content_verified",
                "live_full_run",
            },
        )
        # A new tier must not swallow the one above it: v2 says out loud that it is
        # not content-verified, and v3 -- which closes G2 and changes nothing else --
        # has to say the same, because a name that sounds further along is not a gate.
        for tier in ("simulator_three_act_pools_and_pair_boss",
                     "simulator_three_act_pools_ancients_and_pair_boss"):
            with self.subTest(tier=tier):
                self.assertIn("not content-verified", tiers[tier])
        self.assertIn("live A10 win rate",
                      CAMPAIGN_CONTENT_COVERAGE["must_not_be_quoted_as"])


class FakeEnvironment:
    def __init__(self, seed: int):
        self.seed = seed
        self.steps = 0

    def reset(self, *, seed=None, options=None):
        self.seed = self.seed if seed is None else seed
        self.steps = 0
        return [self.seed], {"encounter": "fixture"}

    def action_masks(self):
        return [True, False]

    def step(self, action: int):
        self.steps += 1
        return [self.seed], 1.0, True, False, {"player_won": self.seed % 2 == 0}

    def close(self):
        pass


class FakePolicy:
    def __init__(self, action: int = 0):
        self.action = action

    def predict(self, observation, *, action_masks, deterministic):
        return self.action, None


class EvaluationTests(unittest.TestCase):
    def test_evaluation_records_results_on_explicit_seeds(self) -> None:
        metrics = evaluate_policy(
            FakePolicy(),
            env_factory=FakeEnvironment,
            seeds=[100, 101, 102, 103],
            stage="combat",
            split="checkpoint",
            scope="simulator_combat",
            checkpoint="fixture.zip",
        )
        self.assertEqual(metrics.wins, 2)
        self.assertEqual(metrics.episodes, 4)
        self.assertEqual(metrics.by_encounter["fixture"]["episodes"], 4)

    def test_illegal_model_action_is_counted_and_stops_episode(self) -> None:
        metrics = evaluate_policy(
            FakePolicy(action=1),
            env_factory=FakeEnvironment,
            seeds=[200],
            stage="combat",
            split="checkpoint",
            scope="simulator_combat",
            checkpoint="fixture.zip",
        )
        self.assertEqual(metrics.illegal_actions, 1)
        self.assertEqual(metrics.truncations, 1)
        self.assertEqual(metrics.mean_steps, 0)

    def test_empty_environment_mask_is_truncation_not_policy_illegal_action(self) -> None:
        class EmptyMaskEnvironment(FakeEnvironment):
            def reset(self, *, seed=None, options=None):
                observation, _ = super().reset(seed=seed)
                # Mirrors RunEngine's stale LastPlayerWon flag after a combat
                # win followed by a later map/shop dead-end.
                return observation, {"player_won": True, "floor": 3}

            def action_masks(self):
                return [False, False]

        metrics = evaluate_policy(
            FakePolicy(action=0),
            env_factory=EmptyMaskEnvironment,
            seeds=[200],
            stage="act1",
            split="checkpoint",
            scope="simulator_act1",
            checkpoint="fixture.zip",
        )
        self.assertEqual(metrics.illegal_actions, 0)
        self.assertEqual(metrics.truncations, 1)
        self.assertEqual(metrics.wins, 0)
        self.assertEqual(metrics.mean_steps, 0)

    def test_external_step_cap_breaks_native_rejection_loop(self) -> None:
        """The simulator's native layer can reject mask-legal actions with
        (reward=-1, terminated=False, truncated=False) forever. The evaluator
        must not trust env signals alone (live hang 2026-09-01, seed 20000043).
        """

        class RejectingEnvironment(FakeEnvironment):
            def step(self, action: int):
                # never terminates, never truncates, always -1 reward
                return [self.seed], -1.0, False, False, {"player_won": False}

        metrics = evaluate_policy(
            FakePolicy(),
            env_factory=RejectingEnvironment,
            seeds=[300],
            stage="act1",
            split="checkpoint",
            scope="simulator_act1",
            checkpoint="fixture.zip",
            max_steps_per_episode=50,
        )
        self.assertEqual(metrics.episodes, 1)
        self.assertEqual(metrics.mean_steps, 50)
        self.assertEqual(metrics.truncations, 1)
        self.assertEqual(metrics.truncation_rate, 1.0)


class MetricsIoTests(unittest.TestCase):
    def test_atomic_write_json_retries_through_reader_lock(self) -> None:
        """Windows os.replace raises PermissionError while a reader holds the
        target open; monitoring must never be able to kill a training run."""
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "heartbeat.json"
            target.write_text("{}", encoding="utf-8")
            calls = {"n": 0}
            original = Path.replace

            def flaky_replace(self, other):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise PermissionError(5, "simulated reader lock")
                return original(self, other)

            import training.metrics as metrics_module

            real_sleep = metrics_module.time.sleep
            with unittest.mock.patch.object(Path, "replace", flaky_replace), \
                 unittest.mock.patch.object(metrics_module.time, "sleep", lambda s: real_sleep(0)):
                atomic_write_json(target, {"status": "running"})
            self.assertEqual(calls["n"], 2)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["status"], "running")


class ConfigTests(unittest.TestCase):
    def test_checked_in_curriculum_has_required_stages_and_disjoint_seeds(self) -> None:
        root = Path(__file__).resolve().parent.parent
        config = load_training_config(root / "config" / "training.toml")
        self.assertEqual(
            [stage.name for stage in config.stages], ["combat", "act1", "full_run"]
        )
        self.assertFalse(config.save_load)
        self.assertTrue(config.stage("full_run").experimental)
        train = set(config.seeds["train"].seeds(100))
        checkpoint = set(config.seeds["checkpoint"].seeds(100))
        self.assertTrue(train.isdisjoint(checkpoint))
        for stage in config.stages:
            self.assertGreater(stage.promotion_probe_every_steps, 0)

    def test_config_rejects_save_load(self) -> None:
        root = Path(__file__).resolve().parent.parent
        source = (root / "config" / "training.toml").read_text(encoding="utf-8")
        source = source.replace("save_load = false", "save_load = true", 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.toml"
            path.write_text(source, encoding="utf-8")
            with self.assertRaises(ValueError):
                load_training_config(path)


def _probe_stage(**overrides):
    from training.config import PromotionConfig, StageConfig

    fields: dict = {
        "name": "combat",
        "environment": "combat",
        "timesteps": 10_000_000,
        "parallel_envs": 2,
        "checkpoint_every_steps": 500,
        "checkpoint_eval_episodes": 1,
        "promotion_eval_episodes": 2,
        "max_episode_steps": 80,
        "max_floors": None,
        "initialize_from_previous": False,
        "experimental": False,
        "promotion": PromotionConfig(
            min_episodes=2,
            min_win_rate=1.0,
            min_wilson_lower=0.3,
            max_truncation_rate=0.01,
            max_illegal_actions=0,
        ),
        "promotion_probe_every_steps": 500,
    }
    fields.update(overrides)
    return StageConfig(**fields)


class FakeSavingPolicy:
    """Fakes the pieces of MaskablePPO the callback touches."""

    def __init__(self, action: int = 0):
        self.action = action

    def predict(self, observation, *, action_masks, deterministic):
        return self.action, None

    def save(self, path):
        Path(str(path) + ".zip").write_bytes(b"fake checkpoint")


class EarlyPromotionTests(unittest.TestCase):
    def _callback(self, directory: str, stage, model, promotion_seeds):
        from training.curriculum import _callback_class

        base = type("FakeBase", (), {"__init__": lambda self, verbose=0: None})
        callback = _callback_class(base)(
            stage=stage,
            stage_dir=Path(directory),
            env_factory=FakeEnvironment,
            seeds=[100],
            promotion_seeds=promotion_seeds,
            promotion_probe_every_steps=stage.promotion_probe_every_steps,
        )
        callback.model = model
        callback.num_timesteps = 500
        return callback

    def test_passed_probe_stops_training_and_records_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stage_dir = Path(directory)
            (stage_dir / "checkpoints").mkdir()
            (stage_dir / "metrics").mkdir()
            callback = self._callback(
                directory, _probe_stage(), FakeSavingPolicy(), [200, 202]
            )
            self.assertFalse(callback._on_step())
            self.assertIsNotNone(callback.early_promotion)
            self.assertTrue((stage_dir / "early-promotion-decision.json").is_file())
            self.assertTrue((stage_dir / "metrics" / "promotion.json").is_file())
            self.assertTrue((stage_dir / "metrics" / "step_000000000500.json").is_file())
            decision = json.loads(
                (stage_dir / "early-promotion-decision.json").read_text(encoding="utf-8")
            )
            self.assertTrue(decision["promoted"])
            self.assertEqual(decision["observed"]["episodes"], 2)
            # promotion.json must carry the promotion split, not the checkpoint probe
            metrics = json.loads(
                (stage_dir / "metrics" / "promotion.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metrics["split"], "promotion")

    def test_failed_probe_keeps_training(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stage_dir = Path(directory)
            (stage_dir / "checkpoints").mkdir()
            (stage_dir / "metrics").mkdir()
            callback = self._callback(
                directory, _probe_stage(), FakeSavingPolicy(), [201, 203]
            )
            self.assertTrue(callback._on_step())
            self.assertIsNone(callback.early_promotion)
            self.assertFalse((stage_dir / "early-promotion-decision.json").is_file())
            self.assertFalse((stage_dir / "metrics" / "promotion.json").is_file())
            self.assertTrue((stage_dir / "metrics" / "step_000000000500.json").is_file())
            self.assertTrue((stage_dir / "metrics" / "early-promotion-0m.json").is_file())

    def test_probe_only_fires_on_its_own_interval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stage_dir = Path(directory)
            (stage_dir / "checkpoints").mkdir()
            (stage_dir / "metrics").mkdir()
            stage = _probe_stage(promotion_probe_every_steps=10_000)
            callback = self._callback(directory, stage, FakeSavingPolicy(), [200, 202])
            self.assertTrue(callback._on_step())  # checkpoint saved, probe skipped
            self.assertEqual(callback.early_promotion, None)

    def test_resume_alignment_numbers_from_global_steps(self) -> None:
        """SB3 learn() restarts its counter at zero after load (verified
        2026-09-01); the callback must number checkpoints/probes from
        base + num_timesteps so a resume continues a fresh run's cadence."""
        with tempfile.TemporaryDirectory() as directory:
            stage_dir = Path(directory)
            (stage_dir / "checkpoints").mkdir()
            (stage_dir / "metrics").mkdir()
            # Large probe interval so only checkpoint numbering is exercised.
            stage = _probe_stage(promotion_probe_every_steps=10_000_000)
            from training.curriculum import _callback_class

            base = type("FakeBase", (), {"__init__": lambda self, verbose=0: None})
            callback = _callback_class(base)(
                stage=stage,
                stage_dir=stage_dir,
                env_factory=FakeEnvironment,
                seeds=[100],
                promotion_seeds=[201],  # odd seed -> FakeEnvironment loses; probe never passes
                promotion_probe_every_steps=stage.promotion_probe_every_steps,
                resume_base_steps=10_000_020,
            )
            callback.model = FakeSavingPolicy()
            callback.num_timesteps = 0  # real BaseCallback initializes this
            self.assertEqual(callback.global_steps, 10_000_020)
            self.assertEqual(callback.last_checkpoint, 10_000_000)
            # below one checkpoint interval -> no save
            callback.num_timesteps = 400  # global 10,000,420 < 10,000,500
            self.assertTrue(callback._on_step())
            self.assertEqual(list((stage_dir / "checkpoints").glob("*.zip")), [])
            # cross a boundary -> checkpoint numbered by GLOBAL steps
            callback.num_timesteps = 1_000_004  # global 11,000,024
            self.assertTrue(callback._on_step())
            stem = f"step_{11_000_024:012d}"
            self.assertTrue((stage_dir / "metrics" / f"{stem}.json").is_file())
            self.assertEqual(callback.last_checkpoint, 11_000_024)
            self.assertIsNone(callback.early_promotion)

    def test_resumed_global_step_comes_from_filename_not_sb3_counter(self) -> None:
        from training.curriculum import _checkpoint_global_steps

        self.assertEqual(
            _checkpoint_global_steps(Path("step_000016000008.zip")),
            16_000_008,
        )
        with self.assertRaises(ValueError):
            _checkpoint_global_steps(Path("final.zip"))


if __name__ == "__main__":
    unittest.main()
