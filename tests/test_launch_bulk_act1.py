"""Small pure tests for the bounded bulk launcher budget guard."""

from __future__ import annotations

import unittest

from scripts.launch_bulk_act1 import _HardBudgetStopMixin, _safe_total_timesteps


class _FakeCallback:
    def __init__(self, budget: int) -> None:
        self._hard_budget = budget
        self.num_timesteps = 0
        self.calls = 0

    def _on_step(self) -> bool:
        self.calls += 1
        return True


class _GuardedFakeCallback(_HardBudgetStopMixin, _FakeCallback):
    pass


class BulkLauncherBudgetTests(unittest.TestCase):
    def test_budget_floors_to_vector_step_without_overshoot(self) -> None:
        self.assertEqual(_safe_total_timesteps(20_000_000, 8), 20_000_000)
        self.assertEqual(_safe_total_timesteps(20_000_003, 8), 20_000_000)
        self.assertEqual(_safe_total_timesteps(7, 8), 0)

    def test_callback_stops_at_safe_eight_env_boundary(self) -> None:
        target = 20_000_003
        budget = _safe_total_timesteps(target, 8)
        callback = _GuardedFakeCallback(budget)
        while callback.num_timesteps < budget:
            callback.num_timesteps += 8
            keep_training = callback._on_step()
            if not keep_training:
                break
        self.assertEqual(callback.num_timesteps, budget)
        self.assertLessEqual(callback.num_timesteps, target)
        self.assertFalse(callback._on_step())

    def test_invalid_budget_inputs_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            _safe_total_timesteps(-1, 8)
        with self.assertRaises(ValueError):
            _safe_total_timesteps(10, 0)
        with self.assertRaises(ValueError):
            _safe_total_timesteps(True, 8)


if __name__ == "__main__":
    unittest.main()
