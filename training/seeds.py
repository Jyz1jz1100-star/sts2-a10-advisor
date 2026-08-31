from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SeedPartition:
    name: str
    start: int
    count: int

    @property
    def stop(self) -> int:
        return self.start + self.count

    def contains(self, seed: int) -> bool:
        return self.start <= seed < self.stop

    def seeds(self, count: int | None = None) -> list[int]:
        requested = self.count if count is None else count
        if requested < 0 or requested > self.count:
            raise ValueError(
                f"partition {self.name!r} contains {self.count} seeds, "
                f"cannot provide {requested}"
            )
        return list(range(self.start, self.start + requested))

    def shard(self, index: int, total: int) -> "SeedPartition":
        if total <= 0 or index < 0 or index >= total:
            raise ValueError("require total > 0 and 0 <= index < total")
        base, remainder = divmod(self.count, total)
        count = base + int(index < remainder)
        if count == 0:
            raise ValueError(
                f"partition {self.name!r} is too small for {total} worker shards"
            )
        offset = index * base + min(index, remainder)
        return SeedPartition(
            name=f"{self.name}/worker-{index}",
            start=self.start + offset,
            count=count,
        )


class SeedPartitions:
    def __init__(self, partitions: list[SeedPartition]):
        if not partitions:
            raise ValueError("at least one seed partition is required")
        names = [partition.name for partition in partitions]
        if len(names) != len(set(names)):
            raise ValueError("seed partition names must be unique")
        for partition in partitions:
            if partition.start < 0 or partition.count <= 0:
                raise ValueError(
                    "seed partition start must be non-negative and count positive"
                )
        ordered = sorted(partitions, key=lambda partition: partition.start)
        for left, right in zip(ordered, ordered[1:]):
            if left.stop > right.start:
                raise ValueError(
                    f"seed partitions {left.name!r} and {right.name!r} overlap"
                )
        self._partitions = {partition.name: partition for partition in partitions}

    def __getitem__(self, name: str) -> SeedPartition:
        return self._partitions[name]

    def as_list(self) -> list[SeedPartition]:
        return list(self._partitions.values())


class SeedStream:
    """Deterministic no-replacement stream within one reserved partition."""

    def __init__(self, partition: SeedPartition, namespace: str):
        self.partition = partition
        self.namespace = namespace
        digest = hashlib.blake2b(namespace.encode("utf-8"), digest_size=16).digest()
        candidate = int.from_bytes(digest[:8], "big") % partition.count
        while math.gcd(candidate, partition.count) != 1:
            candidate = (candidate + 1) % partition.count
        self._multiplier = candidate
        self._offset = int.from_bytes(digest[8:], "big") % partition.count
        self._index = 0

    def next(self) -> int:
        if self._index >= self.partition.count:
            raise RuntimeError(
                f"seed partition {self.partition.name!r} exhausted after "
                f"{self.partition.count} episodes"
            )
        seed = self.partition.start + (
            (self._multiplier * self._index + self._offset) % self.partition.count
        )
        self._index += 1
        return seed


def seed_digest(seeds: list[int]) -> str:
    encoded = ",".join(str(seed) for seed in seeds).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()
