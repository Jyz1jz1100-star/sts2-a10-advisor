from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .seeds import seed_digest
from .wilson import wilson_interval

METRICS_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class EpisodeMetric:
    seed: int
    won: bool
    terminated: bool
    truncated: bool
    steps: int
    episode_return: float
    illegal_actions: int
    final_floor: int | None = None
    encounter: str | None = None


@dataclass(frozen=True)
class EvaluationMetrics:
    schema_version: int
    generated_at: str
    stage: str
    split: str
    scope: str
    experimental: bool
    checkpoint: str
    checkpoint_sha256: str | None
    deterministic: bool
    seed_count: int
    seed_sha256: str
    episodes: int
    wins: int
    win_rate: float
    wilson_95_low: float
    wilson_95_high: float
    truncations: int
    truncation_rate: float
    illegal_actions: int
    mean_steps: float
    mean_return: float
    mean_final_floor: float | None
    max_final_floor: int | None
    by_encounter: dict[str, dict[str, float | int]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def summarize_episodes(
    episodes: list[EpisodeMetric],
    *,
    stage: str,
    split: str,
    scope: str,
    checkpoint: str,
    deterministic: bool,
    experimental: bool = False,
) -> EvaluationMetrics:
    if not episodes:
        raise ValueError("cannot summarize an empty evaluation")
    wins = sum(int(episode.won) for episode in episodes)
    truncations = sum(int(episode.truncated) for episode in episodes)
    illegal_actions = sum(episode.illegal_actions for episode in episodes)
    low, high = wilson_interval(wins, len(episodes))
    floors = [
        episode.final_floor for episode in episodes if episode.final_floor is not None
    ]
    encounter_buckets: dict[str, list[EpisodeMetric]] = {}
    for episode in episodes:
        if episode.encounter is not None:
            encounter_buckets.setdefault(episode.encounter, []).append(episode)
    by_encounter: dict[str, dict[str, float | int]] = {}
    for encounter, bucket in sorted(encounter_buckets.items()):
        bucket_wins = sum(int(episode.won) for episode in bucket)
        by_encounter[encounter] = {
            "episodes": len(bucket),
            "wins": bucket_wins,
            "win_rate": bucket_wins / len(bucket),
            "mean_steps": sum(episode.steps for episode in bucket) / len(bucket),
            "mean_return": sum(episode.episode_return for episode in bucket)
            / len(bucket),
        }
    seeds = [episode.seed for episode in episodes]
    checkpoint_path = Path(checkpoint)
    checkpoint_sha256 = None
    if checkpoint_path.is_file():
        digest = hashlib.sha256()
        with checkpoint_path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        checkpoint_sha256 = digest.hexdigest()
    return EvaluationMetrics(
        schema_version=METRICS_SCHEMA_VERSION,
        generated_at=datetime.now(UTC).isoformat(),
        stage=stage,
        split=split,
        scope=scope,
        experimental=experimental,
        checkpoint=checkpoint,
        checkpoint_sha256=checkpoint_sha256,
        deterministic=deterministic,
        seed_count=len(seeds),
        seed_sha256=seed_digest(seeds),
        episodes=len(episodes),
        wins=wins,
        win_rate=wins / len(episodes),
        wilson_95_low=low,
        wilson_95_high=high,
        truncations=truncations,
        truncation_rate=truncations / len(episodes),
        illegal_actions=illegal_actions,
        mean_steps=sum(episode.steps for episode in episodes) / len(episodes),
        mean_return=sum(episode.episode_return for episode in episodes) / len(episodes),
        mean_final_floor=(sum(floors) / len(floors) if floors else None),
        max_final_floor=(max(floors) if floors else None),
        by_encounter=by_encounter,
    )


def atomic_write_json(path: Path, payload: dict) -> None:
    """Write JSON via temp+replace with sharing-violation retries.

    On Windows, os.replace fails with AccessDenied while any reader holds the
    target open. Monitoring tools legitimately read these files, so transient
    denials must never kill a multi-day training run (supervisor crash of
    2026-09-01T06:23Z motivated this retry loop).
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    delay = 0.25
    for attempt in range(8):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 8.0)
