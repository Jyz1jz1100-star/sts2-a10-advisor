"""V2 run-level Gymnasium contract for curriculum training.

The upstream simulator exposes two awkward behaviours to callers:

* ``player_won`` means that the most recently completed *combat* was won.  It
  can therefore remain true while a run is still active or is truncated.
* some reachable simulator states expose no legal action at all.  MaskablePPO
  cannot consume an all-false mask.

This wrapper owns those boundary semantics and reward shaping in one place. It
does not depend on the curriculum runner, so it can be tested and adopted by a
future training entry point independently.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

import gymnasium as gym
import numpy as np


@dataclass(frozen=True, slots=True)
class V2RewardConfig:
    """Potential-based run reward parameters.

    For a non-terminal transition the returned reward is::

        combat_reward_scale * raw_reward + gamma * Phi(next) - Phi(current)
        + first_floor_advance_reward * 1[floor advanced past floor 1 once]
        + boundary_success_reward * 1[new boundary node entered]
        - step_cost

    where ``Phi = floor_weight * floor + hp_weight * hp_fraction``.  A true
    terminal state has zero potential and receives exactly one win/loss bonus.
    Truncations are not losses and receive no terminal bonus.

    Review item 4 adds four components (2026-09-01):

    * ``first_floor_advance_reward`` — fires on the *first* transition whose
      floor advances past floor 1 (early exit from the starting room);
    * ``boundary_success_reward`` — fires when the next state's floor carries
      a higher boundary-node marker than any floor seen before in the episode
      (revisiting a floor's node never re-fires it);
    * ``step_cost`` — a small per-step cost so stalling is never free;
    * ``combat_reward_scale`` keeps its existing role as the combat weight.
    """

    gamma: float = 0.99
    combat_reward_scale: float = 0.10
    floor_weight: float = 1.0
    hp_weight: float = 1.0
    terminal_win_reward: float = 25.0
    terminal_death_reward: float = -25.0
    first_floor_advance_reward: float = 0.0
    boundary_success_reward: float = 0.0
    step_cost: float = 0.0

    def __post_init__(self) -> None:
        values = {
            "gamma": self.gamma,
            "combat_reward_scale": self.combat_reward_scale,
            "floor_weight": self.floor_weight,
            "hp_weight": self.hp_weight,
            "terminal_win_reward": self.terminal_win_reward,
            "terminal_death_reward": self.terminal_death_reward,
            "first_floor_advance_reward": self.first_floor_advance_reward,
            "boundary_success_reward": self.boundary_success_reward,
            "step_cost": self.step_cost,
        }
        for name, value in values.items():
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        if not 0.0 <= self.gamma <= 1.0:
            raise ValueError("gamma must be in [0, 1]")
        if self.combat_reward_scale < 0.0:
            raise ValueError("combat_reward_scale must be non-negative")
        if self.step_cost < 0.0:
            raise ValueError("step_cost must be non-negative (it is a cost)")
        if self.first_floor_advance_reward < 0.0 or self.boundary_success_reward < 0.0:
            raise ValueError("success bonuses must be non-negative")


def is_run_victory(
    *, terminated: bool, truncated: bool, info: Mapping[str, Any]
) -> bool:
    """Return the sole V2 definition of a completed run victory.

    ``player_won`` from the unwrapped simulator is deliberately considered
    only for a natural, non-truncated terminal transition.  This prevents a
    stale last-combat-win flag from turning a curriculum/dead-end truncation
    into a run win.
    """

    return bool(terminated and not truncated and info.get("player_won", False))


def run_potential(info: Mapping[str, Any], config: V2RewardConfig) -> float:
    """Compute floor-plus-HP potential from simulator or trace-style keys."""

    floor = _finite_number(info.get("floor", 0.0), default=0.0)
    hp = _finite_number(
        info.get("player_hp", info.get("hp", 0.0)), default=0.0
    )
    max_hp = _finite_number(
        info.get("player_max_hp", info.get("max_hp", 0.0)), default=0.0
    )
    hp_fraction = 0.0 if max_hp <= 0.0 else min(1.0, max(0.0, hp / max_hp))
    return config.floor_weight * max(0.0, floor) + config.hp_weight * hp_fraction


def _finite_number(value: Any, *, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


#: Node type the locked emulator build reports for a floor stairway transition.
_STAIRWAY_NODE_TYPE = 9


def _floor_number(info: Mapping[str, Any]) -> int:
    """Current run floor from simulator or trace-style info keys."""

    floor = _finite_number(info.get("floor", 0.0), default=0.0)
    return int(max(0.0, floor))


def _is_boundary_node(info: Mapping[str, Any]) -> bool:
    """True when the *next* state sits on a floor boundary node.

    The native info stack exposes the current map node type; the stairway
    transition node is node type 9 on the locked build.  Explicit boolean
    ``stairs``-style markers are also honoured so trace-style replay data can
    drive the same reward without the native buffer.
    """

    node_type = _finite_number(info.get("current_node_type", 0), default=0.0)
    if int(node_type) == _STAIRWAY_NODE_TYPE:
        return True
    for key in ("stairs", "node_stairway", "on_stairs"):
        marker = info.get(key)
        if marker is not None and bool(marker):
            return True
    return False


class V2RunEnvWrapper(gym.Wrapper):
    """Normalize run outcome, masks, curriculum horizon, and shaped reward.

    The wrapped environment must have a discrete action space and expose
    ``action_masks()``.  When its mask is all false, this wrapper advertises a
    single synthetic sentinel action and intercepts the following ``step`` as
    one truncating transition without calling the broken underlying state.

    ``max_floor`` is a curriculum boundary, not a win condition.  Reaching it
    changes a still-active transition to ``truncated=True``; a natural terminal
    emitted on the same transition remains authoritative.
    """

    def __init__(
        self,
        env: gym.Env,
        *,
        max_floor: int | None = None,
        reward_config: V2RewardConfig | None = None,
        sentinel_action: int | None = None,
    ) -> None:
        super().__init__(env)
        if max_floor is not None and (
            isinstance(max_floor, bool) or not isinstance(max_floor, int) or max_floor <= 0
        ):
            raise ValueError("max_floor must be a positive integer or None")

        action_count = getattr(self.action_space, "n", None)
        if not isinstance(action_count, (int, np.integer)) or int(action_count) <= 0:
            raise TypeError("V2RunEnvWrapper requires a non-empty Discrete action space")
        self._action_count = int(action_count)
        chosen_sentinel = self._action_count - 1 if sentinel_action is None else sentinel_action
        if (
            isinstance(chosen_sentinel, bool)
            or not isinstance(chosen_sentinel, (int, np.integer))
            or not 0 <= int(chosen_sentinel) < self._action_count
        ):
            raise ValueError("sentinel_action must be a valid discrete action index")

        self.max_floor = max_floor
        self.reward_config = reward_config or V2RewardConfig()
        self.sentinel_action = int(chosen_sentinel)
        self._last_observation: Any = None
        self._last_info: dict[str, Any] = {}
        self._previous_potential = 0.0
        self._empty_mask_pending = False
        self._episode_done = True
        # Review-item-4 event-tracking state (episode scoped):
        self._first_floor_advance_fired = False
        self._max_floor_seen = 0
        self._boundary_floors: set[int] = set()

    def reset(self, *, seed: int | None = None, options: dict | None = None):
        observation, raw_info = self.env.reset(seed=seed, options=options)
        info = self._normalize_info(
            raw_info, terminated=False, truncated=False, outcome_source="reset"
        )
        self._last_observation = observation
        self._last_info = info
        self._previous_potential = run_potential(info, self.reward_config)
        self._empty_mask_pending = False
        self._episode_done = False
        # Reset the episode-scoped event trackers.
        self._first_floor_advance_fired = False
        self._max_floor_seen = _floor_number(info)
        self._boundary_floors = (
            {self._max_floor_seen} if _is_boundary_node(info) else set()
        )
        return observation, info

    def action_masks(self) -> np.ndarray:
        """Return the real mask or one synthetic action for an empty mask."""

        self._require_active_episode()
        mask = self._read_action_mask()
        self._empty_mask_pending = not bool(np.any(mask))
        if self._empty_mask_pending:
            mask[self.sentinel_action] = True
        return mask

    def step(self, action: int):
        self._require_active_episode()

        # Be correct even when a caller invokes step() without first asking
        # for action_masks().  Re-reading a legal mask is side-effect free for
        # the simulator and avoids relying on a particular RL-library order.
        if not self._empty_mask_pending and not bool(np.any(self._read_action_mask())):
            self._empty_mask_pending = True

        if self._empty_mask_pending:
            self._empty_mask_pending = False
            info = dict(self._last_info)
            info["simulator_dead_end"] = "empty_action_mask"
            info["synthetic_sentinel_action"] = self.sentinel_action
            return self._finish_transition(
                observation=self._last_observation,
                raw_reward=0.0,
                terminated=False,
                truncated=True,
                raw_info=info,
                outcome_source="empty_action_mask",
            )

        observation, raw_reward, terminated, truncated, raw_info = self.env.step(action)
        info = dict(raw_info)
        floor = _finite_number(info.get("floor"), default=-1.0)
        if (
            self.max_floor is not None
            and not terminated
            and not truncated
            and floor >= self.max_floor
        ):
            truncated = True
            info["curriculum_truncated"] = True
            info["curriculum_max_floor"] = self.max_floor
            outcome_source = "max_floor"
        else:
            outcome_source = "environment"

        return self._finish_transition(
            observation=observation,
            raw_reward=float(raw_reward),
            terminated=bool(terminated),
            truncated=bool(truncated),
            raw_info=info,
            outcome_source=outcome_source,
        )

    def _finish_transition(
        self,
        *,
        observation: Any,
        raw_reward: float,
        terminated: bool,
        truncated: bool,
        raw_info: Mapping[str, Any],
        outcome_source: str,
    ):
        info = self._normalize_info(
            raw_info,
            terminated=terminated,
            truncated=truncated,
            outcome_source=outcome_source,
        )
        observed_next_potential = run_potential(info, self.reward_config)
        # Terminal potential is defined as zero.  A truncation is a rollout
        # boundary rather than an MDP terminal and therefore keeps its
        # observable next-state potential for correct bootstrapping.
        next_potential = 0.0 if terminated else observed_next_potential
        scaled_reward = self.reward_config.combat_reward_scale * raw_reward
        potential_reward = (
            self.reward_config.gamma * next_potential - self._previous_potential
        )
        if terminated:
            terminal_reward = (
                self.reward_config.terminal_win_reward
                if info["run_won"]
                else self.reward_config.terminal_death_reward
            )
        else:
            terminal_reward = 0.0

        # ---- review-item-4 event components (all logged separately) ----
        next_floor = _floor_number(info)
        first_floor_advance_reward = 0.0
        boundary_success_reward = 0.0
        if (
            not self._first_floor_advance_fired
            and next_floor > 1
            and next_floor > self._max_floor_seen
        ):
            self._first_floor_advance_fired = True
            first_floor_advance_reward = self.reward_config.first_floor_advance_reward
        if _is_boundary_node(info) and next_floor not in self._boundary_floors:
            self._boundary_floors.add(next_floor)
            boundary_success_reward = self.reward_config.boundary_success_reward
        self._max_floor_seen = max(self._max_floor_seen, next_floor)
        # Step cost applies to ongoing decisions only: a truncation is either
        # an environment dead end (never the policy's fault) or a curriculum
        # boundary (a stage completion), and neither should be taxed.
        step_cost = (
            0.0
            if terminated or truncated
            else self.reward_config.step_cost
        )

        shaped_reward = (
            scaled_reward
            + potential_reward
            + terminal_reward
            + first_floor_advance_reward
            + boundary_success_reward
            - step_cost
        )

        info.update(
            {
                "raw_reward": raw_reward,
                "scaled_combat_reward": scaled_reward,
                "potential_before": self._previous_potential,
                "potential_after": observed_next_potential,
                "potential_reward": potential_reward,
                "terminal_reward": terminal_reward,
                "first_floor_advance_reward": first_floor_advance_reward,
                "boundary_success_reward": boundary_success_reward,
                "step_cost": step_cost,
                "shaped_reward": shaped_reward,
            }
        )
        self._previous_potential = observed_next_potential
        self._last_observation = observation
        self._last_info = info
        self._episode_done = bool(terminated or truncated)
        return observation, float(shaped_reward), terminated, truncated, info

    def _normalize_info(
        self,
        raw_info: Mapping[str, Any] | None,
        *,
        terminated: bool,
        truncated: bool,
        outcome_source: str,
    ) -> dict[str, Any]:
        info = dict(raw_info or {})
        simulator_player_won = bool(
            info.get("simulator_player_won", info.get("player_won", False))
        )
        victory_probe = dict(info)
        victory_probe["player_won"] = simulator_player_won
        run_won = is_run_victory(
            terminated=terminated, truncated=truncated, info=victory_probe
        )
        if terminated:
            outcome = "win" if run_won else "loss"
        elif truncated:
            outcome = "truncated"
        else:
            outcome = "ongoing"
        info.update(
            {
                # Preserve the faulty upstream flag under an explicit name,
                # then make player_won safe for existing evaluators.
                "simulator_player_won": simulator_player_won,
                "player_won": run_won,
                "run_won": run_won,
                "run_terminated": bool(terminated),
                "run_truncated": bool(truncated),
                "run_outcome": outcome,
                "run_outcome_source": outcome_source,
            }
        )
        return info

    def _read_action_mask(self) -> np.ndarray:
        provider = getattr(self.env, "action_masks", None)
        if not callable(provider):
            raise TypeError("wrapped environment must expose action_masks()")
        mask = np.asarray(provider(), dtype=bool)
        if mask.ndim != 1 or len(mask) != self._action_count:
            raise ValueError(
                "action mask must be one-dimensional and match action_space.n "
                f"({self._action_count}), got shape {mask.shape}"
            )
        return mask.copy()

    def _require_active_episode(self) -> None:
        if self._episode_done:
            raise RuntimeError("reset() must be called before action_masks() or step()")


__all__ = [
    "V2RewardConfig",
    "V2RunEnvWrapper",
    "is_run_victory",
    "run_potential",
]
