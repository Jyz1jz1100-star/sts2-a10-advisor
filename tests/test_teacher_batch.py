"""Tests for the V2 teacher batch label generator."""

from __future__ import annotations

import unittest
from pathlib import Path

import numpy as np

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from training.teacher_batch import (  # noqa: E402
    TeacherBatchConfig,
    TraversalDecision,
    alive_enemy_indices,
    codec_for_state,
    generate_batch,
    is_interesting,
    label_decision,
)
from training.v2_constants import COMBAT_OBS_SIZE, RUN_MAX_ACTIONS, RUN_OBS_SIZE  # noqa: E402


def make_raw(phase: int, *, hand=(30, 472, 131), enemies: int = 2, hp=60, max_hp=80):
    raw = np.zeros(RUN_OBS_SIZE, dtype=np.int32)
    raw[0] = hp
    raw[1] = max_hp
    raw[3] = 3
    for index, def_id in enumerate(hand):
        raw[8 + index * 2] = def_id
    for slot in range(enemies):
        raw[54 + slot * 15] = 20
        raw[54 + slot * 15 + 1] = 30
    raw[COMBAT_OBS_SIZE + 0] = phase
    raw[COMBAT_OBS_SIZE + 1] = 5
    return raw


def mask_of(legal):
    mask = np.zeros(RUN_MAX_ACTIONS, dtype=bool)
    for action in legal:
        mask[action] = True
    return mask


class CandidateEnumerationTests(unittest.TestCase):
    def test_two_enemies_split_only_single_target_cards(self) -> None:
        codec = codec_for_state(
            make_raw(0, hand=(30, 465, 131)),  # Bash, Stomp(AoE), Defend
            mask_of([0, 1, 2, 3]),
            encounter_id=17,
        )
        ids = [item.action_id for item in codec.candidates]
        self.assertEqual(sum(1 for item in codec.candidates if item.action == 0), 2)
        self.assertEqual(sum(1 for item in codec.candidates if item.action == 1), 1)
        self.assertEqual(sum(1 for item in codec.candidates if item.action == 2), 1)
        self.assertTrue(any(id.startswith("v2:a:card%3A30%40h0:t:") for id in ids))

    def test_map_state_candidates_equal_map_options(self) -> None:
        codec = codec_for_state(make_raw(2), mask_of([0, 2]), encounter_id=-1)
        self.assertEqual(len(codec), 2)
        self.assertTrue(all(item.target is None for item in codec.candidates))

    def test_interesting_predicate_rules(self) -> None:
        def decision(phase, hp, enemies, node_type=1):
            return TraversalDecision(
                seed=1,
                decisions=[],
                raw_obs=make_raw(phase, enemies=enemies, hp=hp),
                base_mask=mask_of([0, 3] if phase == 0 else [0]),
                info={"player_hp": hp, "player_max_hp": 80, "floor": 5,
                      "current_node_type": node_type, "encounter_id": 1},
            )

        self.assertTrue(is_interesting(decision(0, 20, 2)))  # multi-enemy dmg
        self.assertTrue(is_interesting(decision(0, 10, 1)))  # low hp
        self.assertTrue(is_interesting(decision(0, 70, 1, node_type=6)))  # boss
        self.assertFalse(is_interesting(decision(0, 80, 1)))  # safe single dummy
        self.assertTrue(is_interesting(decision(2, 80, 0)))  # map route
        self.assertTrue(is_interesting(decision(4, 80, 0)))  # shop


class FakeTeacherEnv:
    """Deterministic env with a combat state whose target choice matters."""

    def __init__(self, seed):
        self.seed = int(seed)
        self.steps = 0

    def reset(self, *, seed=None):
        self.seed = self.seed if seed is None else int(seed)
        self.steps = 0
        return make_raw(0), {"player_won": False, "floor": 5,
                             "player_hp": 60, "player_max_hp": 80,
                             "current_node_type": 1, "encounter_id": 17}

    def action_masks(self):
        # Slot 2 (Defend) is not playable this turn, matching every fixture
        # mask used by the candidate-enumeration tests above.
        return mask_of([0, 1, 3])

    def step(self, action, target=-1):
        self.steps += 1
        # Killing enemy 0 requires targeting it; wrong targets dawdle.
        if action == 0 and target == 0:
            return (make_raw(0, enemies=1, hand=(472, 131)), 0.4, self.steps >= 4, False,
                    {"player_won": False, "floor": 5})
        return (make_raw(0, hand=(30, 472, 131)), 0.05, False, False,
                {"player_won": False, "floor": 5})

    def close(self):
        pass


def fake_factory(seed):
    return FakeTeacherEnv(seed)


class LabelRecordTests(unittest.TestCase):
    def config(self) -> TeacherBatchConfig:
        return TeacherBatchConfig(rollout_max_steps=4, min_score_gap=0.1,
                                  max_decisions_per_run=5, traversal_steps=6)

    def test_record_has_gap_hashes_and_scope(self) -> None:
        decision = TraversalDecision(
            seed=3,
            decisions=[],
            raw_obs=make_raw(0),
            base_mask=mask_of([0, 1, 3]),
            info={"player_won": False, "floor": 5, "player_hp": 60,
                  "player_max_hp": 80, "current_node_type": 1, "encounter_id": 17},
        )
        record = label_decision(
            decision, env_factory=fake_factory,
            emulator_hash="ab" * 32, config=self.config(),
        )
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["scope"], "simulator_act1")
        self.assertIn("not a real-game A10 result", record["disclaimer"])
        self.assertEqual(record["emulator_native_sha256"], "ab" * 32)
        self.assertEqual(len(record["prefix_sha256"]), 64)
        self.assertEqual(record["state_hashes"]["capture"],
                         record["state_hashes"]["reverify"])
        self.assertGreaterEqual(record["score_gap"], 0.1)
        self.assertTrue(record["high_confidence"])
        # The teacher's best candidate must be an actual (action,target) pair
        # from this state's codec, and the runner-up must be different.
        self.assertNotEqual(record["best_action_id"], record["runner_up_action_id"])
        by_id = {item["action_id"]: item for item in record["candidates"]}
        self.assertIn(record["best_action_id"], by_id)

    def test_single_candidate_state_is_not_labelled(self) -> None:
        decision = TraversalDecision(
            seed=3,
            decisions=[],
            raw_obs=make_raw(2),
            base_mask=mask_of([1]),
            info={"player_won": False, "floor": 5, "player_hp": 60,
                  "player_max_hp": 80, "current_node_type": 1, "encounter_id": -1},
        )
        record = label_decision(
            decision, env_factory=fake_factory,
            emulator_hash="ab" * 32, config=self.config(),
        )
        self.assertIsNone(record)


class IntegrationTests(unittest.TestCase):
    """End-to-end: traversal + labelling over the scripted fake environment."""

    def test_generate_batch_yields_only_validated_records(self) -> None:
        records = list(
            generate_batch(
                [3, 4],
                env_factory=fake_factory,
                emulator_hash="cd" * 32,
                config=TeacherBatchConfig(rollout_max_steps=3, min_score_gap=0.05,
                                          max_decisions_per_run=3, traversal_steps=4),
            )
        )
        self.assertGreaterEqual(len(records), 1)
        for record in records:
            self.assertEqual(record["state_hashes"]["capture"],
                             record["state_hashes"]["reverify"])
            self.assertGreaterEqual(record["score_gap"], 0.05)
            self.assertTrue(record["replay_verified"])


if __name__ == "__main__":
    unittest.main()
