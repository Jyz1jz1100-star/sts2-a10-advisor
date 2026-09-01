from __future__ import annotations

import unittest

import gymnasium as gym
import numpy as np

from training.v2_run_wrapper import (
    V2RewardConfig,
    V2RunEnvWrapper,
    is_run_victory,
    run_potential,
)


class ScriptedRunEnv(gym.Env):
    def __init__(self, transitions=(), *, mask=(True, False, False), reset_info=None):
        super().__init__()
        self.action_space = gym.spaces.Discrete(3)
        self.observation_space = gym.spaces.Box(
            low=0, high=100, shape=(1,), dtype=np.int32
        )
        self.transitions = list(transitions)
        self.mask = np.asarray(mask, dtype=bool)
        self.reset_info = dict(
            reset_info
            or {"floor": 1, "player_hp": 50, "player_max_hp": 100}
        )
        self.step_calls = 0

    def reset(self, *, seed=None, options=None):
        super().reset(seed=seed)
        self.step_calls = 0
        return np.asarray([0], dtype=np.int32), dict(self.reset_info)

    def action_masks(self):
        return self.mask.copy()

    def step(self, action):
        self.step_calls += 1
        return self.transitions.pop(0)


ZERO_REWARD = V2RewardConfig(
    gamma=1.0,
    combat_reward_scale=0.0,
    floor_weight=0.0,
    hp_weight=0.0,
    terminal_win_reward=0.0,
    terminal_death_reward=0.0,
)


class OutcomeContractTests(unittest.TestCase):
    def test_victory_requires_natural_nontruncated_terminal(self):
        stale = {"player_won": True}
        self.assertFalse(
            is_run_victory(terminated=False, truncated=False, info=stale)
        )
        self.assertFalse(is_run_victory(terminated=False, truncated=True, info=stale))
        self.assertFalse(is_run_victory(terminated=True, truncated=True, info=stale))
        self.assertTrue(is_run_victory(terminated=True, truncated=False, info=stale))

    def test_public_player_won_is_normalized_but_raw_flag_is_preserved(self):
        transition = (
            np.asarray([1], dtype=np.int32),
            0.0,
            False,
            False,
            {"floor": 2, "player_won": True},
        )
        wrapper = V2RunEnvWrapper(
            ScriptedRunEnv([transition]), reward_config=ZERO_REWARD
        )
        wrapper.reset()
        _, _, terminated, truncated, info = wrapper.step(0)
        self.assertFalse(terminated)
        self.assertFalse(truncated)
        self.assertTrue(info["simulator_player_won"])
        self.assertFalse(info["player_won"])
        self.assertFalse(info["run_won"])
        self.assertEqual(info["run_outcome"], "ongoing")

    def test_true_terminal_win_gets_one_win_bonus(self):
        transition = (
            np.asarray([1], dtype=np.int32),
            3.0,
            True,
            False,
            {"floor": 16, "player_won": True, "player_hp": 20, "player_max_hp": 80},
        )
        config = V2RewardConfig(
            gamma=1.0,
            combat_reward_scale=2.0,
            floor_weight=0.0,
            hp_weight=0.0,
            terminal_win_reward=7.0,
            terminal_death_reward=-9.0,
        )
        wrapper = V2RunEnvWrapper(ScriptedRunEnv([transition]), reward_config=config)
        wrapper.reset()
        _, reward, terminated, truncated, info = wrapper.step(0)
        self.assertTrue(terminated)
        self.assertFalse(truncated)
        self.assertTrue(info["player_won"])
        self.assertEqual(info["run_outcome"], "win")
        self.assertAlmostEqual(reward, 13.0)
        self.assertAlmostEqual(info["terminal_reward"], 7.0)


class BoundaryTests(unittest.TestCase):
    def test_all_zero_mask_advertises_sentinel_then_truncates_in_one_step(self):
        base = ScriptedRunEnv(mask=(False, False, False))
        wrapper = V2RunEnvWrapper(base, reward_config=ZERO_REWARD)
        original_observation, _ = wrapper.reset()
        mask = wrapper.action_masks()
        self.assertEqual(mask.tolist(), [False, False, True])

        observation, reward, terminated, truncated, info = wrapper.step(2)
        np.testing.assert_array_equal(observation, original_observation)
        self.assertEqual(base.step_calls, 0)
        self.assertEqual(reward, 0.0)
        self.assertFalse(terminated)
        self.assertTrue(truncated)
        self.assertEqual(info["simulator_dead_end"], "empty_action_mask")
        self.assertEqual(info["run_outcome"], "truncated")
        self.assertFalse(info["run_won"])
        with self.assertRaises(RuntimeError):
            wrapper.step(2)

    def test_step_without_mask_query_still_catches_empty_mask(self):
        base = ScriptedRunEnv(mask=(False, False, False))
        wrapper = V2RunEnvWrapper(base, reward_config=ZERO_REWARD)
        wrapper.reset()
        _, _, _, truncated, info = wrapper.step(0)
        self.assertTrue(truncated)
        self.assertEqual(base.step_calls, 0)
        self.assertEqual(info["run_outcome_source"], "empty_action_mask")

    def test_max_floor_is_a_truncation_not_a_win_or_death(self):
        transition = (
            np.asarray([1], dtype=np.int32),
            0.0,
            False,
            False,
            {"floor": 3, "player_won": True, "player_hp": 40, "player_max_hp": 80},
        )
        config = V2RewardConfig(
            gamma=1.0,
            combat_reward_scale=0.0,
            floor_weight=1.0,
            hp_weight=0.0,
            terminal_win_reward=100.0,
            terminal_death_reward=-100.0,
        )
        wrapper = V2RunEnvWrapper(
            ScriptedRunEnv([transition]), max_floor=3, reward_config=config
        )
        wrapper.reset()
        _, reward, terminated, truncated, info = wrapper.step(0)
        self.assertFalse(terminated)
        self.assertTrue(truncated)
        self.assertTrue(info["curriculum_truncated"])
        self.assertEqual(info["curriculum_max_floor"], 3)
        self.assertTrue(info["simulator_player_won"])
        self.assertFalse(info["player_won"])
        self.assertEqual(info["terminal_reward"], 0.0)
        self.assertAlmostEqual(reward, 2.0)

    def test_natural_terminal_at_max_floor_remains_authoritative(self):
        transition = (
            np.asarray([1], dtype=np.int32),
            0.0,
            True,
            False,
            {"floor": 3, "player_won": True},
        )
        wrapper = V2RunEnvWrapper(
            ScriptedRunEnv([transition]), max_floor=3, reward_config=ZERO_REWARD
        )
        wrapper.reset()
        _, _, terminated, truncated, info = wrapper.step(0)
        self.assertTrue(terminated)
        self.assertFalse(truncated)
        self.assertTrue(info["run_won"])
        self.assertNotIn("curriculum_truncated", info)


class RewardTests(unittest.TestCase):
    def test_floor_hp_potential_accepts_simulator_and_trace_keys(self):
        config = V2RewardConfig(floor_weight=2.0, hp_weight=4.0)
        self.assertAlmostEqual(
            run_potential(
                {"floor": 3, "player_hp": 25, "player_max_hp": 100}, config
            ),
            7.0,
        )
        self.assertAlmostEqual(
            run_potential({"floor": 3, "hp": 25, "max_hp": 100}, config), 7.0
        )

    def test_scaled_potential_reward_then_terminal_death_reward(self):
        transitions = [
            (
                np.asarray([1], dtype=np.int32),
                5.0,
                False,
                False,
                {"floor": 2, "player_hp": 75, "player_max_hp": 100},
            ),
            (
                np.asarray([2], dtype=np.int32),
                -2.0,
                True,
                False,
                {
                    "floor": 2,
                    "player_hp": 0,
                    "player_max_hp": 100,
                    "player_won": False,
                },
            ),
        ]
        config = V2RewardConfig(
            gamma=0.5,
            combat_reward_scale=0.1,
            floor_weight=2.0,
            hp_weight=4.0,
            terminal_win_reward=10.0,
            terminal_death_reward=-10.0,
        )
        wrapper = V2RunEnvWrapper(
            ScriptedRunEnv(transitions), reward_config=config
        )
        wrapper.reset()

        _, first_reward, terminated, truncated, first_info = wrapper.step(0)
        self.assertFalse(terminated)
        self.assertFalse(truncated)
        # Phi(reset)=4, Phi(next)=7: 0.5 + 0.5*7 - 4 = 0.
        self.assertAlmostEqual(first_reward, 0.0)
        self.assertAlmostEqual(first_info["scaled_combat_reward"], 0.5)
        self.assertAlmostEqual(first_info["potential_reward"], -0.5)

        _, death_reward, terminated, truncated, death_info = wrapper.step(0)
        self.assertTrue(terminated)
        self.assertFalse(truncated)
        # Terminal Phi is zero: -0.2 - 7 - 10 = -17.2.
        self.assertAlmostEqual(death_reward, -17.2)
        self.assertAlmostEqual(death_info["potential_after"], 4.0)
        self.assertAlmostEqual(death_info["potential_reward"], -7.0)
        self.assertAlmostEqual(death_info["terminal_reward"], -10.0)
        self.assertEqual(death_info["run_outcome"], "loss")

    def test_config_rejects_invalid_values(self):
        with self.assertRaises(ValueError):
            V2RewardConfig(gamma=1.01)
        with self.assertRaises(ValueError):
            V2RewardConfig(floor_weight=float("nan"))
        with self.assertRaises(ValueError):
            V2RunEnvWrapper(ScriptedRunEnv(), max_floor=0)


if __name__ == "__main__":
    unittest.main()
