"""Batch generation of high-confidence prefix-replay teacher labels.

This is the bridge between the three V2 foundation modules and distillation:

* states are visited by a *behavioural traversal* policy over the real
  emulator (random legal actions with seed-partition rotation), not only by
  teacher-optimal trajectories, so coverage includes the map/event/shop
  decisions the eventual student must survive;
* interesting states (elite/boss combat, low HP, multi-enemy target choices,
  and every run-level non-combat decision) are labelled by comparing all
  legal ``(action, target)`` candidates with the deterministic prefix-replay
  teacher under a strictly bounded rollout;
* every record carries the emulator hash, search budget, best-vs-runner-up
  score gap, prefix replay hash, and a re-verified replay proof.  Records
  whose replay verification fails are never written.

Only decisions whose gap clears ``min_score_gap`` are exported as
``high_confidence``.  Labels are ``simulator_act1`` scoped and are explicitly
not real-game evidence.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterator, Sequence

import numpy as np

from advisor_core.action_codec_v2 import (
    ACTION_ID_VERSION,
    ActionTargetCodecV2,
    EnemyTarget,
    stable_action_id,
)
from advisor_core.card_targeting_v2 import is_single_target

from .prefix_replay_teacher import (
    SCOPE,
    SIMULATOR_DISCLAIMER,
    ActionTarget,
    ContinuationPolicy,
    ReplayPrefix,
    RolloutBudget,
    capture_prefix,
    score_candidates,
    state_hashes,
)
from .v2_constants import COMBAT_OBS_SIZE, MAP_CHOICES, MAX_ENEMIES, MAX_HAND

TEACHER_RECORD_VERSION = 2


# --------------------------------------------------------------------------
# candidate enumeration shared with the flat training action space
# --------------------------------------------------------------------------


def alive_enemy_indices(raw_obs: Sequence[int]) -> list[int]:
    obs = np.asarray(raw_obs)
    enemy_base = 54
    alive: list[int] = []
    for slot in range(MAX_ENEMIES):
        hp = int(obs[enemy_base + slot * 15])
        max_hp = int(obs[enemy_base + slot * 15 + 1])
        if max_hp > 0 and hp > 0:
            alive.append(slot)
    return alive


def hand_def_ids(raw_obs: Sequence[int]) -> list[int]:
    obs = np.asarray(raw_obs)
    return [int(obs[8 + index * 2]) for index in range(MAX_HAND)]


def codec_for_state(
    raw_obs: Sequence[int],
    base_mask: Sequence[int],
    *,
    encounter_id: int,
) -> ActionTargetCodecV2:
    """Candidate codec consistent with :class:`V2FlatActionEnv.codec`."""

    obs = np.asarray(raw_obs)
    phase = int(obs[COMBAT_OBS_SIZE])
    alive = alive_enemy_indices(obs)
    keys: dict[int, str] = {}
    if phase == 0:
        hand = hand_def_ids(obs)
        hand_count = sum(1 for def_id in hand if def_id)
        for index, def_id in enumerate(hand):
            if def_id:
                keys[index] = f"card:{def_id}@h{index}"
        keys[hand_count] = "end_turn"
        for slot in range(3):
            potion = int(obs[28 + slot * 2])
            if potion:
                keys[hand_count + 1 + slot] = f"potion:{potion}"
    targeted = set()
    if phase == 0 and len(alive) >= 2:
        for index, def_id in enumerate(hand_def_ids(obs)):
            if def_id and is_single_target(def_id) and bool(base_mask[index]):
                targeted.add(index)
    targets = tuple(
        EnemyTarget(index, f"{int(encounter_id)}/enemy-{index}") for index in alive
    )
    return ActionTargetCodecV2(
        [bool(value) for value in base_mask], sorted(targeted), targets, action_keys=keys
    )


# --------------------------------------------------------------------------
# continuation (rollout) policy and traversal policy
# --------------------------------------------------------------------------


def heuristic_continuation(
    observation: Any,
    mask: Sequence[bool],
    info: Any,
    step_index: int,
) -> ActionTarget:
    """Deterministic rollout policy: play the cheapest legal thing.

    In combat that means the lowest legal hand slot (attacks first because
    ValidActions keeps hand order), then end turn, then the first legal
    non-combat option.  It is deliberately *not* the teacher: rollouts only
    need to be stable and to terminate, while the root comparison does the
    actual searching.
    """

    legal = [index for index, value in enumerate(mask) if value]
    if not legal:
        return ActionTarget(0, -1)
    obs = np.asarray(observation)
    phase = int(obs[COMBAT_OBS_SIZE])
    if phase == 0:
        hand_count = sum(1 for def_id in hand_def_ids(obs) if def_id)
        for action in legal:
            if action < hand_count:
                def_id = int(obs[8 + action * 2])
                target = 0 if is_single_target(def_id) else -1
                return ActionTarget(action, target)
        return ActionTarget(hand_count, -1)  # end turn
    return ActionTarget(legal[0], -1)


class TraversalDecision:
    """One visited decision state during behavioural traversal."""

    def __init__(
        self,
        *,
        seed: int | str,
        decisions: list[ActionTarget],
        raw_obs: np.ndarray,
        base_mask: np.ndarray,
        info: dict[str, Any],
    ) -> None:
        self.seed = seed
        self.decisions = list(decisions)
        self.raw_obs = raw_obs
        self.base_mask = base_mask
        self.info = info

    @property
    def phase(self) -> int:
        return int(self.raw_obs[COMBAT_OBS_SIZE])

    @property
    def floor(self) -> int:
        return int(self.info.get("floor", 0))

    @property
    def hp_fraction(self) -> float:
        hp = float(self.info.get("player_hp", 0))
        max_hp = float(self.info.get("player_max_hp", 1))
        return hp / max_hp if max_hp > 0 else 0.0


def is_interesting(decision: TraversalDecision) -> bool:
    """Elite/boss, danger, or a genuine multi-target / run-level choice."""

    phase = decision.phase
    info = decision.info
    if phase == 0:
        alive = alive_enemy_indices(decision.raw_obs)
        if len(alive) >= 2 and decision.hp_fraction < 0.5:
            return True
        node_type = int(info.get("current_node_type", 0))
        if node_type in (2, 6):  # elite, boss
            return True
        if decision.hp_fraction < 0.25:
            return True
        if len(alive) >= 2 and any(
            bool(decision.base_mask[index])
            and def_id
            and is_single_target(def_id)
            for index, def_id in enumerate(hand_def_ids(decision.raw_obs))
        ):
            return True
        return False
    # Every non-combat decision is a run-level choice worth distilling.
    return phase in (1, 2, 3, 4, 5, 7, 8, 9, 10)


# --------------------------------------------------------------------------
# label generation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TeacherBatchConfig:
    rollout_max_steps: int = 24
    discount: float = 1.0
    min_score_gap: float = 0.5
    max_decisions_per_run: int = 12
    traversal_steps: int = 400
    max_episode_steps: int = 1200
    high_hp_danger_fraction: float = 0.30


def label_decision(
    decision: TraversalDecision,
    *,
    env_factory: Callable[[int | str], Any],
    emulator_hash: str,
    config: TeacherBatchConfig,
) -> dict[str, Any] | None:
    """Score every legal candidate by replay and emit one validated record."""

    codec = codec_for_state(
        decision.raw_obs,
        decision.base_mask,
        encounter_id=int(decision.info.get("encounter_id", -1)),
    )
    if len(codec) < 1:
        return None
    candidates = [
        ActionTarget(item.action, -1 if item.target is None else item.target)
        for item in codec.candidates
    ]
    prefix = capture_prefix(
        decision.seed, decision.decisions, env_factory=env_factory
    )
    budget = RolloutBudget(
        max_steps=config.rollout_max_steps, discount=config.discount
    )
    scores = score_candidates(
        prefix,
        candidates,
        env_factory=env_factory,
        continuation_policy=heuristic_continuation,
        budget=budget,
    )
    ordered = sorted(scores.candidates, key=lambda item: item.score, reverse=True)
    best = ordered[0]
    if len(ordered) < 2:
        return None  # a forced move is not a decision worth distilling
    gap = best.score - ordered[1].score
    if gap < config.min_score_gap:
        return None

    best_candidate = codec.candidates[
        next(
            index
            for index, item in enumerate(candidates)
            if (item.action, item.target) == (best.candidate.action, best.candidate.target)
        )
    ]
    runner_up = None
    if len(ordered) > 1:
        runner_up = codec.candidates[
            next(
                index
                for index, item in enumerate(candidates)
                if (item.action, item.target)
                == (ordered[1].candidate.action, ordered[1].candidate.target)
            )
        ]

    # Replay verification: rebuild the decision state once more and compare
    # hashes against the final state captured before any search happened.
    # ``replay_prefix`` already raises on any intermediate divergence.
    from .prefix_replay_teacher import replay_prefix

    with replay_prefix(prefix, env_factory=env_factory) as rebuilt:
        reverified_hashes = state_hashes(rebuilt.observation, rebuilt.action_mask)
    if reverified_hashes != prefix.final_state:
        return None  # never emit a record whose state cannot be rebuilt

    return {
        "record_version": TEACHER_RECORD_VERSION,
        "scope": SCOPE,
        "disclaimer": SIMULATOR_DISCLAIMER,
        "seed": decision.seed,
        "decision_index": len(decision.decisions),
        "phase": decision.phase,
        "floor": decision.floor,
        "node_type": int(decision.info.get("current_node_type", 0)),
        "event_id": int(decision.info.get("event_id", -1)),
        "player_hp": int(decision.info.get("player_hp", 0)),
        "player_max_hp": int(decision.info.get("player_max_hp", 0)),
        "gold": int(decision.info.get("gold", 0)),
        "state_hashes": {
            "capture": asdict(prefix.final_state),
            "reverify": asdict(reverified_hashes),
        },
        "prefix_sha256": prefix.sha256,
        # The full prefix is embedded so *any* later process (BC loader,
        # DAgger auditor) can re-verify the exact state -- not just trust a
        # hash it cannot reproduce.
        "prefix": prefix.to_json(),
        "replay_verified": True,
        "budget": {
            "rollout_max_steps": budget.max_steps,
            "discount": budget.discount,
        },
        "candidates": [
            {
                "action_id": item.action_id,
                "codec_version": ACTION_ID_VERSION,
                "action": item.action,
                "target": item.target,
                "score": score.score,
                "rollout_steps": score.rollout_steps,
                "terminated": score.terminated,
                "truncated": score.truncated,
                "final_floor": score.final_floor,
            }
            for item, score in zip(codec.candidates, scores.candidates)
        ],
        "best_action_id": best_candidate.action_id,
        "best_pair": [best_candidate.action, best_candidate.target],
        "runner_up_action_id": runner_up.action_id if runner_up else None,
        "score_gap": gap,
        "high_confidence": True,
        "emulator_native_sha256": emulator_hash,
        "action_id_version": ACTION_ID_VERSION,
    }


def traverse_run(
    seed: int | str,
    *,
    env_factory: Callable[[int | str], Any],
    rng: random.Random,
    config: TeacherBatchConfig,
) -> Iterator[TraversalDecision]:
    """Yield decision states along one rejection-free behavioural traversal.

    The raw ``Sts2RunEnv`` silently no-ops a mask-legal action the engine
    itself rejects (reward -1, unchanged state) -- the V1 hang hazard -- while
    the V2 contract stack *truncates* on the same native status.  A prefix
    that crossed such a step would replay fine under the raw teacher path but
    die early under the contract stack the student trains on.  So the
    traversal detects a rejection (reward -1 plus unchanged observation/mask
    hashes; a genuine non-terminal combat reward can never be exactly -1 with
    a frozen state) and excludes that action at that state instead of
    appending it.  Prefixes therefore contain only engine-accepted steps and
    are replayable under both semantics.
    """

    env = env_factory(seed)
    try:
        observation, info = env.reset(seed=seed)
        decisions: list[ActionTarget] = []
        rejected_here: set[int] = set()
        for _ in range(config.traversal_steps):
            base_mask = np.asarray(env.action_masks(), dtype=bool)
            legal = [
                int(index)
                for index in np.flatnonzero(base_mask)
                if int(index) not in rejected_here
            ]
            if not legal:
                return  # empty mask, or every masked action is engine-rejected
            yield TraversalDecision(
                seed=seed,
                decisions=list(decisions),
                raw_obs=np.asarray(observation).copy(),
                base_mask=base_mask.copy(),
                info=dict(info),
            )
            action = int(rng.choice(legal))
            target = -1
            if int(np.asarray(observation)[COMBAT_OBS_SIZE]) == 0:
                alive = alive_enemy_indices(observation)
                hand = hand_def_ids(observation)
                if action < len(hand) and hand[action] and is_single_target(hand[action]):
                    if len(alive) >= 2:
                        target = int(rng.choice(alive))
            pre_hashes = state_hashes(observation, base_mask)
            observation, reward, terminated, truncated, info = env.step(action, target)
            if not terminated and not truncated and float(reward) == -1.0:
                post_mask = np.asarray(env.action_masks(), dtype=bool)
                if state_hashes(observation, post_mask) == pre_hashes:
                    # Silent native rejection: the engine ignored the action.
                    # Never let it enter a training prefix.
                    rejected_here.add(action)
                    continue
            rejected_here.clear()
            decisions.append(ActionTarget(action, target))
            if terminated or truncated:
                return
    finally:
        env.close()


def generate_batch(
    seeds: Sequence[int],
    *,
    env_factory: Callable[[int | str], Any],
    emulator_hash: str,
    config: TeacherBatchConfig = TeacherBatchConfig(),
    progress: Callable[[dict[str, int]], None] | None = None,
) -> Iterator[dict[str, Any]]:
    """Label interesting states across seeds; yields validated records."""

    stats = {"runs": 0, "visited": 0, "interesting": 0, "labelled": 0, "rejected": 0}
    for seed in seeds:
        stats["runs"] += 1
        # Process-stable traversal RNG: Python's built-in hash() is salted,
        # so derive the seed from a cryptographic digest instead.
        digest = hashlib.blake2b(f"teacher-traversal:{seed}".encode(), digest_size=8)
        rng = random.Random(int.from_bytes(digest.digest(), "big"))
        labelled = 0
        for decision in traverse_run(
            seed, env_factory=env_factory, rng=rng, config=config
        ):
            stats["visited"] += 1
            if not is_interesting(decision):
                continue
            stats["interesting"] += 1
            if labelled >= config.max_decisions_per_run:
                continue
            try:
                record = label_decision(
                    decision,
                    env_factory=env_factory,
                    emulator_hash=emulator_hash,
                    config=config,
                )
            except Exception:  # a divergent replay is a rejection, not a crash
                stats["rejected"] += 1
                continue
            if record is None:
                stats["rejected"] += 1
                continue
            labelled += 1
            stats["labelled"] += 1
            yield record
        if progress is not None:
            progress(dict(stats))


__all__ = [
    "TEACHER_RECORD_VERSION",
    "TeacherBatchConfig",
    "TraversalDecision",
    "alive_enemy_indices",
    "codec_for_state",
    "generate_batch",
    "hand_def_ids",
    "heuristic_continuation",
    "is_interesting",
    "label_decision",
    "traverse_run",
]
