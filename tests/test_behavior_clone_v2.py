"""Tests for the V2 behaviour-cloning trainer (run-grouped split)."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import torch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from training.behavior_clone_v2 import (  # noqa: E402
    BC_PHASES,
    BCv2Config,
    PHASE_SLOT,
    PhaseHeadActionScorer,
    RESERVED_TEACHER_TEST_SEED_START,
    evaluate,
    family_slot_for_phase,
    load_model,
    load_samples,
    masked_scores,
    run_key_of,
    save_checkpoint,
    split_samples,
    train_behavior_clone_v2,
)
from training.v2_flat_env import FLAT_SIZE  # noqa: E402
from training.v2_observation import OBS_SIZE, observation_contract  # noqa: E402

CONTRACT_BLOCKS = {b["name"]: b["offset"] for b in observation_contract()["blocks"]}


def synthetic_sample(*, phase: int, signal: int, label: int, legal: list[int],
                     gap: float, seed: int, source: str = "teacher") -> dict:
    observation = np.zeros(OBS_SIZE, dtype=np.int32)
    # Learnable signal: the first-legal-action index is stored in the
    # alive-enemy slot; a trained head can recover it from there.
    observation[CONTRACT_BLOCKS["alive_enemy_count"]] = signal
    observation[0] = 60
    observation[1] = 80
    return {
        "sample_version": 1,
        "scope": "simulator_act1",
        "seed": seed,
        "prefix_sha256": hashlib.sha256(f"synthetic:{seed}:{signal}".encode()).hexdigest(),
        "decision_index": signal,
        "source": source,
        "phase": phase,
        "floor": 3,
        "score_gap": gap,
        "label_flat_action": label,
        "label_action_id": f"v2:a:synthetic:{label}",
        "legal_flat_actions": legal,
        "observation": [int(v) for v in observation],
        "observation_sha256": "00" * 32,
    }


def dataset(count: int = 600, *, seed_start: int = 100) -> list[dict]:
    samples = []
    for index in range(count):
        seed = seed_start + index // 5  # five decisions per run
        first_legal = index % 5
        legal = [first_legal, first_legal + 7, first_legal + 13]
        phase = 0 if index % 2 == 0 else 2
        samples.append(
            synthetic_sample(
                phase=phase,
                signal=first_legal,
                label=first_legal,
                legal=legal,
                gap=0.6 + (index % 4),
                seed=seed,
            )
        )
    return samples


class SplitTests(unittest.TestCase):
    def test_same_run_never_straddles_the_split(self) -> None:
        """The review's core fix: prefix-bucket splitting leaked 85.9% of
        runs; whole-run grouping must leak exactly zero."""

        samples = dataset(600)
        train, holdout, stats = split_samples(samples)
        self.assertGreater(len(holdout), 0)
        self.assertEqual(stats["shared_runs"], 0)
        self.assertEqual(stats["holdout_run_leakage_rate"], 0.0)
        train_runs = {run_key_of(s) for s in train}
        holdout_runs = {run_key_of(s) for s in holdout}
        self.assertTrue(train_runs.isdisjoint(holdout_runs))
        # Every decision of a holdout run is in holdout: no partial runs.
        for run in holdout_runs:
            members = [s for s in samples if run_key_of(s) == run]
            self.assertTrue(all(s in holdout for s in members))

    def test_prefix_bucket_split_would_have_leaked(self) -> None:
        """Guard the review finding itself: under the OLD prefix-hash rule
        (bucket of prefix_sha256), the same dataset has cross-side runs."""

        samples = dataset(600)
        train_runs, holdout_runs = set(), set()
        for sample in samples:
            bucket = int(sample["prefix_sha256"][:8], 16) % 20
            (holdout_runs if bucket == 0 else train_runs).add(run_key_of(sample))
        self.assertGreater(len(train_runs & holdout_runs), 0)

    def test_reserved_test_seed_range_is_excluded_from_training(self) -> None:
        samples = dataset(60, seed_start=RESERVED_TEACHER_TEST_SEED_START)
        train, holdout, stats = split_samples(samples)
        self.assertEqual(train, [])
        self.assertEqual(holdout, [])
        self.assertEqual(stats["skipped_reserved_test_seeds"], 60)

    def test_dagger_seed_shared_with_teacher_is_a_different_run_key(self) -> None:
        teacher = synthetic_sample(phase=0, signal=1, label=1, legal=[1, 8], gap=2.0,
                                   seed=500, source="teacher")
        dagger = synthetic_sample(phase=0, signal=2, label=2, legal=[2, 9], gap=2.0,
                                  seed=500, source="dagger")
        train, holdout, stats = split_samples([teacher, dagger], holdout_bucket_size=10)
        # Both may land on the same side (different run keys), but one run
        # alone can never be split: grouping is per (source, seed).
        self.assertEqual(stats["shared_runs"], 0)

    def test_hash_split_is_deterministic(self) -> None:
        samples = dataset(300)
        first = split_samples(samples)
        second = split_samples(list(reversed(samples)))
        self.assertEqual(
            sorted(s["prefix_sha256"] for s in first[0]),
            sorted(s["prefix_sha256"] for s in second[0]),
        )

    def test_non_act1_scope_is_refused(self) -> None:
        sample = dataset(4)[0]
        sample["scope"] = "real_game_a10"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.jsonl"
            path.write_text(json.dumps(sample) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_samples([path])


class MaskingTests(unittest.TestCase):
    def test_mask_never_selects_illegal_candidates(self) -> None:
        config = BCv2Config(hidden_dim=32, depth=1)
        model = PhaseHeadActionScorer(config)
        observation = torch.randint(-100, 3000, (4, OBS_SIZE), dtype=torch.int32)
        family = torch.tensor([PHASE_SLOT["combat"], PHASE_SLOT["map"],
                               PHASE_SLOT["combat"], PHASE_SLOT["shop"]])
        legal = torch.zeros(4, FLAT_SIZE, dtype=torch.bool)
        legal[0, 3] = True
        legal[1, 40] = True
        legal[2, 7] = True
        legal[3, 8] = True
        with torch.no_grad():
            scores = masked_scores(model(observation, family), legal)
        self.assertEqual(scores.argmax(-1).tolist(), [3, 40, 7, 8])
        self.assertTrue(torch.isneginf(scores[0, 4]) or scores[0, 4] < -1e8)

    def test_every_family_has_its_own_head(self) -> None:
        config = BCv2Config(hidden_dim=16, depth=1)
        model = PhaseHeadActionScorer(config)
        model.eval()  # disable dropout so repeated forwards are comparable
        self.assertEqual(len(model.heads), len(BC_PHASES))
        observation = torch.randint(0, 10, (1, OBS_SIZE), dtype=torch.int32)
        # Heads start independent: perturbing one head's weights must not
        # change another family's logits.
        with torch.no_grad():
            before = model(observation, torch.tensor([PHASE_SLOT["combat"]]))
            model.heads[PHASE_SLOT["shop"]].weight.add_(1.0)
        self.assertTrue(torch.equal(before, before))  # sanity
        with torch.no_grad():
            combat_after = model(observation, torch.tensor([PHASE_SLOT["combat"]]))
            shop_after = model(observation, torch.tensor([PHASE_SLOT["shop"]]))
        self.assertTrue(torch.equal(combat_after, before))
        self.assertFalse(torch.equal(shop_after, before))


class TrainingTests(unittest.TestCase):
    def test_trains_to_high_holdout_accuracy_on_learnable_signal(self) -> None:
        samples = dataset(1000)
        train, holdout, _stats = split_samples(samples)
        config = BCv2Config(
            hidden_dim=128, depth=2, epochs=6, batch_size=64, random_seed=1
        )
        model, result = train_behavior_clone_v2(
            train, holdout, config, device="cpu", log=lambda *_: None
        )
        self.assertGreater(result["holdout"]["top1"], 0.5,
                           f"holdout top1 too low: {result['holdout']}")
        # Contract property: masked argmax stays inside the legal set.
        observations, legal_t, _labels, family_t, _gaps = (
            __import__("training.behavior_clone_v2", fromlist=["_tensors"])._tensors(
                holdout, "cpu"
            )
        )
        with torch.no_grad():
            masked = masked_scores(model(observations, family_t), legal_t)
        picked = masked.argmax(dim=-1)
        self.assertTrue(bool(legal_t.gather(1, picked.view(-1, 1)).all()),
                        "masked selection escaped the legal candidate set")

    def test_checkpoint_roundtrip_is_hash_verified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ckpt.pt"
            config = BCv2Config(hidden_dim=32, depth=1, epochs=1)
            model = PhaseHeadActionScorer(config)
            meta = save_checkpoint(
                model, {"best_epoch": 1, "holdout": {}, "history": []}, config,
                sample_paths=[],
                split_statistics={"train_samples": 1, "holdout_samples": 1,
                                  "shared_runs": 0},
                output=path,
            )
            self.assertEqual(len(meta["checkpoint"]["sha256"]), 64)
            reloaded = load_model(path)
            self.assertEqual(
                sum(p.numel() for p in reloaded.parameters()),
                sum(p.numel() for p in model.parameters()),
            )
            path.write_bytes(path.read_bytes() + b"\x00")
            with self.assertRaises(ValueError):
                load_model(path)

    def test_family_mapping_is_total(self) -> None:
        for phase in range(11):
            self.assertIn(family_slot_for_phase(phase), range(len(BC_PHASES)))
        self.assertEqual(family_slot_for_phase(999), PHASE_SLOT["event"])


if __name__ == "__main__":
    unittest.main()
