"""Tests for teacher v3: combat beam search + out-of-run long rollouts."""

from __future__ import annotations

import random
import unittest
from pathlib import Path

import numpy as np

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from training.prefix_replay_teacher import (  # noqa: E402
    ActionTarget,
    ReplayPrefix,
    capture_prefix,
)
from training.teacher_batch import TraversalDecision  # noqa: E402
from training.teacher_v3 import (  # noqa: E402
    FROZEN_R2_SEED_RANGES,
    RESERVED_TEACHER_TEST_SEED_START,
    TEACHER_V3_RECORD_VERSION,
    V3_DEFAULT_SEED_START,
    BeamSearchConfig,
    LongRolloutConfig,
    assert_seeds_outside_frozen_lineages,
    average_continuation_scores,
    beam_search_action,
    label_decision_v3,
    long_rollout,
    strength_report_approves,
)
from training.v2_constants import (  # noqa: E402
    COMBAT_OBS_SIZE,
    RUN_MAX_ACTIONS,
    RUN_OBS_SIZE,
)

STRIKE = 30
DEFEND = 131
ENCOUNTER_ID = 17


def _obs(phase: int, *, enemy_hp=(10, 10), player_hp=60) -> np.ndarray:
    raw = np.zeros(RUN_OBS_SIZE, dtype=np.int32)
    raw[0] = player_hp
    raw[1] = 80
    raw[3] = 3
    raw[8] = STRIKE
    raw[10] = DEFEND  # masked out: never playable in this fixture
    for slot, hp in enumerate(enemy_hp):
        raw[54 + slot * 15] = hp
        raw[54 + slot * 15 + 1] = 10
    raw[COMBAT_OBS_SIZE + 0] = phase
    raw[COMBAT_OBS_SIZE + 1] = 5
    return raw


def _mask(legal) -> np.ndarray:
    mask = np.zeros(RUN_MAX_ACTIONS, dtype=bool)
    for action in legal:
        mask[action] = True
    return mask


class TargetMattersCombatEnv:
    """Two-enemy combat where the *target* provably changes the outcome.

    Enemy 0 hits for 5, enemy 1 for 2; both have 10 HP and the hand holds a
    single strike per turn (cards exhaust to discard until end turn).  Killing
    the hard hitter first loses 2 HP at the horizon; killing the weak one
    first loses 5 HP.  The beam search must rank ``strike enemy 0`` first.
    """

    ENEMY_ATK = (5, 2)
    STRIKE_DMG = 10
    MAX_ENEMY_ROUNDS = 4

    def __init__(self, seed, enemy_hp_start=(10, 10)):
        self.seed = int(seed)
        self.enemy_hp_start = tuple(enemy_hp_start)
        self.enemy_hp = list(self.enemy_hp_start)
        self.player_hp = 60
        self.enemy_rounds = 0
        self.strike_played = False
        self._open = False

    def reset(self, *, seed=None):
        self.seed = self.seed if seed is None else int(seed)
        self.enemy_hp = list(self.enemy_hp_start)
        self.player_hp = 60
        self.enemy_rounds = 0
        self.strike_played = False
        self._open = True
        return self._obs(), self._info()

    # -- state helpers ------------------------------------------------------

    def _obs(self, phase: int = 0) -> np.ndarray:
        return _obs(phase, enemy_hp=tuple(self.enemy_hp), player_hp=self.player_hp)

    def _info(self, won: bool = False) -> dict:
        return {
            "player_won": won,
            "floor": 5,
            "player_hp": self.player_hp,
            "player_max_hp": 80,
            "current_node_type": 1,
            "encounter_id": ENCOUNTER_ID,
        }

    # -- RunEnvironment protocol -------------------------------------------

    def action_masks(self) -> np.ndarray:
        legal = [2]  # end turn (hand has two cards -> end turn slot is 2)
        if not self.strike_played:
            legal.insert(0, 0)
        return _mask(legal)

    def step(self, action, target=-1):
        if not self._open:
            raise RuntimeError("reset() required")
        if action == 2:  # end turn: every living enemy strikes back
            self.enemy_rounds += 1
            self.strike_played = False
            reward = 0.0
            for slot, hp in enumerate(self.enemy_hp):
                if hp > 0:
                    self.player_hp -= self.ENEMY_ATK[slot]
                    reward -= 0.1 * self.ENEMY_ATK[slot]
            truncated = self.enemy_rounds >= self.MAX_ENEMY_ROUNDS
            return self._obs(), reward, False, truncated, self._info()
        if action == 0:  # strike the chosen enemy
            alive = [slot for slot, hp in enumerate(self.enemy_hp) if hp > 0]
            if target not in alive:
                target = alive[0] if alive else -1
            if target < 0:
                return self._obs(), 0.0, False, False, self._info()
            self.enemy_hp[target] -= self.STRIKE_DMG
            self.strike_played = True
            killed = self.enemy_hp[target] <= 0
            reward = 0.6 if killed else 0.4
            if all(hp <= 0 for hp in self.enemy_hp):
                return (
                    self._obs(phase=1),
                    reward + 1.0,
                    True,
                    False,
                    self._info(won=True),
                )
            return self._obs(), reward, False, False, self._info()
        return self._obs(), 0.0, False, False, self._info()

    def close(self):
        self._open = False


class TwoPhaseEnv:
    """Map choice then the combat above; choice 0 starts the easier fight."""

    def __init__(self, seed):
        self.seed = int(seed)
        self.combat: TargetMattersCombatEnv | None = None
        self._open = False

    def reset(self, *, seed=None):
        self.seed = self.seed if seed is None else int(seed)
        self.combat = None
        self._open = True
        return _obs(2), {"player_won": False, "floor": 5, "player_hp": 60,
                         "player_max_hp": 80, "current_node_type": 2,
                         "encounter_id": -1}

    def action_masks(self) -> np.ndarray:
        if self.combat is None:
            return _mask([0, 1])
        return self.combat.action_masks()

    def step(self, action, target=-1):
        if self.combat is None:
            easy = action == 0  # option 0 -> weaker enemies
            self.combat = TargetMattersCombatEnv(
                self.seed, enemy_hp_start=(10, 10) if easy else (25, 25)
            )
            observation, info = self.combat.reset(seed=self.seed)
            return observation, 0.0, False, False, info
        return self.combat.step(action, target)

    def close(self):
        self._open = False


def combat_factory(seed):
    return TargetMattersCombatEnv(seed)


def map_factory(seed):
    return TwoPhaseEnv(seed)


def combat_decision(seed: int = 3) -> TraversalDecision:
    return TraversalDecision(
        seed=seed,
        decisions=[],
        raw_obs=_obs(0),
        base_mask=_mask([0, 2]),
        info={"player_won": False, "floor": 5, "player_hp": 60,
              "player_max_hp": 80, "current_node_type": 1,
              "encounter_id": ENCOUNTER_ID},
    )


def map_decision() -> TraversalDecision:
    return TraversalDecision(
        seed=3,
        decisions=[],
        raw_obs=_obs(2),
        base_mask=_mask([0, 1]),
        info={"player_won": False, "floor": 5, "player_hp": 60,
              "player_max_hp": 80, "current_node_type": 2,
              "encounter_id": -1},
    )


def forced_decision() -> TraversalDecision:
    return TraversalDecision(
        seed=3,
        decisions=[],
        raw_obs=_obs(2),
        base_mask=_mask([0]),
        info={"player_won": False, "floor": 5, "player_hp": 60,
              "player_max_hp": 80, "current_node_type": 2,
              "encounter_id": -1},
    )


class BeamSearchTests(unittest.TestCase):
    def test_hard_hitter_is_killed_first(self) -> None:
        """The correct target must strictly outrank every alternative."""

        prefix = capture_prefix(3, [], env_factory=combat_factory)
        result = beam_search_action(
            prefix,
            env_factory=combat_factory,
            config=BeamSearchConfig(beam_width=64, opponent_lookahead_turns=1,
                                    max_node_expansions=600),
        )
        self.assertEqual((result.best_action, result.best_target), (0, 0))
        scores = result.root_scores
        self.assertGreater(scores["0:0"], scores["0:1"])
        self.assertGreater(scores["0:1"], scores["2:-1"])
        self.assertGreaterEqual(result.best_score - result.runner_up_score, 0.05)

    def test_lookahead_two_keeps_the_same_winner(self) -> None:
        prefix = capture_prefix(3, [], env_factory=combat_factory)
        result = beam_search_action(
            prefix,
            env_factory=combat_factory,
            config=BeamSearchConfig(beam_width=64, opponent_lookahead_turns=2,
                                    max_node_expansions=2000),
        )
        self.assertEqual((result.best_action, result.best_target), (0, 0))
        self.assertTrue(result.search_done)

    def test_beam_width_one_still_solves_narrow_combat(self) -> None:
        prefix = capture_prefix(3, [], env_factory=combat_factory)
        result = beam_search_action(
            prefix,
            env_factory=combat_factory,
            config=BeamSearchConfig(beam_width=1, max_node_expansions=200),
        )
        self.assertEqual((result.best_action, result.best_target), (0, 0))

    def test_invalid_configs_rejected(self) -> None:
        with self.assertRaises(ValueError):
            BeamSearchConfig(beam_width=0)
        with self.assertRaises(ValueError):
            BeamSearchConfig(opponent_lookahead_turns=-1)
        with self.assertRaises(ValueError):
            LongRolloutConfig(continuations=0)


class LongRolloutTests(unittest.TestCase):
    def test_rollout_stops_when_the_next_combat_ends(self) -> None:
        prefix = capture_prefix(3, [], env_factory=map_factory)
        result = long_rollout(
            prefix,
            env_factory=map_factory,
            decisions=(ActionTarget(0, -1),),  # force the easy fight
            config=LongRolloutConfig(max_steps=50),
            rng=random.Random(7),
        )
        self.assertTrue(result.entered_combat)
        self.assertTrue(result.combat_completed or result.terminated)
        self.assertGreaterEqual(result.steps, 3)

    def test_plain_map_rollout_enters_combat(self) -> None:
        prefix = capture_prefix(3, [], env_factory=map_factory)
        result = long_rollout(
            prefix,
            env_factory=map_factory,
            config=LongRolloutConfig(max_steps=50),
            rng=random.Random(7),
        )
        self.assertTrue(result.entered_combat)
        self.assertGreaterEqual(result.steps, 3)

    def test_continuation_average_is_mean_of_seeds(self) -> None:
        prefix = capture_prefix(3, [], env_factory=combat_factory)
        payload = average_continuation_scores(
            prefix,
            env_factory=combat_factory,
            decisions=(ActionTarget(0, 0),),
            config=LongRolloutConfig(continuations=3, max_steps=20),
            rng_seeds=(11, 22, 33),
        )
        self.assertEqual(len(payload["per_seed"]), 3)
        mean = sum(item["score"] for item in payload["per_seed"]) / 3
        self.assertAlmostEqual(payload["mean_score"], mean)
        repeat = average_continuation_scores(
            prefix,
            env_factory=combat_factory,
            decisions=(ActionTarget(0, 0),),
            config=LongRolloutConfig(continuations=3, max_steps=20),
            rng_seeds=(11, 22, 33),
        )
        self.assertEqual(payload, repeat)

    def test_seed_count_must_match_continuations(self) -> None:
        prefix = capture_prefix(3, [], env_factory=combat_factory)
        with self.assertRaises(ValueError):
            average_continuation_scores(
                prefix,
                env_factory=combat_factory,
                config=LongRolloutConfig(continuations=3),
                rng_seeds=(1, 2),
            )


class V3RecordTests(unittest.TestCase):
    def test_combat_record_matches_v2_contract(self) -> None:
        from training.teacher_batch import codec_for_state

        record = label_decision_v3(
            combat_decision(),
            env_factory=combat_factory,
            emulator_hash="ab" * 32,
            beam_config=BeamSearchConfig(beam_width=64,
                                         opponent_lookahead_turns=1,
                                         max_node_expansions=600),
            min_score_gap=0.05,
        )
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["record_version"], TEACHER_V3_RECORD_VERSION)
        self.assertEqual(record["scope"], "simulator_act1")
        self.assertIn("not a real-game A10 result", record["disclaimer"])
        self.assertEqual(record["source"], "teacher_v3")
        self.assertEqual(
            ReplayPrefix.from_json(record["prefix"]).sha256,
            record["prefix_sha256"],
        )
        self.assertEqual(record["state_hashes"]["capture"],
                         record["state_hashes"]["reverify"])
        self.assertTrue(record["replay_verified"])
        self.assertGreaterEqual(record["score_gap"], 0.05)
        search = record["teacher"]["search"]
        self.assertEqual(search["mode"], "combat_beam")
        self.assertIn("expert_label", record["teacher"])
        pairs = {
            (item.action, item.target)
            for item in codec_for_state(
                _obs(0), _mask([0, 2]), encounter_id=ENCOUNTER_ID
            ).candidates
        }
        self.assertIn(tuple(record["best_pair"]), pairs)

    def test_beam_budget_recorded_in_record(self) -> None:
        record = label_decision_v3(
            combat_decision(),
            env_factory=combat_factory,
            emulator_hash="ab" * 32,
            beam_config=BeamSearchConfig(beam_width=128,
                                         opponent_lookahead_turns=2,
                                         max_node_expansions=2000),
            min_score_gap=0.05,
        )
        assert record is not None
        self.assertEqual(record["budget"]["beam_width"], 128)
        self.assertEqual(record["budget"]["opponent_lookahead_turns"], 2)
        self.assertEqual(record["teacher"]["search"]["opponent_lookahead_turns"], 2)

    def test_map_decision_labels_by_rollout_mode(self) -> None:
        record = label_decision_v3(
            map_decision(),
            env_factory=map_factory,
            emulator_hash="ab" * 32,
            rollout_config=LongRolloutConfig(continuations=2, max_steps=60),
            min_score_gap=0.01,
        )
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["teacher"]["search"]["mode"], "out_of_run_rollout")
        self.assertGreaterEqual(record["score_gap"], 0.01)
        # Option 0 (the easier fight) must be the labelled choice.
        self.assertEqual(record["best_pair"], [0, -1])

    def test_thin_gap_is_not_exported(self) -> None:
        record = label_decision_v3(
            combat_decision(),
            env_factory=combat_factory,
            emulator_hash="ab" * 32,
            beam_config=BeamSearchConfig(beam_width=64, max_node_expansions=600),
            min_score_gap=10_000.0,
        )
        self.assertIsNone(record)

    def test_forced_state_returns_none(self) -> None:
        record = label_decision_v3(
            forced_decision(),
            env_factory=map_factory,
            emulator_hash="ab" * 32,
        )
        self.assertIsNone(record)


class FreezeGuardTests(unittest.TestCase):
    """The R2 freeze (FREEZE-R2.txt) must be machine-enforced."""

    def test_freeze_notice_names_the_v3_paths(self) -> None:
        notice = (Path(__file__).resolve().parents[1]
                  / "data" / "teacher" / "FREEZE-R2.txt").read_text(encoding="utf-8")
        self.assertIn("FROZEN", notice)
        self.assertIn("training/teacher_v3.py", notice)
        self.assertIn("scripts/evaluate_teacher_strength.py", notice)

    def test_frozen_r2_seeds_are_refused(self) -> None:
        for seed in (1_400_100_000,  # batch0 lineage
                     1_400_217_997,  # batch1 end
                     1_500_100_500):  # dagger0
            with self.assertRaises(ValueError):
                assert_seeds_outside_frozen_lineages([seed])

    def test_reserved_test_corpus_is_refused_by_default(self) -> None:
        for seed in (RESERVED_TEACHER_TEST_SEED_START, 1_550_000_000):
            with self.assertRaises(ValueError):
                assert_seeds_outside_frozen_lineages([seed])
        # Evaluation-only use is allowed when explicitly requested.
        assert_seeds_outside_frozen_lineages(
            [1_550_000_000], allow_reserved_test_corpus=True
        )

    def test_default_v3_range_is_fresh(self) -> None:
        assert_seeds_outside_frozen_lineages([V3_DEFAULT_SEED_START])
        for start, _stop in FROZEN_R2_SEED_RANGES:
            self.assertNotEqual(V3_DEFAULT_SEED_START, start)

    def test_strength_report_gate(self) -> None:
        self.assertFalse(strength_report_approves(None))
        self.assertFalse(strength_report_approves({}))
        self.assertFalse(strength_report_approves(
            {"scope": "simulator_act1", "gate": {"expert_labels_approved": False}}
        ))
        self.assertFalse(strength_report_approves(
            {"scope": "something_else", "gate": {"expert_labels_approved": True}}
        ))
        self.assertTrue(strength_report_approves(
            {"scope": "simulator_act1", "gate": {"expert_labels_approved": True}}
        ))


if __name__ == "__main__":
    unittest.main()
