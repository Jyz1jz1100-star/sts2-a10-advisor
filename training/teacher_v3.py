"""Teacher v3: true combat beam search and out-of-run long rollouts.

Review item 5 (2026-09-01).  The frozen R2 lineage proves replay and label
semantics, *not* optimality: its scorer continued every candidate with a
24-step greedy rollout, which is neither beam search nor a long-horizon
evaluation.  This module replaces the continuation with two honest search
procedures while keeping the record contract (replayable prefix, verified
state hashes, score gap) that downstream tooling already consumes:

* **Combat — beam search to end-of-turn plus opponent rounds.**  From the
  replayed decision state the search branches over every legal
  ``(action, target)`` pair, prunes to ``beam_width`` (64–256) states, and
  keeps expanding until the labelled player turn *and* 1–2 further enemy
  rounds complete (the engine folds enemy turns into the ``end turn``
  transition, so each completed player turn is one enemy round).  Terminal
  and horizon utilities include the combat result and remaining HP.
* **Out-of-run — long rollouts averaged over continuations.**  From every
  candidate's post-state, a seeded continuation policy plays until the *next
  combat ends* (or the curriculum boundary / Act 1 terminal / step cap), and
  the score is the mean over several continuation seeds, so the label does
  not depend on one arbitrary greedy trajectory.

A record may only be called an *expert label* after the teacher-strength
evaluation (``scripts/evaluate_teacher_strength.py``) shows this search beats
the heuristic, BC, and PPO baselines on independent seeds.  Until then its
output is "v3 search labels", scoped ``simulator_act1``.

Every score replays the seed through the raw ``RunEnvironment`` protocol
(scripted doubles in tests, ``sts2_run_env_factory`` in production), so
results are exactly reproducible and re-auditable.
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from advisor_core.action_codec_v2 import ACTION_ID_VERSION

from .prefix_replay_teacher import (
    SCOPE,
    SIMULATOR_DISCLAIMER,
    ActionTarget,
    PrefixReplayError,
    ReplayPrefix,
    RunEnvironment,
    capture_prefix,
    replay_prefix,
    state_hashes,
)
from .teacher_batch import (
    TraversalDecision,
    alive_enemy_indices,
    codec_for_state,
    hand_def_ids,
    is_single_target,
)
from .v2_constants import COMBAT_OBS_SIZE, PHASE_COMBAT

#: Record version emitted by this module (the frozen R2 lineage is version 2).
TEACHER_V3_RECORD_VERSION = 3

#: Frozen R2 teacher lineage (``data/teacher/FREEZE-R2.txt``): batch0/batch1
#: and dagger0 must never be re-generated or expanded with more of the same
#: design.  Bounds are generous safety margins around the audited manifests.
FROZEN_R2_SEED_RANGES: tuple[tuple[int, int], ...] = (
    (1_400_000_000, 1_401_000_000),  # batch0 + batch1 lineage
    (1_500_100_000, 1_500_102_000),  # dagger0
)

#: Everything at or above this seed is reserved for the final BC holdout
#: corpus; training labels must never be generated there (evaluation-only
#: use, e.g. the strength report, is fine).
RESERVED_TEACHER_TEST_SEED_START = 1_410_000_000

#: Default v3 generation range: below the reserved holdout, above the frozen
#: R2 lineage, disjoint from every train/eval partition (1.0e8–1.42e8,
#: smoke 1.9e9+) and from the dagger seeds (1.5001e9).
V3_DEFAULT_SEED_START = 1_405_000_000


def assert_seeds_outside_frozen_lineages(
    seeds: Sequence[int], *, allow_reserved_test_corpus: bool = False
) -> None:
    """Refuse any seed inside a frozen R2 range or the reserved holdout.

    Raises :class:`ValueError` naming the offending seed and range, so a
    mis-configured batch job fails loudly before writing anything.
    """

    for seed in seeds:
        value = int(seed)
        for start, stop in FROZEN_R2_SEED_RANGES:
            if start <= value < stop:
                raise ValueError(
                    f"seed {value} is inside the frozen R2 teacher lineage "
                    f"[{start}, {stop}) (FREEZE-R2.txt); do not expand that dataset"
                )
        if not allow_reserved_test_corpus and value >= RESERVED_TEACHER_TEST_SEED_START:
            raise ValueError(
                f"seed {value} is >= RESERVED_TEACHER_TEST_SEED_START "
                f"({RESERVED_TEACHER_TEST_SEED_START}); those seeds are reserved "
                "for the final BC holdout and must never receive training labels"
            )


def strength_report_approves(report: dict[str, Any] | None) -> bool:
    """True only when a strength report's gate actually passed."""

    if not isinstance(report, dict):
        return False
    gate = report.get("gate")
    if not isinstance(gate, dict):
        return False
    return gate.get("expert_labels_approved") is True and report.get(
        "scope"
    ) == "simulator_act1"


# --------------------------------------------------------------------------
# configurations
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BeamSearchConfig:
    """Combat search budget and utility weights.

    ``opponent_lookahead_turns`` adds full player-turn + enemy-round cycles
    after the labelled turn completes: 1 means "the labelled turn plus one
    more enemy round", 2 means two more (the review's 1–2 range).  Weights
    are *teacher scoring* knobs — they rank candidates for labelling and are
    deliberately independent of the training reward.
    """

    beam_width: int = 128
    opponent_lookahead_turns: int = 1
    max_node_expansions: int = 4096
    max_steps_per_turn: int = 16
    discount: float = 1.0
    #: Per-unit HP fraction at the horizon leaf; kept at 1.0 so the potential
    #: (bounded by 1.0) can never outweigh the terminal win/loss bonuses.
    hp_weight: float = 1.0
    floor_weight: float = 0.0
    combat_win_bonus: float = 5.0
    combat_loss_bonus: float = -5.0
    horizon_penalty: float = -2.0

    def __post_init__(self) -> None:
        if self.beam_width < 1:
            raise ValueError("beam_width must be >= 1 (64–256 recommended)")
        if self.opponent_lookahead_turns < 0:
            raise ValueError("opponent_lookahead_turns must be >= 0")
        if self.max_node_expansions < 1 or self.max_steps_per_turn < 1:
            raise ValueError("node and per-turn step caps must be positive")
        if not 0.0 < self.discount <= 1.0:
            raise ValueError("discount must be in (0, 1]")

    @property
    def horizon_turns(self) -> int:
        """Completed player turns the search may span (labelled + lookahead)."""

        return 1 + self.opponent_lookahead_turns


@dataclass(frozen=True)
class LongRolloutConfig:
    """Out-of-run rollout budget and continuation count."""

    continuations: int = 3
    max_steps: int = 600
    discount: float = 1.0
    #: Stop as soon as the next combat ends ("至下场战斗结束").
    stop_at_combat_end: bool = True
    #: Curriculum boundary: stop once the floor advances past this.
    max_floor: int | None = None
    win_bonus: float = 5.0
    loss_bonus: float = -5.0
    truncation_penalty: float = -1.0

    def __post_init__(self) -> None:
        if self.continuations < 1 or self.max_steps < 1:
            raise ValueError("continuations and max_steps must be positive")
        if not 0.0 < self.discount <= 1.0:
            raise ValueError("discount must be in (0, 1]")


# --------------------------------------------------------------------------
# shared low-level helpers (mirror the frozen v2 semantics)
# --------------------------------------------------------------------------


def _mask_of(env: RunEnvironment) -> tuple[bool, ...]:
    return tuple(bool(value) for value in env.action_masks())


def _end_turn_action(raw_obs: Any) -> int:
    """Action index the codec assigned to end turn (first free hand slot)."""

    return sum(1 for def_id in hand_def_ids(raw_obs) if def_id)


def _phase_of(raw_obs: Any) -> int:
    return int(np.asarray(raw_obs)[COMBAT_OBS_SIZE])


def _hp_fraction(info: Mapping[str, Any]) -> float:
    hp = float(info.get("player_hp", 0) or 0)
    max_hp = float(info.get("player_max_hp", 0) or 0)
    return hp / max_hp if max_hp > 0 else 0.0


def _targeted_actions(obs: Any) -> list[int]:
    return [
        index
        for index, def_id in enumerate(hand_def_ids(obs))
        if def_id and is_single_target(def_id)
    ]


def _replay_forward(
    prefix: ReplayPrefix,
    decisions: Sequence[ActionTarget],
    env_factory: Callable[[int | str], RunEnvironment],
) -> tuple[RunEnvironment, Any, tuple[bool, ...], Mapping[str, Any], bool]:
    """Rebuild ``prefix + decisions`` from the seed; return the live state.

    Raises through the replay contract on any hash divergence.  The caller
    closes the returned environment.  ``done`` is True when the replay ended
    the episode before all decisions were consumed.
    """

    env = env_factory(prefix.seed)
    observation, info = env.reset(seed=prefix.seed)
    for step in prefix.steps:
        observation, _reward, terminated, truncated, info = env.step(
            step.decision.action, step.decision.target
        )
        if terminated or truncated:
            return env, observation, _mask_of(env), info, True
    done = False
    for decision in decisions:
        mask = _mask_of(env)
        if (
            decision.action < 0
            or decision.action >= len(mask)
            or not mask[decision.action]
        ):
            env.close()
            raise PrefixReplayError(
                f"search decision {decision} is mask-illegal on replay"
            )
        observation, _reward, terminated, truncated, info = env.step(
            decision.action, decision.target
        )
        if terminated or truncated:
            done = True
            break
    return env, observation, _mask_of(env), info, done


def _step_normalized(
    env: RunEnvironment,
    observation: Any,
    mask: Sequence[bool],
    decision: ActionTarget,
) -> tuple[Any, float, bool, bool, Mapping[str, Any]]:
    """One step with the frozen v2 native-rejection normalization."""

    pre_state = state_hashes(observation, mask)
    observation, reward, terminated, truncated, info = env.step(
        decision.action, decision.target
    )
    if not terminated and not truncated and float(reward) == -1.0:
        post_mask = _mask_of(env)
        if state_hashes(observation, post_mask) == pre_state:
            reward, truncated = 0.0, True
    return observation, float(reward), bool(terminated), bool(truncated), info


def _leaf_potential(info: Mapping[str, Any], config: BeamSearchConfig) -> float:
    floor = float(info.get("floor", 0) or 0)
    return config.hp_weight * _hp_fraction(info) + config.floor_weight * floor


# --------------------------------------------------------------------------
# combat beam search
# --------------------------------------------------------------------------


@dataclass
class _BeamNode:
    decisions: tuple[ActionTarget, ...]
    score: float
    end_turns_fired: int
    steps_in_turn: int
    steps: int
    done: bool = False
    won: bool | None = None
    truncated: bool = False


@dataclass
class BeamSearchResult:
    best_action: int
    best_target: int
    best_score: float
    runner_up_score: float | None
    best_line: tuple[tuple[int, int], ...]
    root_scores: dict[str, float]
    expansions: int
    horizon_turns: int
    search_done: bool
    won: bool | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _search_root_subtree(
    prefix: ReplayPrefix,
    root: ActionTarget,
    *,
    env_factory: Callable[[int | str], RunEnvironment],
    config: BeamSearchConfig,
) -> tuple[float, bool, int]:
    """Beam-search one root candidate's subtree to the uniform horizon.

    The root action is forced at ply one; the beam then explores that root's
    lines until ``1 + opponent_lookahead_turns`` player turns have completed
    (each end-turn transition folds one enemy round) or the episode ends.
    Returns ``(score, horizon_reached, expansions)``.  The score is the best
    of: terminal lines (win/loss bonuses) and the line evaluated exactly at
    the horizon; if the budget expires first, the best completed-turn score
    seen so far is the honest fallback and ``horizon_reached`` is False.
    """

    beam: list[_BeamNode] = [
        _BeamNode(decisions=(), score=0.0, end_turns_fired=0, steps_in_turn=0, steps=0)
    ]
    expansions = 0
    best_terminal: float | None = None
    best_at_horizon: float | None = None
    best_completed_turn: float | None = None

    while any(not node.done for node in beam):
        if expansions >= config.max_node_expansions:
            break
        level: list[_BeamNode] = []
        for node in beam:
            if node.done:
                level.append(node)
                continue
            env, observation, mask, info, replay_done = _replay_forward(
                prefix, node.decisions, env_factory
            )
            try:
                if replay_done or not any(mask):
                    node.done, node.truncated = True, True
                    node.score += config.discount**node.steps * config.horizon_penalty
                    level.append(node)
                    continue
                end_turn = _end_turn_action(observation)
                targeted = (
                    set(_targeted_actions(observation))
                    if _phase_of(observation) == PHASE_COMBAT
                    else set()
                )
                alive = alive_enemy_indices(observation)
                forced = root if not node.decisions else None
                for action in range(len(mask)):
                    if not mask[action]:
                        continue
                    targets = [-1]
                    if action in targeted and len(alive) >= 2:
                        targets = list(alive)
                    for target in targets:
                        decision = ActionTarget(action, target)
                        if forced is not None and decision != forced:
                            continue
                        if expansions >= config.max_node_expansions:
                            break
                        expansions += 1
                        _obs, reward, terminated, truncated, step_info = (
                            _step_normalized(env, observation, mask, decision)
                        )
                        discount = config.discount**node.steps
                        child = _BeamNode(
                            decisions=node.decisions + (decision,),
                            score=node.score + discount * reward,
                            end_turns_fired=node.end_turns_fired,
                            steps_in_turn=node.steps_in_turn + 1,
                            steps=node.steps + 1,
                        )
                        fired_turn = action == end_turn
                        if fired_turn:
                            child.end_turns_fired += 1
                            child.steps_in_turn = 0
                        if terminated:
                            child.done = True
                            child.won = bool(step_info.get("player_won", False))
                            child.score += discount * (
                                config.combat_win_bonus
                                if child.won
                                else config.combat_loss_bonus
                            )
                            best_terminal = max(
                                best_terminal
                                if best_terminal is not None
                                else float("-inf"),
                                child.score,
                            )
                        elif truncated:
                            child.done, child.truncated = True, True
                            child.score += discount * config.horizon_penalty
                        elif fired_turn and child.end_turns_fired > config.opponent_lookahead_turns:
                            child.done = True
                            child.score += discount * _leaf_potential(step_info, config)
                        elif child.steps_in_turn >= config.max_steps_per_turn:
                            child.done, child.truncated = True, True
                            child.score += discount * config.horizon_penalty
                        if fired_turn and child.end_turns_fired >= 1:
                            best_completed_turn = max(
                                best_completed_turn
                                if best_completed_turn is not None
                                else float("-inf"),
                                child.score,
                            )
                            if child.end_turns_fired == config.horizon_turns:
                                best_at_horizon = max(
                                    best_at_horizon
                                    if best_at_horizon is not None
                                    else float("-inf"),
                                    child.score,
                                )
                        level.append(child)
                    if expansions >= config.max_node_expansions:
                        break
            finally:
                env.close()
        if not level:
            break
        beam = sorted(level, key=lambda item: item.score, reverse=True)[
            : config.beam_width
        ]

    candidates = [
        value
        for value in (best_at_horizon, best_terminal, best_completed_turn)
        if value is not None
    ]
    if not candidates:
        raise PrefixReplayError(
            f"root {root} produced no completed turn within budget"
        )
    horizon_reached = best_at_horizon is not None or best_terminal is not None
    return max(candidates), horizon_reached, expansions


def beam_search_action(
    prefix: ReplayPrefix,
    *,
    env_factory: Callable[[int | str], RunEnvironment],
    config: BeamSearchConfig = BeamSearchConfig(),
    root_candidates: Sequence[ActionTarget] | None = None,
) -> BeamSearchResult:
    """Score every root candidate with its own beam search and rank them.

    Each root candidate gets the full ``beam_width`` budget inside its own
    subtree, so no root is disadvantaged by cross-root pruning, and every
    score is measured at the same horizon (labelled turn plus
    ``opponent_lookahead_turns`` enemy rounds).  Terminal lines (combat won
    or lost) rank by their terminal score; the best/runner-up pair is the
    label's confidence basis.
    """

    if root_candidates is None:
        env, observation, mask, info, _done = _replay_forward(prefix, (), env_factory)
        env.close()
        codec = codec_for_state(
            observation,
            np.asarray(mask, dtype=bool),
            encounter_id=int(info.get("encounter_id", -1)),
        )
        root_candidates = [
            ActionTarget(int(item.action), -1 if item.target is None else int(item.target))
            for item in codec.candidates
        ]
    if len(root_candidates) < 2:
        raise PrefixReplayError(
            "fewer than two root candidates: a forced move is not a decision"
        )

    root_scores: dict[str, float] = {}
    expansions = 0
    horizon_reached_all = True
    for root in root_candidates:
        score, reached, used = _search_root_subtree(
            prefix, root, env_factory=env_factory, config=config
        )
        expansions += used
        horizon_reached_all = horizon_reached_all and reached
        root_scores[f"{root.action}:{root.target}"] = score

    ranked = sorted(root_scores.items(), key=lambda kv: kv[1], reverse=True)
    best_key, best_score = ranked[0]
    runner_up_score = ranked[1][1]
    best_action_s, best_target_s = best_key.split(":")
    return BeamSearchResult(
        best_action=int(best_action_s),
        best_target=int(best_target_s),
        best_score=best_score,
        runner_up_score=runner_up_score,
        best_line=(),
        root_scores=dict(ranked),
        expansions=expansions,
        horizon_turns=config.horizon_turns,
        search_done=horizon_reached_all,
        won=None,
    )


# --------------------------------------------------------------------------
# out-of-run long rollouts with continuation averaging
# --------------------------------------------------------------------------


@dataclass
class RolloutResult:
    score: float
    steps: int
    entered_combat: bool
    combat_completed: bool
    terminated: bool
    truncated: bool
    final_floor: int
    final_hp_fraction: float
    won: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _seeded_continuation(rng: random.Random) -> Callable[..., ActionTarget]:
    """Heuristic continuation with rng tie-breaking among legal actions.

    In combat it usually plays a random legal hand card (target split across
    living enemies for single-target cards) and ends the turn otherwise;
    outside combat it picks a random legal option.  Different continuation
    seeds explore different downstream trajectories — the label averages
    over them instead of trusting one greedy line.
    """

    def continuation(
        observation: Any,
        mask: Sequence[bool],
        info: Mapping[str, Any],
        step_index: int,
    ) -> ActionTarget:
        legal = [index for index, value in enumerate(mask) if value]
        if not legal:
            return ActionTarget(0, -1)
        obs = np.asarray(observation)
        if _phase_of(obs) == PHASE_COMBAT:
            hand = hand_def_ids(obs)
            playable = [
                index
                for index in legal
                if index < len(hand) and hand[index]
            ]
            if playable and rng.random() < 0.75:
                action = int(rng.choice(playable))
                target = -1
                if action < len(hand) and hand[action] and is_single_target(hand[action]):
                    alive = alive_enemy_indices(obs)
                    if len(alive) >= 2:
                        target = int(rng.choice(alive))
                return ActionTarget(action, target)
            end_turn = _end_turn_action(obs)
            if end_turn in legal:
                return ActionTarget(end_turn, -1)
            return ActionTarget(legal[-1], -1)
        return ActionTarget(int(rng.choice(legal)), -1)

    return continuation


def long_rollout(
    prefix: ReplayPrefix,
    *,
    env_factory: Callable[[int | str], RunEnvironment],
    decisions: Sequence[ActionTarget] = (),
    config: LongRolloutConfig = LongRolloutConfig(),
    rng: random.Random,
) -> RolloutResult:
    """Roll the run forward from ``prefix + decisions`` until a stop condition.

    Stop conditions, in priority order: the simulator terminates or truncates,
    the next combat completes after having been entered, the floor passes
    ``config.max_floor`` (curriculum boundary), or ``config.max_steps`` is
    reached.  The score accumulates discounted engine rewards plus terminal
    bonuses, mirroring the frozen v2 utility so the two eras stay comparable.
    """

    env, observation, mask, info, replay_done = _replay_forward(
        prefix, decisions, env_factory
    )
    try:
        score = 0.0
        entered_combat = _phase_of(observation) == PHASE_COMBAT
        combat_completed = False
        terminated = truncated = False
        won = False
        continuation = _seeded_continuation(rng)
        steps = 0
        while steps < config.max_steps and not replay_done:
            if not any(mask):
                truncated = True
                break
            decision = continuation(observation, mask, info, steps)
            observation, reward, terminated, truncated, info = _step_normalized(
                env, observation, mask, decision
            )
            discount = config.discount**steps
            score += discount * reward
            steps += 1
            mask = _mask_of(env)
            phase = _phase_of(observation)
            if phase == PHASE_COMBAT:
                entered_combat = True
            elif entered_combat:
                combat_completed = True
            if terminated:
                won = bool(terminated and info.get("player_won", False))
                score += discount * (config.win_bonus if won else config.loss_bonus)
                break
            if truncated:
                score += discount * config.truncation_penalty
                break
            if config.stop_at_combat_end and combat_completed:
                break
            floor = int(info.get("floor", 0) or 0)
            if config.max_floor is not None and floor > config.max_floor:
                break
        return RolloutResult(
            score=score,
            steps=steps,
            entered_combat=entered_combat,
            combat_completed=combat_completed,
            terminated=bool(terminated),
            truncated=bool(truncated),
            final_floor=int(info.get("floor", 0) or 0),
            final_hp_fraction=_hp_fraction(info),
            won=bool(won),
        )
    finally:
        env.close()


def average_continuation_scores(
    prefix: ReplayPrefix,
    *,
    env_factory: Callable[[int | str], RunEnvironment],
    decisions: Sequence[ActionTarget] = (),
    config: LongRolloutConfig = LongRolloutConfig(),
    rng_seeds: Sequence[int],
) -> dict[str, Any]:
    """Mean long-rollout score over independent continuation seeds."""

    if len(rng_seeds) != config.continuations:
        raise ValueError(
            f"expected {config.continuations} continuation seeds, got {len(rng_seeds)}"
        )
    results = [
        long_rollout(
            prefix,
            env_factory=env_factory,
            decisions=decisions,
            config=config,
            rng=random.Random(seed),
        )
        for seed in rng_seeds
    ]
    return {
        "mean_score": sum(item.score for item in results) / len(results),
        "per_seed": [item.to_dict() for item in results],
        "rng_seeds": list(rng_seeds),
    }


# --------------------------------------------------------------------------
# v3 labelling
# --------------------------------------------------------------------------


def label_decision_v3(
    decision: TraversalDecision,
    *,
    env_factory: Callable[[int | str], RunEnvironment],
    emulator_hash: str,
    beam_config: BeamSearchConfig = BeamSearchConfig(),
    rollout_config: LongRolloutConfig = LongRolloutConfig(),
    min_score_gap: float = 0.5,
    continuation_seed_start: int = 902_100_001,
) -> dict[str, Any] | None:
    """Label one decision state with v3 search; ``None`` when not exportable.

    Combat states are scored by :func:`beam_search_action`; every other state
    by continuation-averaged long rollouts from each candidate's post-state.
    The record layout matches the frozen v2 contract (prefix, hashes,
    ``best_pair``, ``score_gap``) so ``training.teacher_bc_dataset`` and the
    auditors consume it unchanged, plus a ``teacher`` provenance block.
    """

    codec = codec_for_state(
        decision.raw_obs,
        decision.base_mask,
        encounter_id=int(decision.info.get("encounter_id", -1)),
    )
    if len(codec) < 1:
        return None
    prefix = capture_prefix(
        decision.seed, decision.decisions, env_factory=env_factory
    )
    candidates = [
        ActionTarget(int(item.action), -1 if item.target is None else int(item.target))
        for item in codec.candidates
    ]

    if decision.phase == PHASE_COMBAT:
        search = beam_search_action(
            prefix, env_factory=env_factory, config=beam_config
        )
        ranked = sorted(search.root_scores.items(), key=lambda kv: kv[1], reverse=True)
        if len(ranked) < 2:
            return None  # a forced move is not a decision worth distilling
        gap = ranked[0][1] - ranked[1][1]
        best_pair = (search.best_action, search.best_target)
        scores_by_pair: dict[str, float] = search.root_scores
        search_payload: dict[str, Any] = {
            "mode": "combat_beam",
            "beam_width": beam_config.beam_width,
            "horizon_turns": beam_config.horizon_turns,
            "opponent_lookahead_turns": beam_config.opponent_lookahead_turns,
            "expansions": search.expansions,
            "root_scores": search.root_scores,
            "best_score": search.best_score,
            "runner_up_score": search.runner_up_score,
            "search_done": search.search_done,
        }
    else:
        detail: dict[str, float] = {}
        for index, candidate in enumerate(candidates):
            averaged = average_continuation_scores(
                prefix,
                env_factory=env_factory,
                decisions=(candidate,),
                config=rollout_config,
                rng_seeds=[
                    continuation_seed_start + index * 1000 + offset
                    for offset in range(rollout_config.continuations)
                ],
            )
            detail[f"{candidate.action}:{candidate.target}"] = averaged["mean_score"]
        if len(detail) < 2:
            return None
        ranked = sorted(detail.items(), key=lambda kv: kv[1], reverse=True)
        gap = ranked[0][1] - ranked[1][1]
        action_s, target_s = ranked[0][0].split(":")
        best_pair = (int(action_s), int(target_s))
        scores_by_pair = detail
        search_payload = {
            "mode": "out_of_run_rollout",
            "continuations": rollout_config.continuations,
            "max_steps": rollout_config.max_steps,
            "stop_at_combat_end": rollout_config.stop_at_combat_end,
            "max_floor": rollout_config.max_floor,
            "candidate_mean_scores": detail,
            "best_score": ranked[0][1],
            "runner_up_score": ranked[1][1],
        }

    if gap < min_score_gap:
        return None

    def _pair_of(item: Any) -> tuple[int, int]:
        return (int(item.action), -1 if item.target is None else int(item.target))

    best_candidate = codec.candidates[
        next(
            index
            for index, item in enumerate(codec.candidates)
            if _pair_of(item) == best_pair
        )
    ]
    runner_up_key = ranked[1][0]
    action_s, target_s = runner_up_key.split(":")
    runner_up = next(
        (
            item
            for item in codec.candidates
            if _pair_of(item) == (int(action_s), int(target_s))
        ),
        None,
    )

    with replay_prefix(prefix, env_factory=env_factory) as rebuilt:
        reverified = state_hashes(rebuilt.observation, rebuilt.action_mask)
    if reverified != prefix.final_state:
        return None  # never emit a record whose state cannot be rebuilt

    return {
        "record_version": TEACHER_V3_RECORD_VERSION,
        "scope": SCOPE,
        "disclaimer": SIMULATOR_DISCLAIMER,
        "seed": decision.seed,
        "decision_index": len(decision.decisions),
        "source": "teacher_v3",
        "phase": decision.phase,
        "floor": decision.floor,
        "node_type": int(decision.info.get("current_node_type", 0)),
        "event_id": int(decision.info.get("event_id", -1)),
        "player_hp": int(decision.info.get("player_hp", 0)),
        "player_max_hp": int(decision.info.get("player_max_hp", 0)),
        "gold": int(decision.info.get("gold", 0)),
        "state_hashes": {
            "capture": asdict(prefix.final_state),
            "reverify": asdict(reverified),
        },
        "prefix_sha256": prefix.sha256,
        "prefix": prefix.to_json(),
        "replay_verified": True,
        "budget": {
            "beam_width": beam_config.beam_width,
            "opponent_lookahead_turns": beam_config.opponent_lookahead_turns,
            "continuations": rollout_config.continuations,
            "rollout_max_steps": rollout_config.max_steps,
            "discount": rollout_config.discount,
        },
        "candidates": [
            {
                "action_id": item.action_id,
                "codec_version": ACTION_ID_VERSION,
                "action": item.action,
                "target": item.target,
                "score": scores_by_pair.get(f"{item.action}:{item.target}"),
            }
            for item in codec.candidates
        ],
        "best_action_id": best_candidate.action_id,
        "best_pair": [best_pair[0], best_pair[1]],
        "runner_up_action_id": runner_up.action_id if runner_up else None,
        "score_gap": gap,
        "high_confidence": True,
        "emulator_native_sha256": emulator_hash,
        "action_id_version": ACTION_ID_VERSION,
        "teacher": {
            "version": TEACHER_V3_RECORD_VERSION,
            "search": search_payload,
            "expert_label": (
                "PENDING: only scripts/evaluate_teacher_strength.py passing "
                "against heuristic/BC/PPO on independent seeds upgrades "
                "these records to expert labels"
            ),
        },
    }


__all__ = [
    "FROZEN_R2_SEED_RANGES",
    "RESERVED_TEACHER_TEST_SEED_START",
    "TEACHER_V3_RECORD_VERSION",
    "V3_DEFAULT_SEED_START",
    "BeamSearchConfig",
    "BeamSearchResult",
    "LongRolloutConfig",
    "RolloutResult",
    "assert_seeds_outside_frozen_lineages",
    "average_continuation_scores",
    "beam_search_action",
    "label_decision_v3",
    "long_rollout",
    "strength_report_approves",
]
