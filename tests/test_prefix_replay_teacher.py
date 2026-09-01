from __future__ import annotations

import unittest

from training.prefix_replay_teacher import (
    SCOPE,
    ActionTarget,
    IllegalReplayAction,
    ReplayHashMismatch,
    RolloutBudget,
    capture_prefix,
    replay_prefix,
    score_candidates,
)


class TinyRunEnv:
    """Deterministic action/target environment with an Sts2RunEnv-like API."""

    created = 0
    closed = 0

    def __init__(self, seed: int, *, drift: int = 0):
        type(self).created += 1
        self.seed = seed
        self.drift = drift
        self.value = 0
        self.steps = 0

    def reset(self, *, seed=None):
        self.seed = self.seed if seed is None else seed
        self.value = int(self.seed) % 7 + self.drift
        self.steps = 0
        return [self.value, self.steps], self._info(False)

    def action_masks(self):
        if self.steps >= 4:
            return [False, False, False]
        return [True, True, self.value % 2 == 0]

    def step(self, action: int, target: int = -1):
        mask = self.action_masks()
        if action < 0 or action >= len(mask) or not mask[action]:
            return [self.value, self.steps], -1.0, False, False, self._info(False)
        self.steps += 1
        self.value += (action + 1) * (target + 2)
        terminated = self.steps >= 4
        won = terminated and self.value >= 12
        return (
            [self.value, self.steps],
            float(self.value) / 10.0,
            terminated,
            False,
            self._info(won),
        )

    def _info(self, won: bool):
        return {"player_won": won, "floor": self.steps}

    def close(self):
        type(self).closed += 1


def factory(seed):
    return TinyRunEnv(seed)


def first_legal(_observation, mask, _info, _step):
    return ActionTarget(next(index for index, legal in enumerate(mask) if legal))


class PrefixReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        TinyRunEnv.created = 0
        TinyRunEnv.closed = 0

    def test_capture_and_replay_validate_each_state_hash(self) -> None:
        prefix = capture_prefix(
            9,
            [ActionTarget(0, 0), ActionTarget(1, 1)],
            env_factory=factory,
        )
        self.assertEqual(prefix.scope, SCOPE)
        self.assertEqual(len(prefix.sha256), 64)

        with replay_prefix(prefix, env_factory=factory) as rebuilt:
            self.assertEqual(rebuilt.info["floor"], 2)
            self.assertFalse(rebuilt.terminated)
            self.assertTrue(any(rebuilt.action_mask))

        self.assertEqual(TinyRunEnv.created, TinyRunEnv.closed)

    def test_replay_detects_emulator_or_observation_drift(self) -> None:
        prefix = capture_prefix(9, [ActionTarget(0)], env_factory=factory)

        with self.assertRaises(ReplayHashMismatch):
            with replay_prefix(
                prefix,
                env_factory=lambda seed: TinyRunEnv(seed, drift=1),
            ):
                pass

        self.assertEqual(TinyRunEnv.created, TinyRunEnv.closed)

    def test_candidate_rollouts_are_isolated_bounded_and_simulator_labeled(self) -> None:
        prefix = capture_prefix(9, [ActionTarget(0, 0)], env_factory=factory)
        scores = score_candidates(
            prefix,
            [ActionTarget(0), ActionTarget(1, 1)],
            env_factory=factory,
            continuation_policy=first_legal,
            budget=RolloutBudget(max_steps=2, discount=0.9),
            leaf_value=lambda observation, _info: observation[0] / 100.0,
        )

        self.assertEqual(scores.scope, "simulator_act1")
        self.assertIn("not a real-game A10 result", scores.disclaimer)
        self.assertEqual(len(scores.candidates), 2)
        self.assertTrue(all(item.rollout_steps <= 2 for item in scores.candidates))
        self.assertEqual(scores.best.candidate, ActionTarget(1, 1))
        # Capture plus one independent reconstruction per candidate.
        self.assertEqual(TinyRunEnv.created, 3)
        self.assertEqual(TinyRunEnv.created, TinyRunEnv.closed)

    def test_mask_illegal_candidate_is_rejected_before_step(self) -> None:
        # seed 8 starts with odd value, so action 2 is mask-illegal.
        prefix = capture_prefix(8, [], env_factory=factory)
        with self.assertRaises(IllegalReplayAction):
            score_candidates(
                prefix,
                [ActionTarget(2)],
                env_factory=factory,
                continuation_policy=first_legal,
            )

        self.assertEqual(TinyRunEnv.created, TinyRunEnv.closed)

    def test_stale_combat_win_flag_is_not_reported_as_run_win(self) -> None:
        class StaleWinEnv(TinyRunEnv):
            def _info(self, won: bool):
                return {"player_won": True, "floor": self.steps}

        stale_factory = lambda seed: StaleWinEnv(seed)
        prefix = capture_prefix(9, [], env_factory=stale_factory)
        scores = score_candidates(
            prefix,
            [ActionTarget(0)],
            env_factory=stale_factory,
            continuation_policy=first_legal,
            budget=RolloutBudget(max_steps=1),
        )
        self.assertFalse(scores.candidates[0].terminated)
        self.assertFalse(scores.candidates[0].player_won)


if __name__ == "__main__":
    unittest.main()
