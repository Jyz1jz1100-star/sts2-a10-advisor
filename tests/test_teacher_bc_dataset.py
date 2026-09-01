"""Tests for the teacher-records -> BC-samples converter."""

from __future__ import annotations

import unittest
from pathlib import Path

import numpy as np
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_teacher_batch import (  # noqa: E402
    FakeTeacherEnv,
    fake_factory,
    make_raw,
    mask_of,
)
from tests.test_v2_contract import empty_lists  # noqa: E402
from training.teacher_batch import (  # noqa: E402
    TeacherBatchConfig,
    TraversalDecision,
    label_decision,
)
from training.teacher_bc_dataset import materialize_records  # noqa: E402


class FakeCore:
    """RunCore adapter over the teacher test environment."""

    def __init__(self):
        self._env = None

    def reset(self, seed):
        self._env = FakeTeacherEnv(seed)
        observation, info = self._env.reset(seed=seed)
        return np.asarray(observation, dtype=np.int32), info, 0

    def step(self, action, target):
        observation, reward, terminated, truncated, info = self._env.step(action, target)
        return (
            np.asarray(observation, dtype=np.int32),
            reward,
            terminated,
            truncated,
            info,
            0,
        )

    def action_mask(self):
        return self._env.action_masks()

    def state_lists(self):
        return empty_lists()

    def close(self):
        pass


def one_labelled_record() -> dict:
    decision = TraversalDecision(
        seed=3,
        decisions=[],
        raw_obs=make_raw(0),
        base_mask=mask_of([0, 1, 3]),
        info={"player_won": False, "floor": 5, "player_hp": 60,
              "player_max_hp": 80, "current_node_type": 1, "encounter_id": 17},
    )
    record = label_decision(
        decision,
        env_factory=fake_factory,
        emulator_hash="ab" * 32,
        config=TeacherBatchConfig(rollout_max_steps=4, min_score_gap=0.1,
                                  max_decisions_per_run=5, traversal_steps=6),
    )
    assert record is not None
    return record


class MaterializeTests(unittest.TestCase):
    def test_record_converts_with_verified_state_and_legal_label(self) -> None:
        record = one_labelled_record()
        samples = list(materialize_records(iter([record]), core_factory=FakeCore))
        self.assertEqual(len(samples), 1)
        sample = samples[0]
        self.assertEqual(sample["scope"], "simulator_act1")
        self.assertEqual(sample["label_action_id"], record["best_action_id"])
        self.assertIn(sample["label_flat_action"], sample["legal_flat_actions"])
        self.assertEqual(len(sample["observation_sha256"]), 64)
        self.assertEqual(sample["prefix_sha256"], record["prefix_sha256"])

    def test_tampered_prefix_hash_is_refused(self) -> None:
        record = one_labelled_record()
        record["prefix"]["final_state"]["observation_sha256"] = "00" * 32
        with self.assertRaises(ValueError):
            list(materialize_records(iter([record]), core_factory=FakeCore))

    def test_seed_swap_is_refused(self) -> None:
        record = one_labelled_record()
        broken = dict(record)
        broken["seed"] = 999
        with self.assertRaises(ValueError):
            list(materialize_records(iter([broken]), core_factory=FakeCore))


if __name__ == "__main__":
    unittest.main()
