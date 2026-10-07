"""The frozen combat executor must absorb fights, not delete them.

G1 claims the trained gradient contains no combat step.  That is only true if the wrapper
actually plays every combat transition it hides, pays the summed reward, and propagates whatever
the fight ended with.  A wrapper that quietly skipped states would make the phase audit go green
while training on a simulator that no longer reaches the same places, so each of those properties
gets its own test against a scripted stand-in for the contract stack.

The stand-in mimics the three surface facts the wrapper depends on: ``info["phase_name"]`` says
which phase it is in, ``action_masks()`` returns the flat mask, and an empty mask comes back as
the sentinel bit only -- which is what ``V2RunEnvWrapper`` does, and why a dead-ended fight is a
truncation here rather than a spin.
"""
from __future__ import annotations

import unittest

import gymnasium as gym
import numpy as np
from gymnasium import spaces

from training.frozen_combat_env import (
    FrozenCombatExecutor,
    IllegalExecutorAction,
    phase_of,
)

ACTION_SPACE = 10
SENTINEL = 9


class _ScriptedStack(gym.Env):
    """A tiny env whose next state is whatever the test put in the script.

    ``states[i]`` is the state the env is in after ``i`` steps (``states[0]`` is the reset
    state), with ``reward``/``terminated``/``truncated``/``mask`` read off that same entry --
    the same "the transition lands in a state" convention the real stack uses.
    """

    def __init__(self, states):
        self.states = list(states)
        self.cursor = 0
        self.steps_taken = 0
        self.action_space = spaces.Discrete(ACTION_SPACE)
        self.observation_space = spaces.Box(0, 1000, shape=(1,), dtype=np.int32)

    def reset(self, *, seed=None, options=None):
        self.cursor = 0
        return self._observation(), self._info()

    def action_masks(self):
        mask = np.zeros(ACTION_SPACE, dtype=bool)
        legal = self._state().get("mask")
        if legal is None:
            mask[:] = True
        elif legal == "empty":
            mask[SENTINEL] = True          # the contract wrapper's sentinel handling
        else:
            mask[list(legal)] = True
        return mask

    def step(self, action):
        self.steps_taken += 1
        self.cursor = min(self.cursor + 1, len(self.states) - 1)
        state = self._state()
        return (self._observation(), float(state.get("reward", 0.0)),
                bool(state.get("terminated", False)), bool(state.get("truncated", False)),
                self._info())

    def _state(self):
        return self.states[self.cursor]

    def _observation(self):
        return np.array([self.cursor], dtype=np.int32)

    def _info(self):
        return {"phase_name": self._state()["phase"], "floor": self._state().get("floor", 1)}


def _lowest_legal(_observation, mask):
    return int(np.flatnonzero(np.asarray(mask, dtype=bool))[0])


def _stack(*phases, **per_state):
    """One scripted state per phase name; ``per_state`` lists align with ``phases``."""
    states = [{"phase": phase} for phase in phases]
    for key, values in per_state.items():
        for state, value in zip(states, values):
            state[key] = value
    return states


def _wrap(states, executor=_lowest_legal, **kw):
    return FrozenCombatExecutor(_ScriptedStack(states), executor, **kw)


class AbsorptionTests(unittest.TestCase):
    def test_the_agent_is_handed_only_out_of_combat_states(self) -> None:
        env = _wrap(_stack("map", "combat", "combat", "combat", "card_reward"))
        env.reset()
        _obs, _r, _t, _tr, info = env.step(0)
        self.assertEqual(phase_of(info), "card_reward")
        self.assertEqual(info["combat_steps"], 3)

    def test_every_hidden_combat_transition_was_really_stepped(self) -> None:
        inner = _ScriptedStack(_stack("map", "combat", "combat", "card_reward"))
        env = FrozenCombatExecutor(inner, _lowest_legal)
        env.reset()
        env.step(0)
        # one agent action (map -> combat) plus two absorbed combat actions.
        self.assertEqual(inner.steps_taken, 3)
        self.assertEqual(env.absorbed_combat_steps, 2)
        self.assertEqual(env.absorbed_combat_episodes, 1)

    def test_absorbed_rewards_reach_the_agent_instead_of_vanishing(self) -> None:
        states = _stack("map", "combat", "combat", "card_reward",
                        reward=[0.0, 1.0, 1.5, 2.0])
        env = _wrap(states)
        env.reset()
        _obs, reward, _t, _tr, _info = env.step(0)
        self.assertAlmostEqual(4.5, reward, places=9)

    def test_reset_never_returns_a_combat_state(self) -> None:
        env = _wrap(_stack("combat", "combat", "map"))
        _obs, info = env.reset()
        self.assertEqual(phase_of(info), "map")
        self.assertEqual(info["combat_steps"], 2)

    def test_an_out_of_combat_step_is_passed_through_untouched(self) -> None:
        env = _wrap(_stack("map", "card_reward"))
        env.reset()
        _obs, reward, _t, _tr, info = env.step(0)
        self.assertEqual(phase_of(info), "card_reward")
        self.assertNotIn("combat_steps", info)
        self.assertEqual(env.absorbed_combat_steps, 0)


class TerminationTests(unittest.TestCase):
    def test_dying_in_a_fight_terminates_the_transition_the_agent_is_charged_for(self) -> None:
        states = _stack("map", "combat", "combat",
                        terminated=[False, False, True],
                        reward=[0.0, 0.0, -25.0])
        env = _wrap(states)
        env.reset()
        _obs, reward, terminated, truncated, info = env.step(0)
        self.assertTrue(terminated)
        self.assertFalse(truncated)
        self.assertAlmostEqual(-25.0, reward, places=9)
        # The absorbed steps are still reported: a death is a result, not a skipped fight.
        self.assertEqual(info["combat_steps"], 1)

    def test_an_empty_mask_inside_a_fight_is_the_wrappers_dead_end_not_a_spin(self) -> None:
        states = _stack("map", "combat", "combat",
                        mask=[None, [0, 1], "empty"],
                        truncated=[False, False, True])
        inner = _ScriptedStack(states)
        env = FrozenCombatExecutor(inner, _lowest_legal)
        env.reset()
        _obs, _r, _t, truncated, info = env.step(0)
        self.assertTrue(truncated)
        self.assertEqual(phase_of(info), "combat")
        # The sentinel is the only legal action, so exactly one absorbed step is possible.
        self.assertEqual(inner.steps_taken, 2)
        self.assertEqual(info["combat_steps"], 1)

    def test_a_fight_the_executor_cannot_finish_is_reported_not_traded_away(self) -> None:
        env = _wrap(_stack("map", *(["combat"] * 50)), max_combat_steps=5)
        env.reset()
        _obs, _r, terminated, truncated, info = env.step(0)
        self.assertFalse(terminated)
        self.assertTrue(truncated)
        self.assertTrue(info["combat_executor_step_cap"])
        self.assertEqual(info["combat_steps"], 5)


class ExecutorContractTests(unittest.TestCase):
    def test_an_illegal_executor_choice_raises_instead_of_being_rounded(self) -> None:
        states = _stack("map", "combat", mask=[None, [3]])
        env = _wrap(states, executor=lambda obs, mask: 0)
        env.reset()
        with self.assertRaises(IllegalExecutorAction):
            env.step(0)

    def test_the_mask_the_executor_sees_is_the_current_states_mask(self) -> None:
        seen = []

        def spy(observation, mask):
            seen.append(np.flatnonzero(np.asarray(mask, dtype=bool)).tolist())
            return int(np.flatnonzero(np.asarray(mask, dtype=bool))[0])

        states = _stack("map", "combat", "card_reward", mask=[None, [3, 4], [0, 1]])
        env = _wrap(states, executor=spy)
        env.reset()
        env.step(0)
        self.assertEqual([[3, 4]], seen)

    def test_action_masks_is_forwarded_to_the_sb3_masker(self) -> None:
        env = _wrap(_stack("map", mask=[[2, 5]]))
        env.reset()
        self.assertEqual([2, 5],
                         np.flatnonzero(env.action_masks()).tolist())

    def test_a_non_positive_step_cap_is_refused_at_construction(self) -> None:
        with self.assertRaises(ValueError):
            _wrap(_stack("map"), max_combat_steps=0)


if __name__ == "__main__":
    unittest.main()
