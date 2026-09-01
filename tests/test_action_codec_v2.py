from __future__ import annotations

import unittest

from advisor_core.action_codec_v2 import (
    ActionTargetCodecV2,
    EnemyTarget,
    stable_action_id,
    visible_enemy_targets,
)


class ActionTargetCodecV2Tests(unittest.TestCase):
    def test_accepts_numpy_style_mask_without_boolean_coercion(self) -> None:
        class NumpyStyleMask(list[bool]):
            def __bool__(self) -> bool:
                raise ValueError("truth value is ambiguous")

        codec = ActionTargetCodecV2.from_visible_enemies(
            NumpyStyleMask([True, False, True]),
            targeted_actions={0},
            enemies=[{"entity_id": "ENEMY_0", "hp": 3}],
        )
        self.assertEqual(
            [(item.action, item.target) for item in codec.candidates],
            [(0, 0), (2, None)],
        )

    def test_targeted_actions_expand_but_non_target_actions_do_not_alias(self) -> None:
        targets = visible_enemy_targets(
            [
                {"entity_id": "SLIME_0", "name": "史莱姆甲", "hp": 12},
                {"entity_id": "SLIME_1", "name": "史莱姆乙", "hp": 9},
            ]
        )
        codec = ActionTargetCodecV2(
            [True, True, False, True],
            targeted_actions={0},
            targets=targets,
            action_keys={0: "card:BASH#17", 1: "card:DEFEND#8", 3: "end_turn"},
        )

        self.assertEqual(
            [(candidate.action, candidate.target) for candidate in codec.candidates],
            [(0, 0), (0, 1), (1, None), (3, None)],
        )
        self.assertEqual(
            sum(candidate.action == 3 for candidate in codec.candidates),
            1,
            "end turn must not have one target alias per enemy",
        )
        self.assertEqual(len({candidate.action_id for candidate in codec.candidates}), 4)

    def test_encode_decode_round_trip_and_native_no_target_sentinel(self) -> None:
        codec = ActionTargetCodecV2(
            [True, True],
            targeted_actions={0},
            targets=[EnemyTarget(4, "CULTIST_0", "邪教徒")],
            action_keys={0: "card:STRIKE#2", 1: "end_turn"},
        )

        targeted_index = codec.encode(0, 4)
        targeted = codec.decode(targeted_index)
        self.assertEqual(targeted.emulator_pair, (0, 4))
        self.assertEqual(codec.encode_action_id(targeted.action_id), targeted_index)

        no_target_index = codec.encode(1, -1)
        no_target = codec.decode(no_target_index)
        self.assertEqual(no_target.emulator_pair, (1, -1))
        self.assertIsNone(no_target.target_id)
        with self.assertRaises(ValueError):
            codec.encode(1, 4)

    def test_action_ids_are_stable_across_mask_and_enemy_order_changes(self) -> None:
        keys = {2: "card:STRIKE#instance-7"}
        first = ActionTargetCodecV2(
            [False, False, True],
            {2},
            [EnemyTarget(0, "TOADPOLE_0"), EnemyTarget(1, "TOADPOLE_1")],
            action_keys=keys,
        )
        second = ActionTargetCodecV2(
            [True, False, True, True],
            {2},
            [EnemyTarget(8, "TOADPOLE_1"), EnemyTarget(7, "TOADPOLE_0")],
            action_keys=keys,
        )

        first_ids = {item.target_id: item.action_id for item in first.candidates}
        second_ids = {
            item.target_id: item.action_id
            for item in second.candidates
            if item.action == 2
        }
        self.assertEqual(first_ids, second_ids)
        self.assertEqual(
            first_ids["TOADPOLE_0"],
            stable_action_id("card:STRIKE#instance-7", "TOADPOLE_0"),
        )

    def test_per_action_target_restrictions_are_respected(self) -> None:
        codec = ActionTargetCodecV2(
            [True],
            {0},
            [EnemyTarget(0, "A"), EnemyTarget(1, "B")],
            valid_target_ids={0: {"B"}},
        )
        self.assertEqual([(item.action, item.target_id) for item in codec.candidates], [(0, "B")])

    def test_dead_hidden_and_untargetable_enemies_are_not_candidates(self) -> None:
        targets = visible_enemy_targets(
            [
                {"entity_id": "ALIVE", "hp": 1},
                {"entity_id": "DEAD", "hp": 0},
                {"entity_id": "HIDDEN", "hp": 4, "is_visible": False},
                {"entity_id": "OFF_LIMITS", "hp": 4, "targetable": False},
            ]
        )
        self.assertEqual([(target.target_index, target.target_id) for target in targets], [(0, "ALIVE")])

    def test_targeted_action_without_visible_target_is_not_legal_candidate(self) -> None:
        codec = ActionTargetCodecV2([True, True], {0}, [], action_keys={1: "end_turn"})
        self.assertEqual([(item.action, item.target) for item in codec.candidates], [(1, None)])

    def test_duplicate_target_identity_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "target_id values must be unique"):
            ActionTargetCodecV2(
                [True],
                {0},
                [EnemyTarget(0, "SAME"), EnemyTarget(1, "SAME")],
            )

    def test_duplicate_action_keys_are_rejected_when_they_alias(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate action_id"):
            ActionTargetCodecV2(
                [True, True],
                set(),
                [],
                action_keys={0: "same", 1: "same"},
            )


if __name__ == "__main__":
    unittest.main()
