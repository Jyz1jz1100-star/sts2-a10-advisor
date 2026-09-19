"""Train seed lists: the guard that keeps an act-filtered arm inside its partition."""

from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from training.seeds import ListSeedStream, SeedList, SeedPartition, seed_digest
from training.v2_config import load_v2_training_config
from training.v2_curriculum import load_train_seed_list

PARTITION = SeedPartition(name="act1.train", start=258_000_000, count=1_000)


def _stage_with(payload: dict, tmp: Path):
    config = load_v2_training_config(
        Path(__file__).resolve().parents[1] / "config/training_v2.toml"
    )
    stage = next(s for s in config.stages if s.name == "act1")
    path = tmp / "seeds.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return dataclasses.replace(stage, train_seeds_file=str(path))


class SeedListTest(unittest.TestCase):
    def test_rejects_empty_and_duplicate_lists(self):
        with self.assertRaises(ValueError):
            SeedList(name="x", seeds=())
        with self.assertRaises(ValueError):
            SeedList(name="x", seeds=(1, 1))

    def test_shards_stride_and_carry_the_act_label(self):
        seeds = tuple(range(PARTITION.start, PARTITION.start + 12))
        listing = SeedList(name="x", seeds=seeds, generated_act=1)
        shard = listing.shard(1, 3)
        self.assertEqual(shard.seeds, seeds[1::3])
        self.assertEqual(shard.generated_act, 1)
        union = set()
        for index in range(3):
            union.update(listing.shard(index, 3).seeds)
        self.assertEqual(union, set(seeds))

    def test_stream_is_repeatable_disjoint_and_exhausts_loudly(self):
        listing = SeedList(
            name="x", seeds=tuple(range(PARTITION.start, PARTITION.start + 64)),
            generated_act=1,
        )
        first = ListSeedStream(listing, "v2:act1:worker:0")
        draws = [first.next() for _ in range(64)]
        self.assertEqual(len(set(draws)), 64, "a seed was reused inside one episode budget")
        second = ListSeedStream(listing, "v2:act1:worker:0")
        self.assertEqual([second.next() for _ in range(10)], draws[:10])
        with self.assertRaises(RuntimeError):
            for _ in range(64):
                first.next()


class LoadTrainSeedListTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_accepts_a_subset_and_reports_its_digest(self):
        seeds = [PARTITION.start, PARTITION.start + 5]
        stage = _stage_with({"generated_act": 1, "seeds": seeds}, self.tmp)
        listing = load_train_seed_list(stage, PARTITION)
        self.assertEqual(listing.generated_act, 1)
        self.assertEqual(listing.count, 2)
        self.assertEqual(listing.digest, seed_digest(seeds))

    def test_rejects_a_seed_outside_the_train_partition(self):
        # The fan-out's disjointness and teacher-reservation guarantees are
        # properties of the partitions; a list that escapes them breaks both.
        stage = _stage_with(
            {"generated_act": 1, "seeds": [PARTITION.start, PARTITION.stop]},
            self.tmp,
        )
        with self.assertRaisesRegex(ValueError, "outside"):
            load_train_seed_list(stage, PARTITION)

    def test_requires_the_list_to_say_which_act_it_is(self):
        for payload in (
            {"seeds": [PARTITION.start]},
            {"generated_act": 3, "seeds": [PARTITION.start]},
        ):
            with self.subTest(payload=payload):
                stage = _stage_with(payload, self.tmp)
                with self.assertRaisesRegex(ValueError, "generated_act"):
                    load_train_seed_list(stage, PARTITION)

    def test_rejects_an_empty_list(self):
        stage = _stage_with({"generated_act": 1, "seeds": []}, self.tmp)
        with self.assertRaisesRegex(ValueError, "non-empty"):
            load_train_seed_list(stage, PARTITION)


if __name__ == "__main__":
    unittest.main()
