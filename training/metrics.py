from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .seeds import seed_digest
from .wilson import wilson_interval

METRICS_SCHEMA_VERSION = 6


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
    #: Which act the emulator generated for this seed. RunMapGenerator chooses it by
    #: coin flip, so an evaluation configured as "act1" really spans two acts unless
    #: this is recorded; without it the aggregate cannot say which act a win was in.
    act: int | None = None
    encounter: str | None = None
    #: V2 curriculum stages promote on reaching their floor boundary (or a
    #: true terminal win), not on run completion alone.
    boundary_reached: bool = False
    #: Classified simulator dead-end label (empty_action_mask /
    #: native_rejection / rejected_to_exhaustion).  Unclassified truncations
    #: must stay at zero, so the evaluator records the reason for every
    #: labelled environment defect.
    dead_end_reason: str | None = None
    #: Native mask-vs-engine rejections the filter absorbed during the
    #: episode (emulator anomalies, counted separately from policy quality).
    rejection_events: int = 0
    #: Player HP fraction at the episode's end state (0.0 when the run ended
    #: in death, 1.0 impossible to lose HP on).  Feeds the joint ablation
    #: metric ``mean_final_hp_fraction``; None when the simulator did not
    #: expose HP (defensive; the native stack always does).
    final_hp_fraction: float | None = None


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
    boundary_rate: float = 0.0
    #: Wilson 95% lower bound on the boundary rate itself (review item 4):
    #: the floor6 gate requires point >= 0.93 AND this bound >= 0.90.
    boundary_wilson_95_low: float = 0.0
    boundary_wilson_95_high: float = 0.0
    #: Number of episodes that reached the boundary (or won); the raw count
    #: behind boundary_rate so audits can recompute the Wilson interval.
    boundary_hits: int = 0
    #: Mean HP fraction at episode end (None when HP was never exposed).
    mean_final_hp_fraction: float | None = None
    dead_end_reasons: dict[str, int] = field(default_factory=dict)
    #: Terminal-floor counts. ``mean_final_floor`` and ``max_final_floor`` cannot
    #: separate "almost every run dies at floor 8" from "half die at 6, half at
    #: the boss", and which of those holds decides whether the Act 1 wall is
    #: mid-run attrition or the final fight. An empty mapping means no episode
    #: reported a floor at all -- metrics written before schema 4 simply lack the
    #: key, so absence is never read as "zero runs reached that floor".
    final_floor_histogram: dict[int, int] = field(default_factory=dict)
    #: Truncated episodes carrying neither a simulator dead-end label nor a
    #: curriculum boundary.  V2 promotion requires this to be zero: every
    #: abnormal ending must be attributable.
    unclassified_dead_ends: int = 0
    #: Truncations excluding successful curriculum-boundary hits.  The V1
    #: gate reads this under the name ``max_truncation_rate``; for stages
    #: without a floor boundary it equals ``truncation_rate`` exactly.
    defect_truncation_rate: float = 0.0
    #: Total absorbed native rejections across the evaluation (emulator
    #: anomaly volume, reported separately from policy-quality gates).
    rejection_events: int = 0
    by_encounter: dict[str, dict[str, float | int]] = field(default_factory=dict)
    #: Per-act breakdown of the same evaluation. An ``act1`` stage spans both emulator
    #: acts because the generator picks the act per seed, so this is the only field
    #: that lets a win rate be attributed to one act instead of the mix.
    by_act: dict[str, dict[str, float | int | None]] = field(default_factory=dict)
    #: Seeds that produced a terminal win. A rate over thousands of episodes cannot
    #: be re-checked episode by episode; this list can, and "reproduce one reviewable
    #: win" is the thing the acceptance chain actually asks for.
    winning_seeds: list[int] = field(default_factory=list)

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
    boundary_hits = sum(int(episode.boundary_reached) for episode in episodes)
    # Boundary hits that are truncations (stage-completion).  Terminal wins
    # also satisfy boundary_reached, so subtracting boundary_hits from
    # truncations would go negative on the Act-1 stage; count the intersection
    # explicitly instead.
    boundary_truncations = sum(
        int(episode.truncated and episode.boundary_reached) for episode in episodes
    )
    illegal_actions = sum(episode.illegal_actions for episode in episodes)
    rejection_events = sum(episode.rejection_events for episode in episodes)
    dead_end_reasons: dict[str, int] = {}
    for episode in episodes:
        if episode.dead_end_reason is not None:
            dead_end_reasons[episode.dead_end_reason] = (
                dead_end_reasons.get(episode.dead_end_reason, 0) + 1
            )
    unclassified_dead_ends = sum(
        int(
            episode.truncated
            and episode.dead_end_reason is None
            and not episode.boundary_reached
        )
        for episode in episodes
    )
    low, high = wilson_interval(wins, len(episodes))
    boundary_low, boundary_high = wilson_interval(boundary_hits, len(episodes))
    floors = [
        episode.final_floor for episode in episodes if episode.final_floor is not None
    ]
    final_floor_histogram = {floor: floors.count(floor) for floor in sorted(set(floors))}
    hp_fractions = [
        episode.final_hp_fraction
        for episode in episodes
        if episode.final_hp_fraction is not None
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
    act_buckets: dict[int, list[EpisodeMetric]] = {}
    for episode in episodes:
        if episode.act is not None:
            act_buckets.setdefault(int(episode.act), []).append(episode)
    by_act: dict[str, dict[str, float | int | None]] = {}
    for act, bucket in sorted(act_buckets.items()):
        bucket_wins = sum(int(episode.won) for episode in bucket)
        bucket_floors = [episode.final_floor for episode in bucket
                         if episode.final_floor is not None]
        by_act[str(act)] = {
            "episodes": len(bucket),
            "wins": bucket_wins,
            "win_rate": bucket_wins / len(bucket),
            "mean_final_floor": (sum(bucket_floors) / len(bucket_floors)
                                 if bucket_floors else None),
            "max_final_floor": max(bucket_floors) if bucket_floors else None,
            "winning_seeds": [episode.seed for episode in bucket if episode.won],
        }
    seeds = [episode.seed for episode in episodes]
    winning_seeds = [episode.seed for episode in episodes if episode.won]
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
        boundary_rate=boundary_hits / len(episodes),
        boundary_wilson_95_low=boundary_low,
        boundary_wilson_95_high=boundary_high,
        boundary_hits=boundary_hits,
        mean_final_hp_fraction=(
            sum(hp_fractions) / len(hp_fractions) if hp_fractions else None
        ),
        dead_end_reasons=dict(sorted(dead_end_reasons.items())),
        final_floor_histogram=final_floor_histogram,
        unclassified_dead_ends=unclassified_dead_ends,
        defect_truncation_rate=(truncations - boundary_truncations) / len(episodes),
        rejection_events=rejection_events,
        by_encounter=by_encounter,
        by_act=by_act,
        winning_seeds=winning_seeds,
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
