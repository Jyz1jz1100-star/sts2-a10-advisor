from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

try:
    import torch
except ModuleNotFoundError:  # The small inference/runtime environment is torch-free.
    torch = None

if torch is not None:
    from training.behavior_clone import (
        BCConfig,
        PhaseActionScoringNetwork,
        load_checkpoint,
        load_trace_examples,
        save_checkpoint,
        train_behavior_clone,
    )
from training.trace_contract import CURRENT_PUBLIC_BETA_BUILD


def record(step: int, phase: str, chosen: str) -> dict:
    actions = [
        {"action_id": f"{phase}:left", "action_type": "choose"},
        {"action_id": f"{phase}:right", "action_type": "choose"},
    ]
    return {
        "trace_version": 1,
        "run_id": "synthetic-run",
        "decision_id": f"synthetic-run:{step}",
        "step": step,
        "split": "train",
        "build": CURRENT_PUBLIC_BETA_BUILD,
        "seed": "SYNTHETIC-TRAIN-1",
        "character": "IRONCLAD",
        "ascension": 10,
        "save_load_used": False,
        "visible_state": {
            "state_type": phase,
            "signal": "choose-left" if chosen.endswith("left") else "choose-right",
            "hp": 70 - step,
        },
        "legal_actions": actions,
        "chosen_action": next(action for action in actions if action["action_id"] == chosen),
        "result": {"status": "applied", "observed": True},
    }


@unittest.skipIf(torch is None, "behavior-clone tests require the training environment")
class BehaviorCloneTests(unittest.TestCase):
    def test_network_scores_only_supplied_legal_actions(self) -> None:
        model = PhaseActionScoringNetwork(
            BCConfig(feature_dim=32, hidden_dim=16, epochs=1)
        )
        legal = [
            {"action_id": "legal:a", "action_type": "play_card"},
            {"action_id": "legal:b", "action_type": "end_turn"},
        ]
        chosen, scores = model.choose_legal_action(
            {"state_type": "combat", "hp": 50}, "combat", legal
        )
        self.assertEqual(scores.shape, torch.Size([2]))
        self.assertIn(chosen, legal)
        with self.assertRaises(ValueError):
            model.score_legal_actions({}, "combat", [])

    def test_loader_rejects_unvalidated_trace(self) -> None:
        bad = record(1, "combat", "combat:left")
        bad["ascension"] = 9
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bad.jsonl"
            path.write_text(json.dumps(bad) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "trace validation failed"):
                load_trace_examples([path])

    def test_cpu_synthetic_training_and_checkpoint_provenance(self) -> None:
        rows = []
        for step in range(12):
            phase = "combat" if step % 2 == 0 else "map"
            suffix = "left" if step % 3 else "right"
            rows.append(record(step, phase, f"{phase}:{suffix}"))

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            trace = root / "train.jsonl"
            trace.write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )
            examples = load_trace_examples([trace])
            config = BCConfig(
                feature_dim=32,
                hidden_dim=16,
                learning_rate=5e-3,
                epochs=2,
                random_seed=7,
            )
            model, losses = train_behavior_clone(examples, config, device="cpu")
            checkpoint = root / "synthetic.pt"
            metadata = save_checkpoint(
                model,
                checkpoint,
                trace_paths=[trace],
                example_count=len(examples),
                phase_counts={"combat": 6, "map": 6},
                losses=losses,
                expected_build=CURRENT_PUBLIC_BETA_BUILD,
            )
            restored = load_checkpoint(checkpoint)
            legal = examples[0].legal_actions
            selected, scores = restored.choose_legal_action(
                examples[0].visible_state, examples[0].phase, legal
            )

        self.assertEqual(len(losses), 2)
        self.assertTrue(all(torch.isfinite(torch.tensor(losses))))
        self.assertEqual(metadata["checkpoint_version"], 1)
        self.assertEqual(metadata["training"]["random_seed"], 7)
        self.assertEqual(metadata["target"]["build"], CURRENT_PUBLIC_BETA_BUILD)
        self.assertEqual(len(metadata["dataset"]["aggregate_sha256"]), 64)
        self.assertEqual(len(metadata["checkpoint"]["sha256"]), 64)
        self.assertEqual(scores.numel(), len(legal))
        self.assertIn(selected, legal)


if __name__ == "__main__":
    unittest.main()
