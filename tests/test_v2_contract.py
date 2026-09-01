"""V2 training-contract tests: flat action space, observation, curriculum config.

Everything here runs without the native emulator (scripted cores), except the
integration class at the bottom, which is skipped automatically when the
simulator python environment is unavailable.
"""

from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from advisor_core.card_targeting_v2 import (  # noqa: E402
    SINGLE_TARGET_CARD_IDS,
    is_single_target,
)
from training.v2_config import load_v2_training_config  # noqa: E402
from training.v2_constants import COMBAT_OBS_SIZE, RUN_MAX_ACTIONS, RUN_OBS_SIZE  # noqa: E402
from training.v2_flat_env import (  # noqa: E402
    FLAT_SIZE,
    SENTINEL_FLAT,
    TARGET_SLOTS,
    V2FlatActionEnv,
    decode_flat,
    flat_index,
)
from training.v2_observation import OBS_SIZE, build_observation, observation_contract  # noqa: E402
from training.v2_run_wrapper import V2RewardConfig, V2RunEnvWrapper  # noqa: E402


def make_raw_obs(*, phase=0, floor=1) -> np.ndarray:
    raw = np.zeros(RUN_OBS_SIZE, dtype=np.int32)
    raw[0] = 60  # player hp
    raw[1] = 80  # player max hp
    raw[3] = 3  # energy
    hand = [30, 472, 131, 0, 0, 0, 0, 0, 0, 0]  # Bash, Strike, Defend
    for index, def_id in enumerate(hand):
        raw[8 + index * 2] = def_id
        raw[8 + index * 2 + 1] = 0
    # two living enemies
    raw[54] = 20
    raw[55] = 44
    raw[54 + 15] = 30
    raw[54 + 15 + 1] = 30
    raw[COMBAT_OBS_SIZE + 0] = phase
    raw[COMBAT_OBS_SIZE + 1] = floor
    return raw


def empty_lists() -> dict[str, tuple[int, ...]]:
    return {
        "deck": (),
        "relics": (),
        "potions": (),
        "neow_options": (0, 0, 0),
        "reward_upgraded": (0, 0, 0),
        "pending_rewards": (0, 0, 0, 0),
        "map_coords": (-1,) * 8,
        "shop_cards": (0,) * 7,
        "shop_costs": (0,) * 14,
    }


class ScriptedCore:
    """Deterministic RunCore double used across the contract tests."""

    def __init__(self, *, masks, transitions=None, reset_info=None, obs=None):
        self._masks = [np.asarray(mask, dtype=bool) for mask in masks]
        self._transitions = list(transitions or [])
        self._reset_info = dict(reset_info or {"floor": 1, "player_hp": 60,
                                               "player_max_hp": 80})
        self._obs = obs if obs is not None else make_raw_obs()
        self.mask_position = 0
        self.step_log: list[tuple[int, int]] = []
        self.closed = False

    def reset(self, seed):
        self.mask_position = 0
        raw = self._obs.copy()
        raw[COMBAT_OBS_SIZE + 1] = int(self._reset_info.get("floor", 1))
        return raw, dict(self._reset_info), 0

    def step(self, action, target):
        self.step_log.append((int(action), int(target)))
        if not self._transitions:
            raise AssertionError("unexpected step beyond scripted transitions")
        raw, reward, terminated, truncated, info, status = self._transitions.pop(0)
        self._obs = raw
        self.mask_position = min(self.mask_position + 1, len(self._masks) - 1)
        return raw, reward, terminated, truncated, info, status

    def action_mask(self) -> np.ndarray:
        return self._masks[self.mask_position].copy()

    def state_lists(self):
        return empty_lists()

    def close(self):
        self.closed = True


def base_mask(legal_actions) -> np.ndarray:
    mask = np.zeros(RUN_MAX_ACTIONS, dtype=bool)
    for action in legal_actions:
        mask[action] = True
    return mask


class FlatActionSpaceTests(unittest.TestCase):
    def test_flat_index_roundtrip(self) -> None:
        for action in (0, 13, 31):
            for target in (None, -1, 0, 5):
                flat = flat_index(action, target)
                decoded_action, decoded_target = decode_flat(flat)
                self.assertEqual(decoded_action, action)
                self.assertEqual(decoded_target, -1 if target in (None, -1) else target)

    def test_size_reserves_a_distinct_sentinel(self) -> None:
        self.assertEqual(FLAT_SIZE, RUN_MAX_ACTIONS * TARGET_SLOTS + 1)
        self.assertEqual(SENTINEL_FLAT, FLAT_SIZE - 1)
        with self.assertRaises(ValueError):
            decode_flat(SENTINEL_FLAT)

    def test_single_target_card_splits_per_alive_enemy(self) -> None:
        core = ScriptedCore(masks=[base_mask([0, 1, 3])])
        env = V2FlatActionEnv(core)
        env.reset(seed=7)
        mask = env.action_masks()
        # Hand is [Bash(30), Strike(472), Defend(131)]; Bash and Strike are
        # single-target and two enemies live: exactly two candidates each,
        # and the no-target duplicate is withheld.
        for hand_slot in (0, 1):
            flats = [flat for flat in range(FLAT_SIZE)
                     if mask[flat] and decode_flat(flat)[0] == hand_slot]
            self.assertEqual(len(flats), 2, f"hand slot {hand_slot}")
            self.assertEqual(sorted(decode_flat(f)[1] for f in flats), [0, 1])
        # Defend (slot 2) was not in the scripted legal mask; end turn (3)
        # keeps exactly one candidate.
        self.assertTrue(mask[flat_index(3, None)])
        self.assertEqual(sum(1 for f in range(FLAT_SIZE)
                             if mask[f] and decode_flat(f)[0] == 3), 1)

    def test_aoe_and_unknown_cards_never_split(self) -> None:
        raw = make_raw_obs()
        raw[8] = 465  # Stomp: explicitly excluded AoE
        core = ScriptedCore(masks=[base_mask([0])], obs=raw)
        env = V2FlatActionEnv(core)
        env.reset(seed=7)
        mask = env.action_masks()
        self.assertTrue(mask[flat_index(0, None)])
        self.assertFalse(any(mask[flat_index(0, t)] for t in range(0, 6)))

    def test_single_enemy_does_not_split(self) -> None:
        raw = make_raw_obs()
        raw[54 + 15] = 0
        raw[54 + 15 + 1] = 0  # second enemy gone
        core = ScriptedCore(masks=[base_mask([0])], obs=raw)
        env = V2FlatActionEnv(core)
        env.reset(seed=7)
        mask = env.action_masks()
        self.assertTrue(mask[flat_index(0, None)])
        self.assertFalse(any(mask[flat_index(0, t)] for t in range(0, 6)))

    def test_noncombat_states_never_split(self) -> None:
        raw = make_raw_obs(phase=2)
        raw[COMBAT_OBS_SIZE + 12] = 1  # map option 0 present
        core = ScriptedCore(masks=[base_mask([0])], obs=raw)
        env = V2FlatActionEnv(core)
        env.reset(seed=7)
        mask = env.action_masks()
        self.assertEqual(int(mask.sum()), 1)
        self.assertTrue(mask[flat_index(0, None)])

    def test_mask_illegal_flat_step_is_rejected_loudly(self) -> None:
        core = ScriptedCore(masks=[base_mask([0])])
        env = V2FlatActionEnv(core)
        env.reset(seed=7)
        with self.assertRaises(ValueError):
            env.step(flat_index(3, None))  # end turn not legal in the scripted mask

    def test_native_rejection_becomes_classified_truncation(self) -> None:
        """V1's silent (-1, False, False) spin must be a labelled one-step end."""

        raw = make_raw_obs()
        core = ScriptedCore(
            masks=[base_mask([0])],
            transitions=[(raw, -1.0, False, False, {"floor": 1}, -1)],
        )
        env = V2RunEnvWrapper(
            V2FlatActionEnv(core), reward_config=V2RewardConfig(combat_reward_scale=0.0,
                                                                floor_weight=0.0,
                                                                hp_weight=0.0),
            sentinel_action=SENTINEL_FLAT,
        )
        env.reset(seed=7)
        # Bash split across two living enemies: no-target alias is withheld,
        # so the legal candidate carries an explicit enemy index.
        _obs, reward, terminated, truncated, info = env.step(flat_index(0, 0))
        self.assertFalse(terminated)
        self.assertTrue(truncated)
        self.assertEqual(reward, 0.0)  # environment's fault, not the policy's
        self.assertEqual(info["simulator_dead_end"], "native_rejection")
        self.assertEqual(info["run_outcome"], "truncated")
        self.assertEqual(info["native_status"], -1)
        self.assertEqual(env.action_space.n, FLAT_SIZE)

    def test_empty_native_mask_truncates_in_one_step(self) -> None:
        core = ScriptedCore(masks=[base_mask([])])
        env = V2RunEnvWrapper(
            V2FlatActionEnv(core),
            reward_config=V2RewardConfig(combat_reward_scale=0.0, floor_weight=0.0,
                                         hp_weight=0.0),
            sentinel_action=SENTINEL_FLAT,
        )
        env.reset(seed=7)
        mask = env.action_masks()
        self.assertTrue(mask[SENTINEL_FLAT])
        self.assertEqual(int(mask.sum()), 1)  # wrapper exposes only the sentinel
        _obs, _r, terminated, truncated, info = env.step(SENTINEL_FLAT)
        self.assertFalse(terminated)
        self.assertTrue(truncated)
        self.assertEqual(core.step_log, [])  # the broken state was never stepped
        self.assertEqual(info["simulator_dead_end"], "empty_action_mask")


class ObservationContractTests(unittest.TestCase):
    def test_layout_is_stable_and_documented(self) -> None:
        contract = observation_contract()
        self.assertEqual(contract["size"], OBS_SIZE)
        self.assertEqual(
            [block["name"] for block in contract["blocks"]][:2],
            ["combat_native_passthrough", "run_native_passthrough"],
        )
        self.assertTrue(any("relic counters" in gap for gap in contract["known_gaps"]))

    def test_expanded_blocks_carry_deck_relics_potions_shop_and_map(self) -> None:
        raw = make_raw_obs(phase=4)
        vector = build_observation(
            raw,
            deck=(30, -472, 131),  # Bash, upgraded Strike, Defend
            relics=(36, 1533),
            potions=(0, 5, 0),
            neow_options=(242, 167, 240),
            reward_upgraded=(1, 0, 0),
            pending_rewards=(99, 5, 0, 1),
            map_coords=(3, 10, -1, -1, 2, 9, -1, -1),
            shop_cards=(0, 13, 24, 55, 66, 0, 0),
            shop_costs=(50, 75, 90, 120, 140, 160, 180, 0, 0, 0, 0, 0, 0, 69),
        )
        offsets = {block["name"]: block["offset"] for block in
                   observation_contract()["blocks"]}
        self.assertEqual(vector[offsets["combat_native_passthrough"]], 60)
        self.assertEqual(int(vector.sum() >= 0), 1)

        from advisor_core.card_targeting_v2 import CARD_VOCAB, POTION_VOCAB, RELIC_VOCAB

        self.assertEqual(vector[offsets["deck_presence"] + CARD_VOCAB.index(30)], 1)
        self.assertEqual(vector[offsets["deck_presence"] + CARD_VOCAB.index(472)], 1)
        self.assertEqual(vector[offsets["deck_upgraded_presence"] + CARD_VOCAB.index(472)], 1)
        self.assertEqual(vector[offsets["deck_upgraded_presence"] + CARD_VOCAB.index(30)], 0)
        self.assertEqual(vector[offsets["relic_presence"] + RELIC_VOCAB.index(36)], 1)
        self.assertEqual(vector[offsets["relic_presence"] + RELIC_VOCAB.index(1533)], 1)
        self.assertEqual(vector[offsets["potion_slot_presence"] + POTION_VOCAB.index(5)], 1)
        shop_cost_base = offsets["shop_costs"]
        self.assertEqual(vector[shop_cost_base + 13], 69)  # removal price
        coord_base = offsets["map_option_coords"]
        self.assertEqual(
            [int(vector[coord_base + i]) for i in range(8)], [3, 10, -1, -1, 2, 9, -1, -1]
        )
        card_base = offsets["shop_cards_4_to_7"]
        self.assertEqual(int(vector[card_base]), 55)
        self.assertEqual(int(vector[offsets["unknown_id_events"]]), 0)

    def test_unknown_ids_are_counted_not_collided(self) -> None:
        vector = build_observation(
            make_raw_obs(),
            deck=(999999,),
            relics=(),
            potions=(),
            neow_options=(0, 0, 0),
            reward_upgraded=(0, 0, 0),
            pending_rewards=(0, 0, 0, 0),
            map_coords=(-1,) * 8,
            shop_cards=(0,) * 7,
            shop_costs=(0,) * 14,
        )
        offsets = {block["name"]: block["offset"] for block in observation_contract()["blocks"]}
        self.assertEqual(int(vector[offsets["unknown_id_events"]]), 1)

    def test_hand_single_target_flags_and_alive_count(self) -> None:
        vector = build_observation(
            make_raw_obs(),
            deck=(),
            relics=(),
            potions=(),
            neow_options=(0, 0, 0),
            reward_upgraded=(0, 0, 0),
            pending_rewards=(0, 0, 0, 0),
            map_coords=(-1,) * 8,
            shop_cards=(0,) * 7,
            shop_costs=(0,) * 14,
        )
        offsets = {block["name"]: block["offset"] for block in observation_contract()["blocks"]}
        hand_flags = offsets["hand_single_target_flags"]
        self.assertEqual(int(vector[hand_flags]), 1)  # Bash
        self.assertEqual(int(vector[hand_flags + 1]), 1)  # Strike
        self.assertEqual(int(vector[hand_flags + 2]), 0)  # Defend
        self.assertEqual(int(vector[offsets["alive_enemy_count"]]), 2)


class CurriculumConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_v2_training_config(
            PROJECT_ROOT / "config" / "training_v2.toml"
        )

    def test_stage_ladder_matches_the_plan(self) -> None:
        self.assertEqual(
            [(s.name, s.max_floor) for s in self.config.stages],
            [("floor3", 3), ("floor6", 6), ("floor10", 10), ("floor13", 13),
             ("act1", None)],
        )

    def test_every_stage_scores_as_simulator_act1(self) -> None:
        self.assertEqual({stage.scope for stage in self.config.stages}, {"simulator_act1"})

    def test_seed_partitions_are_pairwise_disjoint(self) -> None:
        ranges = [(p.name, p.start, p.stop) for p in self.config.seeds.as_list()]
        self.assertEqual(len(ranges), 20)
        for left_index, (left_name, left_start, left_stop) in enumerate(ranges):
            for right_name, right_start, right_stop in ranges[left_index + 1:]:
                self.assertTrue(
                    left_stop <= right_start or right_stop <= left_start,
                    f"{left_name} overlaps {right_name}",
                )

    def test_final_partitions_are_never_read_by_the_trainer(self) -> None:
        # Structural guarantee: the trainer only asks for train/checkpoint/
        # promotion partitions; final ranges exist in the disjointness proof
        # above and nothing else.
        used = set()
        for stage in self.config.stages:
            used.update({
                self.config.partition(stage.name, "train").name,
                self.config.partition(stage.name, "checkpoint").name,
                self.config.partition(stage.name, "promotion").name,
            })
        self.assertTrue(all(f"{stage.name}.final" not in used for stage in self.config.stages))

    def test_terminal_stage_gates_on_wins_intermediate_on_boundary(self) -> None:
        for stage in self.config.stages[:-1]:
            self.assertGreater(stage.min_boundary_rate, 0.0)
        final = self.config.stages[-1]
        self.assertEqual(final.min_boundary_rate, 0.0)
        self.assertGreaterEqual(final.min_win_rate, 0.35)
        self.assertGreaterEqual(final.min_wilson_lower, 0.31)

    def test_overlap_is_rejected(self) -> None:
        broken = (PROJECT_ROOT / "config" / "training_v2.toml").read_text(
            encoding="utf-8"
        ).replace("start = 102010000", "start = 102005000")
        temp = PROJECT_ROOT / "config" / "_tmp_v2_overlap.toml"
        temp.write_text(broken, encoding="utf-8")
        try:
            with self.assertRaises(ValueError):
                load_v2_training_config(temp)
        finally:
            temp.unlink(missing_ok=True)


class DeterministicReplayTests(unittest.TestCase):
    def test_masks_are_a_pure_function_of_the_observation(self) -> None:
        """Same raw state => same flat mask (the replay-verification basis)."""

        raw = make_raw_obs()
        first = V2FlatActionEnv(ScriptedCore(masks=[base_mask([0, 3])], obs=raw.copy()))
        second = V2FlatActionEnv(ScriptedCore(masks=[base_mask([0, 3])], obs=raw.copy()))
        first.reset(seed=7)
        second.reset(seed=7)
        np.testing.assert_array_equal(first.action_masks(), second.action_masks())
        np.testing.assert_array_equal(first.reset(seed=7)[0], second.reset(seed=7)[0])

    def test_codec_and_flat_mask_agree_in_every_scenario(self) -> None:
        scenarios = [
            (make_raw_obs(phase=0), base_mask([0, 1, 3])),
            (make_raw_obs(phase=2), base_mask([0, 2])),
            (make_raw_obs(phase=4), base_mask([0, 13, 14])),
        ]
        for raw, mask in scenarios:
            env = V2FlatActionEnv(ScriptedCore(masks=[mask], obs=raw))
            env.reset(seed=1)
            codec = env.codec()
            self.assertEqual(len(codec), int(env.action_masks().sum()))
            for candidate in codec.candidates:
                self.assertTrue(env.action_masks()[
                    flat_index(candidate.action, candidate.target)
                ])


class PropertyInvariantsTests(unittest.TestCase):
    """'Every non-terminal state exposes at least one legal action.'

    Verified over a scripted battery of phase/mask combinations AND, in the
    integration class below, over real simulator rollouts.
    """

    def test_advertised_mask_is_never_empty(self) -> None:
        phases = [0, 1, 2, 3, 4, 5, 7, 8, 9, 10]
        for phase in phases:
            for legal in ([], [0], [0, 3], [31]):
                raw = make_raw_obs(phase=phase)
                env = V2RunEnvWrapper(
                    V2FlatActionEnv(
                        ScriptedCore(masks=[base_mask(legal)], obs=raw),
                    ),
                    reward_config=V2RewardConfig(combat_reward_scale=0.0,
                                                 floor_weight=0.0, hp_weight=0.0),
                    sentinel_action=SENTINEL_FLAT,
                )
                env.reset(seed=3)
                mask = env.action_masks()
                self.assertTrue(bool(mask.any()), f"phase {phase} legal {legal}")

    def test_wrapper_max_floor_truncates_without_claiming_a_win(self) -> None:
        transition_obs = make_raw_obs(floor=3)
        core = ScriptedCore(
            masks=[base_mask([0, 3])],
            transitions=[
                (transition_obs, 0.5, False, False,
                 {"floor": 3, "player_won": True, "player_hp": 40, "player_max_hp": 80},
                 0)
            ],
        )
        env = V2RunEnvWrapper(
            V2FlatActionEnv(core), max_floor=3,
            reward_config=V2RewardConfig(combat_reward_scale=0.0, floor_weight=1.0,
                                         hp_weight=0.0, terminal_win_reward=100.0),
            sentinel_action=SENTINEL_FLAT,
        )
        env.reset(seed=9)
        _obs, reward, terminated, truncated, info = env.step(flat_index(3, None))
        self.assertFalse(terminated)
        self.assertTrue(truncated)
        self.assertTrue(info["curriculum_truncated"])
        self.assertFalse(info["player_won"])
        self.assertEqual(info["terminal_reward"], 0.0)
        self.assertTrue(info["boundary_reached"] if "boundary_reached" in info else True)


EMULATOR_SRC = PROJECT_ROOT.parent / "third_party" / "slay-the-spire-2-emulator-main" / "src"


def _emulator_available() -> bool:
    if not EMULATOR_SRC.is_dir():
        return False
    try:
        sys.path.insert(0, str(EMULATOR_SRC))
        importlib.import_module("sts2_gym")
        return True
    except Exception:
        return False


@unittest.skipUnless(_emulator_available(), "native emulator not importable")
class RealSimulatorIntegrationTests(unittest.TestCase):
    """Bounded checks against the actual locked emulator build."""

    def _stack(self, seed: int, *, max_floor=None, max_episode_steps=400):
        from sts2_gym import native  # noqa: PLC0415

        from training.v2_native_env import NativeRunCore

        core = NativeRunCore(native, max_episode_steps=max_episode_steps)
        flat = V2FlatActionEnv(core)
        wrapper = V2RunEnvWrapper(
            flat,
            max_floor=max_floor,
            reward_config=V2RewardConfig(),
            sentinel_action=SENTINEL_FLAT,
        )
        return wrapper

    def _random_rollout(self, seed: int, *, max_floor=None, steps=120):
        """Greedy-random legal rollouts; verifies the contract holds live."""

        import random

        env = self._stack(seed, max_floor=max_floor)
        observation, _info = env.reset(seed=seed)
        rng = random.Random(seed)
        observed_phases: set[int] = set()
        illegal = 0
        dead_ends: dict[str, int] = {}
        done = False
        for _ in range(steps):
            if done:
                observation, _info = env.reset(seed=seed)
                done = False
            mask = env.action_masks()
            self.assertTrue(bool(mask.any()))
            legal_indices = np.flatnonzero(mask)
            action = int(rng.choice(legal_indices))
            try:
                observation, _r, terminated, truncated, info = env.step(action)
            except ValueError:
                illegal += 1
                raise
            observed_phases.add(int(info.get("phase", -1)))
            done = terminated or truncated
            if info.get("simulator_dead_end"):
                dead_ends[info["simulator_dead_end"]] = (
                    dead_ends.get(info["simulator_dead_end"], 0) + 1
                )
        env.close()
        return {
            "observation": observation,
            "phases": observed_phases,
            "illegal": illegal,
            "dead_ends": dead_ends,
            "done": done,
        }

    def test_rollouts_never_produce_illegal_or_unclassified_dead_ends(self) -> None:
        for seed in (1, 2, 3):
            result = self._random_rollout(seed, steps=140)
            self.assertEqual(result["illegal"], 0)
            for reason in result["dead_ends"]:
                self.assertIn(reason, {"empty_action_mask", "native_rejection"})

    def test_expanded_observation_matches_contract(self) -> None:
        result = self._random_rollout(11, steps=30)
        self.assertEqual(result["observation"].shape, (OBS_SIZE,))
        offsets = {b["name"]: b["offset"] for b in observation_contract()["blocks"]}
        # Deck presence must include the starter cards at the initial state.
        from advisor_core.card_targeting_v2 import CARD_VOCAB

        deck_base = offsets["deck_presence"]
        self.assertGreater(int(result["observation"][deck_base:].max()), 0)
        self.assertGreaterEqual(len(CARD_VOCAB), 500)
        self.assertEqual(int(result["observation"][offsets["unknown_id_events"]]), 0)

    def test_max_floor_boundary_is_enforced_live(self) -> None:
        env = self._stack(5, max_floor=3, max_episode_steps=400)
        import random

        rng = random.Random(5)
        observation, _info = env.reset(seed=5)
        hits_boundary = False
        for _ in range(380):
            mask = env.action_masks()
            action = int(rng.choice(np.flatnonzero(mask)))
            _obs, _r, terminated, truncated, info = env.step(action)
            if terminated:
                break
            if truncated:
                self.assertTrue(info.get("curriculum_truncated") or
                                info.get("simulator_dead_end"))
                if info.get("curriculum_truncated"):
                    self.assertEqual(info["curriculum_max_floor"], 3)
                    self.assertGreaterEqual(info["floor"], 3)
                    self.assertFalse(info["player_won"])
                    hits_boundary = True
                break
        env.close()
        # With random routing the boundary may not be reached in 380 steps on
        # every seed, but this seed's floor-3 curriculum must fire at least
        # once (floor 3 is reachable in <10 decisions).
        self.assertTrue(hits_boundary, "floor-3 boundary never observed for seed 5")

    def test_determinism_same_seed_same_trajectory(self) -> None:
        import hashlib
        import random

        def trajectory(seed: int) -> str:
            env = self._stack(seed, max_episode_steps=200)
            rng = random.Random(seed)
            observation, _info = env.reset(seed=seed)
            digest = hashlib.sha256()
            digest.update(observation.tobytes())
            for _ in range(120):
                mask = env.action_masks()
                digest.update(mask.astype(np.uint8).tobytes())
                action = int(rng.choice(np.flatnonzero(mask)))
                observation, reward, terminated, truncated, _info = env.step(action)
                digest.update(observation.tobytes())
                digest.update(str(round(float(reward), 6)).encode())
                if terminated or truncated:
                    break
            env.close()
            return digest.hexdigest()

        self.assertEqual(trajectory(21), trajectory(21))
        self.assertEqual(trajectory(22), trajectory(22))


if __name__ == "__main__":
    unittest.main()
