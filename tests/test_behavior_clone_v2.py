"""Tests for the V2 behaviour-cloning trainer (synthetic samples)."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

import numpy as np
import torch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from training.behavior_clone_v2 import (  # noqa: E402
    BCv2Config,
    PhaseSplitActionScorer,
    load_samples,
    masked_scores,
    train_behavior_clone_v2,
)
from training.v2_flat_env import FLAT_SIZE  # noqa: E402
from training.v2_observation import OBS_SIZE, observation_contract  # noqa: E402

CONTRACT_BLOCKS = {b["name"]: b["offset"] for b in observation_contract()["blocks"]}


def synthetic_sample(*, combat: bool, signal: int, label: int, legal: list[int],
                     gap: float, identity: int) -> dict:
    observation = np.zeros(OBS_SIZE, dtype=np.int32)
    # A learnable signal: put the label value into the alive-enemy slot;
    # the head must discover "predict the value stored there".
    observation[CONTRACT_BLOCKS["alive_enemy_count"]] = signal
    observation[0] = 60
    observation[1] = 80
    prefix_sha = hashlib.sha256(f"synthetic:{identity}".encode()).hexdigest()
    return {
        "sample_version": 1,
        "scope": "simulator_act1",
        "seed": 1400100000 + signal,
        "prefix_sha256": prefix_sha,
        "phase": 0 if combat else 2,
        "floor": 3,
        "score_gap": gap,
        "label_flat_action": label,
        "label_action_id": f"v2:a:synthetic:{label}",
        "legal_flat_actions": legal,
        "observation": [int(v) for v in observation],
        "observation_sha256": "00" * 32,
    }


def dataset(count: int = 600) -> list[dict]:
    """Each sample's legal label is recoverable from the observation slot.

    Label = first legal action; the observation stores the *index* of the
    first legal action so a trained head can learn the mapping.
    """

    samples = []
    for index in range(count):
        first_legal = index % 5
        legal = [first_legal, first_legal + 7, first_legal + 13]
        combat = index % 2 == 0
        samples.append(
            synthetic_sample(
                combat=combat,
                signal=first_legal,
                label=first_legal,
                legal=legal,
                gap=0.6 + (index % 4),
                identity=index,
            )
        )
    return samples


class SplitTests(unittest.TestCase):
    def test_hash_split_is_deterministic_and_disjoint(self) -> None:
        path = Path("_tmp_bc_samples.jsonl")
        samples = dataset()
        path.write_text(
            "\n".join(json.dumps(s) for s in samples) + "\n", encoding="utf-8"
        )
        try:
            train_a, holdout_a = load_samples([path])
            train_b, holdout_b = load_samples([path])
            self.assertEqual(
                [s["prefix_sha256"] for s in train_a],
                [s["prefix_sha256"] for s in train_b],
            )
            self.assertEqual(
                [s["prefix_sha256"] for s in holdout_a],
                [s["prefix_sha256"] for s in holdout_b],
            )
            self.assertTrue(
                set(s["prefix_sha256"] for s in train_a).isdisjoint(
                    s["prefix_sha256"] for s in holdout_a
                )
            )
            self.assertEqual(len(train_a) + len(holdout_a), len(samples))
            self.assertGreater(len(holdout_a), 0)
        finally:
            path.unlink(missing_ok=True)

    def test_non_act1_scope_is_refused(self) -> None:
        sample = dataset(4)[0]
        sample["scope"] = "real_game_a10"
        path = Path("_tmp_bc_bad.jsonl")
        path.write_text(json.dumps(sample) + "\n", encoding="utf-8")
        try:
            with self.assertRaises(ValueError):
                load_samples([path])
        finally:
            path.unlink(missing_ok=True)


class MaskingTests(unittest.TestCase):
    def test_mask_never_selects_illegal_candidates(self) -> None:
        config = BCv2Config(hidden_dim=32, depth=1)
        model = PhaseSplitActionScorer(config)
        observation = torch.randint(-100, 3000, (4, OBS_SIZE), dtype=torch.int32)
        combat = torch.tensor([True, False, True, False])
        legal = torch.zeros(4, FLAT_SIZE, dtype=torch.bool)
        legal[0, 3] = True
        legal[1, 40] = True
        legal[2, 7] = True
        legal[3, 8] = True
        with torch.no_grad():
            scores = masked_scores(model(observation, combat), legal)
        self.assertEqual(scores.argmax(-1).tolist(), [3, 40, 7, 8])
        self.assertTrue(torch.isneginf(scores[0, 4]) or scores[0, 4] < -1e8)


class TrainingTests(unittest.TestCase):
    def test_trains_to_high_holdout_accuracy_on_learnable_signal(self) -> None:
        samples = dataset(800)
        # Force split coverage: ensure both sides exist for this synthetic
        # prefix set (bucket 0 is rare in 800 -> override deterministically).
        from training.behavior_clone_v2 import _hash_bucket

        train = [s for s in samples if _hash_bucket(s["prefix_sha256"]) != 0]
        holdout = [s for s in samples if _hash_bucket(s["prefix_sha256"]) == 0]
        if not holdout:  # synthetic fallback: last 40 samples
            train, holdout = samples[:-40], samples[-40:]
        config = BCv2Config(
            hidden_dim=128, depth=2, epochs=6, batch_size=64, random_seed=1
        )
        model, result = train_behavior_clone_v2(train, holdout, config,
                                                device="cpu", log=lambda *_: None)
        top1 = result["holdout"]["top1"]
        self.assertGreater(top1, 0.5, f"holdout top1 too low: {result['holdout']}")
        # Contract property: masked argmax is always inside the legal set
        # (illegal logits are pinned at -1e9, so any real score wins), no
        # matter how the raw network scores the state.
        from training.behavior_clone_v2 import _tensors

        observations, legal_t, _labels, combat_t, _gaps = _tensors(holdout, "cpu")
        with torch.no_grad():
            masked = masked_scores(model(observations, combat_t), legal_t)
        picked = masked.argmax(dim=-1)
        self.assertTrue(bool(legal_t.gather(1, picked.view(-1, 1)).all()),
                        "masked selection escaped the legal candidate set")

    def test_checkpoint_roundtrip_is_hash_verified(self) -> None:
        from training.behavior_clone_v2 import load_model, save_checkpoint

        config = BCv2Config(hidden_dim=32, depth=1, epochs=1)
        model = PhaseSplitActionScorer(config)
        path = Path("_tmp_bc_ckpt.pt")
        try:
            meta = save_checkpoint(
                model, {"best_epoch": 1, "holdout": {}, "history": []}, config,
                sample_paths=[], train_count=1, holdout_count=1, output=path,
            )
            self.assertEqual(len(meta["checkpoint"]["sha256"]), 64)
            reloaded = load_model(path)
            self.assertEqual(
                sum(p.numel() for p in reloaded.parameters()),
                sum(p.numel() for p in model.parameters()),
            )
            # Tamper detection.
            path.write_bytes(path.read_bytes() + b"\x00")
            with self.assertRaises(ValueError):
                load_model(path)
        finally:
            path.unlink(missing_ok=True)
            path.with_suffix(".pt.metadata.json").unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
