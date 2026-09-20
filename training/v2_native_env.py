"""Native adapter implementing the ``RunCore`` protocol over sts2_gym bindings.

The upstream ``Sts2RunEnv`` keeps the native ``Sts2Run_Step`` status private:
it maps every non-zero status to ``(-1.0, False, False)`` and skips its own
episode counter, which is exactly how the 2026-09-01 evaluation hang happened.
This adapter instead:

* returns the raw status (via the existing ``sts2_gym.native`` wrappers, which
  *do* surface it) so ``V2FlatActionEnv`` can classify a native rejection as a
  one-step truncation with an explicit reason;
* ignores the native ``max_floors`` knob entirely -- enforcing the floor
  boundary is the V2 contract wrapper's job, and the native parameter was one
  of V1's silently-ineffective knobs.  We still apply the episode step limit;
* exposes the raw observation plus every public ``Sts2Run_GetStateList``
  payload, which the flat env turns into the expanded V2 observation.
"""

from __future__ import annotations

import ctypes
from typing import Any

import numpy as np

from .v2_constants import (
    RUN_MAX_ACTIONS,
    RUN_OBS_SIZE,
    STATE_LIST_DECK,
    STATE_LIST_MAP_COORDS,
    STATE_LIST_NEOW,
    STATE_LIST_PENDING_REWARDS,
    STATE_LIST_POTIONS,
    STATE_LIST_RELICS,
    STATE_LIST_REWARD_UPGRADED,
    STATE_LIST_SHOP_CARDS,
    STATE_LIST_SHOP_COSTS,
)


class NativeRunCore:
    """Thin, status-preserving handle around one native run engine."""

    def __init__(self, native: Any, *, max_episode_steps: int = 1200) -> None:
        """``native`` is the ``sts2_gym.native`` module (injectable for tests)."""

        self._native = native
        self._max_episode_steps = max_episode_steps
        self._handle: int | None = None
        self._obs_buf = (ctypes.c_int * RUN_OBS_SIZE)()
        self._rew_buf = (ctypes.c_float * 1)()
        self._term_buf = (ctypes.c_int * 1)()
        self._trunc_buf = (ctypes.c_int * 1)()
        self._elapsed = 0

    # ------------------------------------------------------------- lifecycle

    def reset(
        self, seed: int | str, *, campaign: bool = False
    ) -> tuple[np.ndarray, dict[str, Any], int]:
        if self._handle is not None:
            self._native.run_destroy(self._handle)
        self._handle = self._native.run_create()
        self._elapsed = 0
        status = self._native.run_reset(self._handle, str(seed), self._obs_buf, campaign=campaign)
        return self._obs(), self._info(), int(status)

    def step(
        self, action: int, target: int
    ) -> tuple[np.ndarray, float, bool, bool, dict[str, Any], int]:
        if self._handle is None:
            raise RuntimeError("reset() must be called before step()")
        self._elapsed += 1
        status = self._native.run_step(
            self._handle,
            int(action),
            int(target),
            self._obs_buf,
            self._rew_buf,
            self._term_buf,
            self._trunc_buf,
        )
        terminated = bool(self._term_buf[0])
        truncated = bool(self._trunc_buf[0])
        if not terminated and self._elapsed >= self._max_episode_steps:
            truncated = True
        return (
            self._obs(),
            float(self._rew_buf[0]),
            terminated,
            truncated,
            self._info(),
            int(status),
        )

    def action_mask(self) -> np.ndarray:
        if self._handle is None:
            return np.zeros(RUN_MAX_ACTIONS, dtype=bool)
        mask_buf = self._native.run_action_mask(self._handle, RUN_MAX_ACTIONS)
        return np.ctypeslib.as_array(mask_buf).astype(bool).copy()

    def close(self) -> None:
        if self._handle is not None:
            self._native.run_destroy(self._handle)
            self._handle = None

    # ------------------------------------------------------------------ data

    def state_lists(self) -> dict[str, tuple[int, ...]]:
        if self._handle is None:
            raise RuntimeError("reset() must be called before state_lists()")
        handle = self._handle
        native = self._native
        return {
            "deck": native.run_state_list(handle, STATE_LIST_DECK, 256),
            "relics": native.run_state_list(handle, STATE_LIST_RELICS, 64),
            "potions": native.run_state_list(handle, STATE_LIST_POTIONS, 3),
            "neow_options": native.run_state_list(handle, STATE_LIST_NEOW, 3),
            "reward_upgraded": native.run_state_list(
                handle, STATE_LIST_REWARD_UPGRADED, 3
            ),
            "pending_rewards": native.run_state_list(
                handle, STATE_LIST_PENDING_REWARDS, 4
            ),
            "map_coords": native.run_state_list(handle, STATE_LIST_MAP_COORDS, 8),
            "shop_cards": native.run_state_list(handle, STATE_LIST_SHOP_CARDS, 7),
            "shop_costs": native.run_state_list(
                handle, STATE_LIST_SHOP_COSTS, 14
            ),
        }

    def _obs(self) -> np.ndarray:
        return np.ctypeslib.as_array(self._obs_buf).copy()

    def _info(self) -> dict[str, Any]:
        if self._handle is None:
            raise RuntimeError("reset() must be called before info()")
        info_buf = self._native.run_info(self._handle)
        return {
            "phase": int(info_buf[0]),
            "floor": int(info_buf[1]),
            "act": int(info_buf[2]),
            "deck_size": int(info_buf[3]),
            "gold": int(info_buf[4]),
            "player_hp": int(info_buf[5]),
            "player_max_hp": int(info_buf[6]),
            "relic_count": int(info_buf[7]),
            "current_node_type": int(info_buf[8]),
            "event_id": int(info_buf[9]),
            "relic_reward": int(info_buf[10]),
            "run_cleared": bool(int(info_buf[11])),
            "player_won": bool(self._native.run_player_won(self._handle)),
            "encounter_id": int(self._native.run_encounter_id(self._handle)),
        }


__all__ = ["NativeRunCore"]
