from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from training.config import PromotionConfig, load_training_config
from training.evaluation import evaluate_policy
from training.metrics import EpisodeMetric, summarize_episodes
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

    def test_config_rejects_save_load(self) -> None:
        root = Path(__file__).resolve().parent.parent
        source = (root / "config" / "training.toml").read_text(encoding="utf-8")
        source = source.replace("save_load = false", "save_load = true", 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.toml"
            path.write_text(source, encoding="utf-8")
            with self.assertRaises(ValueError):
                load_training_config(path)


if __name__ == "__main__":
    unittest.main()
