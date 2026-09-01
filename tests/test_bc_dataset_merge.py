"""Tests for the BC sample merge tool."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from training.bc_dataset_merge import merge_sample_files  # noqa: E402


def sample(prefix: str, index: int, label: int, source: str = "teacher") -> dict:
    return {
        "sample_version": 1,
        "scope": "simulator_act1",
        "seed": 1,
        "prefix_sha256": prefix,
        "decision_index": index,
        "source": source,
        "phase": 0,
        "floor": 2,
        "score_gap": 1.0,
        "label_flat_action": label,
        "label_action_id": f"a{label}",
        "legal_flat_actions": [label, label + 1],
        "observation": [0] * 8,
        "observation_sha256": "00" * 32,
    }


class MergeTests(unittest.TestCase):
    def _write(self, directory: Path, name: str, samples: list[dict]) -> Path:
        path = directory / name
        path.write_text(
            "\n".join(json.dumps(s) for s in samples) + "\n", encoding="utf-8"
        )
        return path

    def test_dedupes_same_state_and_keeps_union(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = self._write(root, "a.jsonl", [
                sample("aa", 0, 5), sample("aa", 1, 7), sample("bb", 0, 2)])
            b = self._write(root, "b.jsonl", [
                sample("aa", 1, 7, source="dagger"), sample("cc", 0, 9)])
            out = root / "merged.jsonl"
            summary = merge_sample_files([a, b], out)
            self.assertEqual(summary["merged_samples"], 4)
            self.assertEqual(summary["duplicate_states"], 1)
            lines = [json.loads(x) for x in out.read_text().splitlines()]
            self.assertEqual(
                sorted((s["prefix_sha256"], s["decision_index"]) for s in lines),
                [("aa", "0"), ("aa", "1"), ("bb", "0"), ("cc", "0")],
            )
            self.assertEqual(summary["source_counts"], {"dagger": 1, "teacher": 3})

    def test_conflicting_labels_refuse_to_merge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = self._write(root, "a.jsonl", [sample("aa", 0, 5)])
            b = self._write(root, "b.jsonl", [sample("aa", 0, 6, source="dagger")])
            with self.assertRaises(ValueError):
                merge_sample_files([a, b], root / "out.jsonl")


if __name__ == "__main__":
    unittest.main()
