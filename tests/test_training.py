from __future__ import annotations

import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from training.config import PromotionConfig, load_training_config
from training.evaluation import evaluate_policy
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
        self.assertEqual(metrics.schema_version, 1)
        self.assertEqual(len(metrics.seed_sha256), 64)
        self.assertLess(metrics.wilson_95_low, 0.5)
        self.assertGreater(metrics.wilson_95_high, 0.5)


class FakeEnvironment:
    def __init__(self, seed: int):
        self.seed = seed
        self.steps = 0

    def reset(self, *, seed=None):
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


if __name__ == "__main__":
    unittest.main()
