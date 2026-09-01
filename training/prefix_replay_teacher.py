"""Deterministic prefix-replay teacher for the Act 1 simulator.

This module deliberately labels every output ``simulator_act1``.  Replaying a
seed in :class:`sts2_gym.Sts2RunEnv` is useful for search/distillation, but it
is not evidence of real-game A10 performance.

The public workflow is:

1. :func:`capture_prefix` records observation/action-mask hashes around a
   known ``(action, target)`` prefix.
2. :func:`replay_prefix` reconstructs the environment from its seed and checks
   every recorded hash before exposing the rebuilt decision state.
3. :func:`score_candidates` rebuilds that same state independently for every
   legal candidate and evaluates it with a strictly bounded rollout.

The environment factory is injectable so the replay contract can be tested
without loading the native emulator.  :func:`sts2_run_env_factory` is the
small adapter used for real simulator work.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence


SCOPE = "simulator_act1"
SIMULATOR_DISCLAIMER = (
    "Act 1 emulator search only; this is not a real-game A10 result."
)


class RunEnvironment(Protocol):
    """Small surface shared by Sts2RunEnv and deterministic test doubles."""

    def reset(self, *, seed: int | str): ...

    def action_masks(self): ...

    def step(self, action: int, target: int = -1): ...

    def close(self) -> None: ...


EnvironmentFactory = Callable[[int | str], RunEnvironment]


class PrefixReplayError(RuntimeError):
    """Base error for a prefix that cannot be deterministically replayed."""


class ReplayHashMismatch(PrefixReplayError):
    """The rebuilt simulator state differs from the captured state."""


class IllegalReplayAction(PrefixReplayError):
    """A prefix, candidate, or continuation action is outside the action mask."""


@dataclass(frozen=True, slots=True)
class ActionTarget:
    action: int
    target: int = -1

    def __post_init__(self) -> None:
        if isinstance(self.action, bool) or not isinstance(self.action, int):
            raise TypeError("action must be an integer action-space index")
        if isinstance(self.target, bool) or not isinstance(self.target, int):
            raise TypeError("target must be an integer target index")


@dataclass(frozen=True, slots=True)
class StateHashes:
    observation_sha256: str
    action_mask_sha256: str


@dataclass(frozen=True, slots=True)
class PrefixStep:
    decision: ActionTarget
    state: StateHashes


@dataclass(frozen=True, slots=True)
class ReplayPrefix:
    seed: int | str
    steps: tuple[PrefixStep, ...]
    final_state: StateHashes
    scope: str = SCOPE

    def __post_init__(self) -> None:
        if self.scope != SCOPE:
            raise ValueError(f"prefix scope must remain {SCOPE!r}")

    @property
    def sha256(self) -> str:
        payload = {
            "scope": self.scope,
            "seed": self.seed,
            "steps": [asdict(step) for step in self.steps],
            "final_state": asdict(self.final_state),
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def to_json(self) -> dict[str, Any]:
        """Serializable form whose ``sha256`` a later process can re-verify."""

        return {
            "scope": self.scope,
            "seed": self.seed,
            "steps": [asdict(step) for step in self.steps],
            "final_state": asdict(self.final_state),
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "ReplayPrefix":
        steps = tuple(
            PrefixStep(
                decision=ActionTarget(**item["decision"]),
                state=StateHashes(**item["state"]),
            )
            for item in payload["steps"]
        )
        return cls(
            seed=payload["seed"],
            steps=steps,
            final_state=StateHashes(**payload["final_state"]),
            scope=payload["scope"],
        )


@dataclass(slots=True)
class ReplayedState:
    env: RunEnvironment
    observation: Any
    action_mask: tuple[bool, ...]
    info: Mapping[str, Any]
    prefix_return: float
    terminated: bool
    truncated: bool


@dataclass(frozen=True, slots=True)
class RolloutBudget:
    """Candidate rollout limit and deliberately simple simulator utility."""

    max_steps: int = 64
    discount: float = 1.0
    win_bonus: float = 1.0
    loss_bonus: float = -1.0
    truncation_penalty: float = -0.25

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise ValueError("max_steps must include at least the candidate action")
        if not 0.0 < self.discount <= 1.0:
            raise ValueError("discount must be in (0, 1]")


@dataclass(frozen=True, slots=True)
class CandidateScore:
    candidate: ActionTarget
    score: float
    rollout_steps: int
    terminated: bool
    truncated: bool
    player_won: bool
    final_floor: int
    scope: str = SCOPE


@dataclass(frozen=True, slots=True)
class TeacherScores:
    seed: int | str
    prefix_sha256: str
    budget: RolloutBudget
    candidates: tuple[CandidateScore, ...]
    scope: str = SCOPE
    disclaimer: str = SIMULATOR_DISCLAIMER

    @property
    def best(self) -> CandidateScore:
        if not self.candidates:
            raise ValueError("teacher batch has no candidates")
        return max(self.candidates, key=lambda item: item.score)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


ContinuationPolicy = Callable[
    [Any, Sequence[bool], Mapping[str, Any], int], ActionTarget
]
LeafValue = Callable[[Any, Mapping[str, Any]], float]


def _hash_array_like(value: Any) -> str:
    """Hash values without losing numpy dtype/shape information when present."""

    digest = hashlib.sha256()
    shape = getattr(value, "shape", None)
    dtype = getattr(value, "dtype", None)
    tobytes = getattr(value, "tobytes", None)
    if shape is not None and dtype is not None and callable(tobytes):
        header = json.dumps(
            {"shape": list(shape), "dtype": str(dtype)},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        digest.update(header)
        digest.update(b"\0")
        digest.update(tobytes(order="C"))
        return digest.hexdigest()

    encoded = json.dumps(
        list(value), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    digest.update(encoded)
    return digest.hexdigest()


def state_hashes(observation: Any, action_mask: Sequence[bool]) -> StateHashes:
    """Return the canonical observation and action-mask hashes for one state."""

    return StateHashes(
        observation_sha256=_hash_array_like(observation),
        action_mask_sha256=_hash_array_like(action_mask),
    )


def _mask(env: RunEnvironment) -> tuple[bool, ...]:
    return tuple(bool(value) for value in env.action_masks())


def _assert_state(
    expected: StateHashes,
    observation: Any,
    action_mask: Sequence[bool],
    *,
    position: str,
) -> None:
    actual = state_hashes(observation, action_mask)
    if actual != expected:
        raise ReplayHashMismatch(
            f"prefix replay diverged at {position}: expected {expected}, got {actual}"
        )


def _assert_legal(action: ActionTarget, mask: Sequence[bool], *, position: str) -> None:
    if action.action < 0 or action.action >= len(mask) or not mask[action.action]:
        raise IllegalReplayAction(
            f"action {action.action} target {action.target} is mask-illegal at {position}"
        )


def sts2_run_env_factory(
    *, max_episode_steps: int = 1200, max_floors: int = 16
) -> EnvironmentFactory:
    """Build a seed factory for the installed ``sts2_gym.Sts2RunEnv``.

    The caller remains responsible for putting the emulator's ``src`` folder
    on ``sys.path`` (the curriculum already does this from ``training.toml``).
    """

    from sts2_gym import Sts2RunEnv

    def build(seed: int | str) -> RunEnvironment:
        return Sts2RunEnv(
            seed=seed,
            max_episode_steps=max_episode_steps,
            max_floors=max_floors,
        )

    return build


def capture_prefix(
    seed: int | str,
    decisions: Sequence[ActionTarget],
    *,
    env_factory: EnvironmentFactory,
) -> ReplayPrefix:
    """Execute a known prefix once and capture every deterministic state hash."""

    env = env_factory(seed)
    try:
        observation, _info = env.reset(seed=seed)
        steps: list[PrefixStep] = []
        terminated = truncated = False
        for index, decision in enumerate(decisions):
            mask = _mask(env)
            _assert_legal(decision, mask, position=f"capture step {index}")
            steps.append(PrefixStep(decision, state_hashes(observation, mask)))
            observation, _reward, terminated, truncated, _info = env.step(
                decision.action, decision.target
            )
            if (terminated or truncated) and index + 1 < len(decisions):
                raise PrefixReplayError(
                    f"prefix ended at step {index} before {len(decisions)} actions"
                )
        final_mask = _mask(env)
        return ReplayPrefix(
            seed=seed,
            steps=tuple(steps),
            final_state=state_hashes(observation, final_mask),
        )
    finally:
        env.close()


@contextmanager
def replay_prefix(
    prefix: ReplayPrefix, *, env_factory: EnvironmentFactory
) -> Iterator[ReplayedState]:
    """Rebuild and hash-check a prefix, closing the fresh env on context exit."""

    env = env_factory(prefix.seed)
    try:
        observation, info = env.reset(seed=prefix.seed)
        prefix_return = 0.0
        terminated = truncated = False
        for index, step in enumerate(prefix.steps):
            mask = _mask(env)
            _assert_state(step.state, observation, mask, position=f"step {index}")
            _assert_legal(step.decision, mask, position=f"prefix step {index}")
            observation, reward, terminated, truncated, info = env.step(
                step.decision.action, step.decision.target
            )
            prefix_return += float(reward)
            if (terminated or truncated) and index + 1 < len(prefix.steps):
                raise PrefixReplayError(
                    f"rebuilt prefix ended at step {index} before its recorded end"
                )
        mask = _mask(env)
        _assert_state(prefix.final_state, observation, mask, position="final state")
        yield ReplayedState(
            env=env,
            observation=observation,
            action_mask=mask,
            info=info,
            prefix_return=prefix_return,
            terminated=terminated,
            truncated=truncated,
        )
    finally:
        env.close()


def score_candidates(
    prefix: ReplayPrefix,
    candidates: Sequence[ActionTarget],
    *,
    env_factory: EnvironmentFactory,
    continuation_policy: ContinuationPolicy,
    budget: RolloutBudget = RolloutBudget(),
    leaf_value: LeafValue | None = None,
) -> TeacherScores:
    """Score legal candidates using isolated, deterministic bounded rollouts.

    ``max_steps`` includes the candidate action.  When the budget expires,
    ``leaf_value`` may supply a simulator value estimate; it is never called on
    a terminated or truncated state.  Prefix rewards are intentionally omitted
    because they are identical for every candidate.
    """

    if not candidates:
        raise ValueError("at least one candidate is required")
    if len(set(candidates)) != len(candidates):
        raise ValueError("candidate actions must be unique")

    results: list[CandidateScore] = []
    for candidate_index, candidate in enumerate(candidates):
        with replay_prefix(prefix, env_factory=env_factory) as rebuilt:
            if rebuilt.terminated or rebuilt.truncated:
                raise PrefixReplayError("cannot score actions after the prefix has ended")
            _assert_legal(
                candidate,
                rebuilt.action_mask,
                position=f"candidate {candidate_index}",
            )

            observation, reward, terminated, truncated, info = rebuilt.env.step(
                candidate.action, candidate.target
            )
            score = float(reward)
            steps = 1
            discount = budget.discount

            while not terminated and not truncated and steps < budget.max_steps:
                mask = _mask(rebuilt.env)
                if not any(mask):
                    truncated = True
                    break
                decision = continuation_policy(observation, mask, info, steps)
                if not isinstance(decision, ActionTarget):
                    raise TypeError("continuation_policy must return ActionTarget")
                _assert_legal(decision, mask, position=f"rollout step {steps}")
                observation, reward, terminated, truncated, info = rebuilt.env.step(
                    decision.action, decision.target
                )
                score += discount * float(reward)
                discount *= budget.discount
                steps += 1

            # The current run simulator exposes the previous combat's result
            # as player_won, so only a terminal rollout can be a run win.
            player_won = bool(terminated and info.get("player_won", False))
            if terminated:
                score += discount * (
                    budget.win_bonus if player_won else budget.loss_bonus
                )
            elif truncated:
                score += discount * budget.truncation_penalty
            elif leaf_value is not None:
                score += discount * float(leaf_value(observation, info))

            results.append(
                CandidateScore(
                    candidate=candidate,
                    score=score,
                    rollout_steps=steps,
                    terminated=bool(terminated),
                    truncated=bool(truncated),
                    player_won=player_won,
                    final_floor=int(info.get("floor", 0) or 0),
                )
            )

    return TeacherScores(
        seed=prefix.seed,
        prefix_sha256=prefix.sha256,
        budget=budget,
        candidates=tuple(results),
    )
