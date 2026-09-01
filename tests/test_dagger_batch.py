"""Tests for the DAgger student-traversal batch generator (scripted cores)."""

from __future__ import annotations

import random
import unittest
from pathlib import Path

import numpy as np
import torch

sys_path = str(Path(__file__).resolve().parents[1])
import sys

sys.path.insert(0, sys_path)

from tests.test_teacher_batch import make_raw, mask_of  # noqa: E402
from training.behavior_clone_v2 import BCv2Config, PhaseSplitActionScorer  # noqa: E402
from training.dagger_batch import (  # noqa: E402
    DaggerConfig,
    generate_dagger_batch,
    student_choice,
    traverse_with_student,
)
from training.v2_flat_env import FLAT_SIZE, flat_index  # noqa: E402
from training.v2_native_env import NativeRunCore  # noqa: E402


class RejectingBridgeCore:
    """RunCore where action 0 is always rejected (noop), action 3 advances.

    Mirrors the *contract* semantics the student trains under: a rejected
    step reports status -1 with an unchanged state, so noop mode must keep
    the episode alive and the traversal must exclude the action from the
    prefix instead of appending it.
    """

    def __init__(self):
        self.steps_taken: list[tuple[int, int]] = []
        self.floor = 5

    def reset(self, seed):
        self.floor = 5
        return make_raw(0), _info(), 0

    def step(self, action, target):
        self.steps_taken.append((action, target))
        if action == 0:
            return make_raw(0), 0.0, False, False, _info(), -1
        return make_raw(0), 0.1, True, False, _info(), 0

    def action_mask(self):
        return mask_of([0, 3])

    def state_lists(self):
        from tests.test_v2_contract import empty_lists

        return empty_lists()

    def close(self):
        pass


class AdvancingCore(RejectingBridgeCore):
    """Every action is accepted; the episode ends after three steps."""

    def __init__(self):
        super().__init__()
        self.count = 0

    def step(self, action, target):
        self.steps_taken.append((action, target))
        self.count += 1
        return make_raw(0), 0.1, self.count >= 3, False, _info(), 0


def _info():
    return {"player_won": False, "floor": 5, "player_hp": 60,
            "player_max_hp": 80, "current_node_type": 1, "encounter_id": 17}


def _tiny_model() -> PhaseSplitActionScorer:
    torch.manual_seed(0)
    model = PhaseSplitActionScorer(BCv2Config(hidden_dim=64, depth=1))
    model.eval()
    return model


class TraversalTests(unittest.TestCase):
    def test_student_choice_is_legal_and_reports_margin(self) -> None:
        from tests.test_v2_contract import empty_lists
        from training.v2_observation import build_observation

        model = _tiny_model()
        lists = empty_lists()
        expanded = build_observation(
            make_raw(0), deck=lists["deck"], relics=lists["relics"],
            potions=lists["potions"], neow_options=lists["neow_options"],
            reward_upgraded=lists["reward_upgraded"],
            pending_rewards=lists["pending_rewards"],
            map_coords=lists["map_coords"], shop_cards=lists["shop_cards"],
            shop_costs=lists["shop_costs"],
        )
        mask = np.zeros(FLAT_SIZE, dtype=bool)
        mask[flat_index(0, 0)] = True
        mask[flat_index(0, 1)] = True
        mask[flat_index(3, None)] = True
        chosen, margin = student_choice(
            model, expanded, mask, device="cpu", excluded={flat_index(3, None)}
        )
        self.assertIn(chosen, {flat_index(0, 0), flat_index(0, 1)})
        self.assertTrue(np.isfinite(margin))

    def test_noop_rejection_is_excluded_not_appended(self) -> None:
        core = RejectingBridgeCore()
        model = _tiny_model()
        seen = list(traverse_with_student(
            5,
            core_factory=lambda: core,
            model=model,
            device="cpu",
            rng=random.Random(1),
            config=DaggerConfig(traversal_steps=10),
        ))
        self.assertGreaterEqual(len(seen), 1)
        # A split variant of the always-rejected action 0 was tried at the
        # first state; the next step taken must be the accepted action 3.
        first_taken = core.steps_taken[0]
        self.assertEqual(first_taken[0], 0)
        self.assertEqual(core.steps_taken[-1], (3, -1))
        # Rejected actions never enter a training prefix.
        for decision, _info in seen:
            for step_decision in decision.decisions:
                self.assertNotEqual(step_decision.action, 0)

    def test_generation_yields_dagger_records(self) -> None:
        # Scripted env factory mirroring the rejecting core for replay: the
        # teacher path replays each accepted prefix through it.
        class ReplayEnv:
            def __init__(self, seed):
                self.steps = 0

            def reset(self, *, seed=None):
                self.steps = 0
                return make_raw(0), _info()

            def action_masks(self):
                return mask_of([0, 3])

            def step(self, action, target=-1):
                self.steps += 1
                if action == 0:
                    return make_raw(0), 0.0, False, False, _info()
                return make_raw(0), 0.1, True, False, _info()

            def close(self):
                pass

        core = AdvancingCore()
        records = list(generate_dagger_batch(
            [5],
            core_factory=lambda: core,
            env_factory=lambda seed: ReplayEnv(seed),
            model=_tiny_model(),
            emulator_hash="ab" * 32,
            device="cpu",
            config=DaggerConfig(traversal_steps=6, max_decisions_per_run=4,
                                rollout_max_steps=3, min_score_gap=0.0,
                                safe_label_probability=1.0),
        ))
        self.assertGreaterEqual(len(records), 1)
        for record in records:
            self.assertEqual(record["source"], "dagger")
            self.assertEqual(record["scope"], "simulator_act1")
            self.assertIn("student", record)
            self.assertIn("teacher_agreement", record)
            self.assertIn("margin", record["student"])


if __name__ == "__main__":
    unittest.main()
