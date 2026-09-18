"""Guards on the checkpoint-series comparator.

The tool exists to make win rates comparable across training steps, so its two
failure modes are (1) measuring on the split the weights were trained on and
(2) silently falling back to reused or invented seeds when the requested episode
count exceeds the declared partition.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from scripts.evaluate_checkpoint_series import _partition_seeds, reject_unusable_split


class SplitGuardTests(unittest.TestCase):
    def test_train_split_is_refused(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            reject_unusable_split("train")
        self.assertIn("fit on", str(caught.exception))

    def test_unseen_splits_are_accepted(self) -> None:
        for split in ("checkpoint", "promotion", "final"):
            reject_unusable_split(split)  # must not raise


class PartitionSeedTests(unittest.TestCase):
    def test_exact_partition_size_is_allowed(self) -> None:
        partition = SimpleNamespace(start=130020000, count=3, seeds=None)
        self.assertEqual(_partition_seeds(partition, 3), [130020000, 130020001, 130020002])

    def test_oversized_request_refuses_instead_of_recycling(self) -> None:
        partition = SimpleNamespace(start=130020000, count=10, seeds=None)
        with self.assertRaises(SystemExit) as caught:
            _partition_seeds(partition, 11)
        message = str(caught.exception)
        self.assertIn("11", message)
        self.assertIn("10", message)

    def test_callable_seeds_take_precedence_over_the_range(self) -> None:
        partition = SimpleNamespace(
            start=999, count=999, seeds=lambda: [7, 8, 9])
        self.assertEqual(_partition_seeds(partition, 2), [7, 8])


if __name__ == "__main__":
    unittest.main()
