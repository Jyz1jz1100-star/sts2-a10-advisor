"""V2 flat ``(action, target)`` action space over a native-like run core.

This layer turns the emulator's two-argument combat API into one fixed-width
Discrete space that MaskablePPO, the search teacher, and behavior cloning can
all share:

    flat = base_action * (MAX_ENEMIES + 1) + target_slot

``target_slot`` 0 means "no explicit target" (native ``-1``); slots 1..6 are
the absolute enemy indices 0..5.  One final index is a sentinel reserved for
the environment-contract wrapper's empty-mask truncation.

Candidate masks are a pure function of the current raw observation, so the
same state always yields the same flat mask (replay-verifiable).  Per-enemy
aliases exist only where they change the transition: combat-phase hand slots
holding a single-target card while two or more enemies are alive.  End turn,
potions, AoE cards, and every non-combat action keep exactly one candidate,
which honours the codec's no-alias contract.

The ``core`` object is injectable; the native adapter lives in
``v2_native_env`` and tests use scripted doubles.  A native-layer rejection
(``status != 0`` for a mask-legal action) is reported as a *classified*
truncation instead of V1's silent ``(-1, False, False)`` spin.
"""

from __future__ import annotations

from typing import Any, Protocol

import gymnasium as gym
import numpy as np
from gymnasium import spaces

from advisor_core.action_codec_v2 import ActionTargetCodecV2, EnemyTarget

from .v2_constants import (
    COMBAT_OBS_SIZE,
    MAP_CHOICES,
    MAX_ENEMIES,
    MAX_HAND,
    PHASE_COMBAT,
    PHASE_NAMES,
    RUN_MAX_ACTIONS,
)
from .v2_observation import OBS_SIZE, build_observation


class RunCore(Protocol):
    """Minimal native-like surface required by :class:`V2FlatActionEnv`."""

    def reset(self, seed: int | str) -> tuple[np.ndarray, dict[str, Any], int]: ...

    def step(
        self, action: int, target: int
    ) -> tuple[np.ndarray, float, bool, bool, dict[str, Any], int]: ...

    def action_mask(self) -> np.ndarray: ...

    def state_lists(self) -> dict[str, tuple[int, ...]]: ...

    def close(self) -> None: ...


TARGET_SLOTS = MAX_ENEMIES + 1  # none + six enemy indices
FLAT_SIZE = RUN_MAX_ACTIONS * TARGET_SLOTS + 1
SENTINEL_FLAT = FLAT_SIZE - 1
#: ``Sts2Run_Step`` status meaning "the native layer rejected this action".
#: ``RunEngine.Step`` returns -1 for mask-offered but engine-invalid actions
#: (the event/shop mask/native disagreements), while the upstream wrapper
#: silently turned them into a -1 reward spin.
NATIVE_STATUS_REJECTED = -1


def flat_index(action: int, target: int | None) -> int:
    slot = 0 if target is None or target < 0 else int(target) + 1
    if not 0 <= slot < TARGET_SLOTS:
        raise ValueError(f"target slot {slot} is outside the enemy range")
    return int(action) * TARGET_SLOTS + slot


def decode_flat(flat: int) -> tuple[int, int]:
    """Return ``(action, target)`` with native ``-1`` for the no-target slot."""

    action, slot = divmod(int(flat), TARGET_SLOTS)
    if slot >= TARGET_SLOTS or flat >= SENTINEL_FLAT:
        raise ValueError(f"flat index {flat} does not encode a base action")
    return action, -1 if slot == 0 else slot - 1


class V2FlatActionEnv(gym.Env):
    """Fixed-width (action, target) environment with the expanded observation."""

    metadata = {"render_modes": []}

    def __init__(self, core: RunCore) -> None:
        super().__init__()
        self._core = core
        # Bounds are deliberately signed: buff magnitudes (temporary strength
        # down), map coordinates (-1 fillers), and shop costs legitimately go
        # negative inside the native passthrough blocks.
        self.observation_space = spaces.Box(
            low=-(2**15), high=2**15, shape=(OBS_SIZE,), dtype=np.int32
        )
        self.action_space = spaces.Discrete(FLAT_SIZE)
        self._raw_obs: np.ndarray | None = None
        self._expanded: np.ndarray | None = None
        self._info: dict[str, Any] = {}
        self._episode_done = True

    # ------------------------------------------------------------------ core

    def reset(self, *, seed: int | str | None = None, options: dict | None = None):
        if seed is None:
            # Silent fallback to a fixed seed would poison the whole seed
            # contract; the seed-rotation wrapper must always pass one.
            raise ValueError("V2FlatActionEnv.reset requires an explicit seed")
        raw, info, status = self._core.reset(seed)
        if status != 0:
            raise RuntimeError(f"native run_reset failed with status {status}")
        self._accept(raw, info)
        self._episode_done = False
        return self._expanded.copy(), dict(self._info)

    def step(self, flat_action: int, target: int = -1):
        # ``target`` exists only for call-compatibility with the search
        # teacher's ``(action, target)`` protocol: in the flat space the
        # chosen enemy is already encoded inside ``flat_action``, so a second
        # non-sentinel target argument would silently double-apply.
        if target is not None and int(target) != -1:
            raise ValueError(
                "the flat action already encodes the target; pass target=-1"
            )
        if self._episode_done:
            raise RuntimeError("reset() must be called before step()")
        flat = int(flat_action)
        mask = self.action_masks()
        if not 0 <= flat < FLAT_SIZE or not bool(mask[flat]):
            # The contract wrapper advertises the sentinel itself; anything
            # else arriving here is a caller bug or a policy violating the
            # mask, and must never be hidden inside the simulator.
            raise ValueError(
                f"flat action {flat} is not legal in this state; "
                "the V2 contract requires mask-respecting actions"
            )
        if flat == SENTINEL_FLAT:
            self._episode_done = True
            return self._expanded.copy(), -1.0, False, True, self._classify(
                "empty_action_mask"
            )

        action, target = decode_flat(flat)
        raw, reward, terminated, truncated, info, status = self._core.step(
            action, target
        )
        if status != 0:
            # Native says this action/target pair is illegal even though the
            # native mask offered it: a classified emulator disagreement.
            # The state is unchanged, so end the episode as a truncation with
            # zero reward -- it is the environment's fault, not the policy's,
            # and V1's silent -1-per-step rejection spin must never return.
            self._episode_done = True
            rejection = self._classify("native_rejection")
            rejection["rejected_flat_action"] = flat
            rejection["rejected_action"] = action
            rejection["rejected_target"] = target
            rejection["native_status"] = int(status)
            return self._expanded.copy(), 0.0, False, True, rejection

        self._accept(raw, info)
        self._episode_done = bool(terminated or truncated)
        return self._expanded.copy(), float(reward), bool(terminated), bool(truncated), dict(
            self._info
        )

    def action_masks(self) -> np.ndarray:
        """Flat candidate mask; at a native dead end only the sentinel is on.

        MaskablePPO cannot sample from an all-false mask, so the env must
        always advertise at least one candidate.  The sentinel is that floor:
        ``step()`` intercepts it here (one truncating transition, labelled
        ``empty_action_mask``) and the contract wrapper keeps an equivalent
        interception as a defence in depth when it sits above this env.
        """

        if self._raw_obs is None:
            if self._episode_done:
                raise RuntimeError("reset() must be called before action_masks()")
            return np.zeros(FLAT_SIZE, dtype=bool)
        base = self._core.action_mask()
        base = np.asarray(base, dtype=bool)
        if base.shape != (RUN_MAX_ACTIONS,):
            raise ValueError(f"native mask must have {RUN_MAX_ACTIONS} entries")
        mask = np.zeros(FLAT_SIZE, dtype=bool)
        if not bool(base.any()):
            mask[SENTINEL_FLAT] = True
            return mask

        phase = int(self._raw_obs[COMBAT_OBS_SIZE])
        alive = self._alive_enemy_indices()
        targeted = self._single_target_hand_actions() if phase == PHASE_COMBAT else set()
        # With two or more living enemies, an explicit target changes the
        # transition, so a single-target action exposes exactly one candidate
        # per living enemy.  The "no target" alias would resolve to the first
        # living enemy in the engine, duplicating that enemy's candidate, so
        # it is withheld here (codec's no-alias contract).
        split_targets = len(alive) >= 2
        for action in np.flatnonzero(base):
            action = int(action)
            if action in targeted and split_targets:
                for enemy_index in alive:
                    mask[flat_index(action, enemy_index)] = True
            else:
                mask[flat_index(action, None)] = True
        return mask

    def codec(self) -> ActionTargetCodecV2:
        """Candidate codec for the current state, for teacher/BC labelling."""

        if self._raw_obs is None:
            raise RuntimeError("no active observation")
        base = np.asarray(self._core.action_mask(), dtype=bool)
        flat_mask = self.action_masks()
        phase = int(self._raw_obs[COMBAT_OBS_SIZE])
        alive = self._alive_enemy_indices()
        # Match the flat-mask rule exactly: a single living enemy makes an
        # explicit target a duplicate of the engine default, so no aliases.
        targeted = (
            self._single_target_hand_actions()
            if phase == PHASE_COMBAT and len(alive) >= 2
            else set()
        )
        encounter = self._info.get("encounter_id", -1)
        targets = tuple(
            EnemyTarget(index, f"{int(encounter)}/enemy-{index}") for index in alive
        )
        keys, labels = self._action_names(phase)
        candidates = ActionTargetCodecV2(
            base,
            sorted(targeted),
            targets,
            action_keys=keys,
            action_labels=labels,
        )
        if len(candidates) != int(flat_mask.sum()):
            raise AssertionError(
                "codec candidates and flat mask disagree; contract bug"
            )
        return candidates

    def close(self) -> None:
        self._core.close()

    # --------------------------------------------------------------- helpers

    def _accept(self, raw: np.ndarray, info: dict[str, Any]) -> None:
        raw = np.asarray(raw, dtype=np.int32)
        lists = self._core.state_lists()
        self._raw_obs = raw
        self._expanded = build_observation(
            raw,
            deck=lists["deck"],
            relics=lists["relics"],
            potions=lists["potions"],
            neow_options=lists["neow_options"],
            reward_upgraded=lists["reward_upgraded"],
            pending_rewards=lists["pending_rewards"],
            map_coords=lists["map_coords"],
            shop_cards=lists["shop_cards"],
            shop_costs=lists["shop_costs"],
        )
        info = dict(info)
        info["phase_name"] = PHASE_NAMES[
            min(int(raw[COMBAT_OBS_SIZE]), len(PHASE_NAMES) - 1)
        ]
        self._info = info

    def _classify(self, reason: str) -> dict[str, Any]:
        info = dict(self._info)
        info["simulator_dead_end"] = reason
        self._info = info
        return info

    def _alive_enemy_indices(self) -> list[int]:
        obs = self._raw_obs
        alive: list[int] = []
        enemy_base = 54  # CombatObservation enemy block start
        for slot in range(MAX_ENEMIES):
            hp = int(obs[enemy_base + slot * 15])
            max_hp = int(obs[enemy_base + slot * 15 + 1])
            if max_hp > 0 and hp > 0:
                alive.append(slot)
        return alive

    def _single_target_hand_actions(self) -> set[int]:
        from advisor_core.card_targeting_v2 import is_single_target

        obs = self._raw_obs
        targeted: set[int] = set()
        for index in range(MAX_HAND):
            def_id = int(obs[8 + index * 2])
            if def_id != 0 and is_single_target(def_id):
                targeted.add(index)
        return targeted

    def _action_names(self, phase: int) -> tuple[dict[int, str], dict[int, str]]:
        obs = self._raw_obs
        keys: dict[int, str] = {}
        labels: dict[int, str] = {}
        if phase == PHASE_COMBAT:
            hand_ids = [int(obs[8 + i * 2]) for i in range(MAX_HAND) if obs[8 + i * 2]]
            hand_count = len(hand_ids)
            for position, def_id in enumerate(hand_ids):
                keys[position] = f"card:{def_id}@hand{position}"
                labels[position] = f"出牌槽{position}(卡{def_id})"
            keys[hand_count] = "end_turn"
            labels[hand_count] = "结束回合"
            potion_ids = [int(obs[28 + i * 2]) for i in range(3) if obs[28 + i * 2]]
            for slot, potion_id in enumerate(potion_ids):
                action = hand_count + 1 + slot
                keys[action] = f"potion:{potion_id}@slot{slot}"
                labels[action] = f"使用药水{potion_id}"
        elif phase == 2:  # map
            for option in range(MAP_CHOICES):
                node_type = int(obs[COMBAT_OBS_SIZE + 12 + option])
                if node_type != 0:
                    keys[option] = f"map:{option}:{node_type}"
                    labels[option] = f"地图路线{option}(节点类型{node_type})"
        elif phase == 4:  # shop
            for action, label in (
                *[(i, f"购买商店卡{i}") for i in range(7)],
                (7, "购买遗物1"),
                (8, "购买遗物2"),
                (9, "购买遗物3"),
                (10, "购买药水1"),
                (11, "购买药水2"),
                (12, "购买药水3"),
                (13, "移除卡牌"),
                (14, "离开商店"),
            ):
                keys[action] = f"shop:{action}"
                labels[action] = label
        else:
            keys[3] = "proceed"
            labels[3] = "继续"
        _ = obs  # silence unused in some branches
        return keys, labels


__all__ = [
    "FLAT_SIZE",
    "NATIVE_STATUS_REJECTED",
    "RunCore",
    "SENTINEL_FLAT",
    "TARGET_SLOTS",
    "V2FlatActionEnv",
    "decode_flat",
    "flat_index",
]
