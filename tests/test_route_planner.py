"""Offline tests for the visible, bounded map route planner."""

from __future__ import annotations

import copy
import unittest

from advisor_core.live_candidate_codec import (
    AmbiguousCandidateError,
    IllegalCandidateError,
    UnsupportedScreenError,
)
from advisor_core.policy_live import LiveHeuristicPolicy
from advisor_core.route_planner import RoutePlannerError, RoutePlannerPolicy, score_node


def map_state(
    options: list[dict],
    *,
    nodes: list[dict] | None = None,
    hp: int = 80,
    max_hp: int = 80,
) -> dict:
    container = {
        "current_position": {"col": 3, "row": 0, "type": "Ancient"},
        "next_options": copy.deepcopy(options),
    }
    if nodes is not None:
        container["nodes"] = copy.deepcopy(nodes)
    return {
        "state_type": "map",
        "map": container,
        "player": {"hp": hp, "max_hp": max_hp},
        "run": {"act": 1, "floor": 1, "ascension": 10},
    }


def two_branch_graph() -> tuple[list[dict], list[dict]]:
    options = [
        {
            "index": 0,
            "col": 0,
            "row": 1,
            "type": "Monster",
            "leads_to": [{"col": 0, "row": 2, "type": "RestSite"}],
        },
        {
            "index": 1,
            "col": 1,
            "row": 1,
            "type": "Monster",
            "leads_to": [{"col": 1, "row": 2, "type": "Elite"}],
        },
    ]
    nodes = [
        {"col": 0, "row": 1, "type": "Monster", "children": [[0, 2]]},
        {"col": 1, "row": 1, "type": "Monster", "children": [[1, 2]]},
        {"col": 0, "row": 2, "type": "RestSite", "children": [[3, 3]]},
        {"col": 1, "row": 2, "type": "Elite", "children": [[3, 3]]},
        {"col": 3, "row": 3, "type": "Boss", "children": []},
    ]
    return options, nodes


class RoutePlannerTests(unittest.TestCase):
    def test_full_graph_lookahead_beats_immediate_tie_and_preserves_wire_index(self) -> None:
        options, nodes = two_branch_graph()
        state = map_state(options, nodes=nodes)
        recommendation = RoutePlannerPolicy(max_depth=1).recommend(state)

        # Both immediate nodes are Monsters.  The second route's visible Elite
        # child is selected by max-over-branches, so this differs from the
        # existing immediate-only heuristic's index tie-break.
        self.assertEqual(recommendation.primary.action, {"type": "map_choose_node", "index": 1})
        self.assertEqual(recommendation.primary.metrics["immediate_score"], 2.0)
        self.assertAlmostEqual(recommendation.primary.metrics["future_score"], 4.2 * 0.85)
        self.assertEqual(recommendation.primary.metrics["lookahead_depth"], 1.0)
        self.assertEqual(
            RoutePlannerPolicy.wire_action_for(state, recommendation.primary.action),
            {"action": "choose_map_node", "index": 1},
        )
        self.assertTrue(any("Monster" in fact for fact in recommendation.primary.facts))
        self.assertTrue(any("2.00" in fact for fact in recommendation.primary.facts))
        self.assertTrue(any("map.nodes" in warning for warning in recommendation.warnings))

        baseline = LiveHeuristicPolicy().recommend(state)
        self.assertEqual(baseline.primary.action["index"], 0)

    def test_hp_band_reuses_live_baseline_and_normalizes_rest_site(self) -> None:
        self.assertEqual(score_node("RestSite", 0.20), 5.0)
        self.assertEqual(score_node("Campfire", 0.20), 5.0)
        self.assertEqual(score_node("RestSite", 0.80), 3.2)
        self.assertEqual(score_node("Elite", 0.20), 0.8)
        self.assertEqual(score_node("Elite", 0.80), 4.2)
        self.assertEqual(score_node("Boss", 0.80), 0.0)

    def test_one_level_fallback_does_not_infer_hidden_graph(self) -> None:
        options, _ = two_branch_graph()
        # No map.nodes: only the explicitly projected leads_to children may be
        # considered.  A grandchild cannot affect this recommendation.
        state = map_state(options)
        recommendation = RoutePlannerPolicy(max_depth=4).recommend(state)
        self.assertEqual(recommendation.primary.action["index"], 1)
        self.assertEqual(recommendation.primary.metrics["lookahead_depth"], 1.0)
        self.assertEqual(recommendation.search_nodes, 2)
        self.assertTrue(any("next_options.leads_to" in warning for warning in recommendation.warnings))
        self.assertNotIn("Boss", " ".join(recommendation.primary.facts))

    def test_missing_successors_are_immediate_only_and_explicit(self) -> None:
        state = map_state(
            [
                {"index": 0, "col": 0, "row": 1, "type": "Monster"},
                {"index": 1, "col": 1, "row": 1, "type": "Monster"},
            ]
        )
        recommendation = RoutePlannerPolicy(max_depth=4).recommend(state)
        self.assertEqual(recommendation.primary.action["index"], 0)
        self.assertEqual(recommendation.primary.metrics["future_score"], 0.0)
        self.assertEqual(recommendation.primary.metrics["lookahead_depth"], 0.0)
        self.assertTrue(any("leads_to" in warning for warning in recommendation.warnings))
        self.assertTrue(any("map.nodes" in warning for warning in recommendation.warnings))

    def test_budget_forces_one_common_depth_for_all_candidates(self) -> None:
        options, nodes = two_branch_graph()
        # A depth-one footprint contains both roots and both children.  With
        # two nodes available, all candidates fall back together to immediate
        # scores instead of comparing a deep first route with a shallow second.
        recommendation = RoutePlannerPolicy(max_nodes=2, max_depth=4).recommend(
            map_state(options, nodes=nodes)
        )
        self.assertEqual(recommendation.primary.action["index"], 0)
        self.assertEqual(recommendation.primary.metrics["lookahead_depth"], 0.0)
        self.assertTrue(
            all(candidate.metrics["lookahead_depth"] == 0.0
                for candidate in (recommendation.primary, *recommendation.alternatives))
        )
        self.assertEqual(recommendation.search_nodes, 0)
        self.assertTrue(any("max_nodes=2" in warning for warning in recommendation.warnings))

    def test_reachable_cycle_is_rejected_instead_of_receiving_path_dependent_scores(self) -> None:
        state = map_state(
            [
                {
                    "index": 0,
                    "col": 0,
                    "row": 1,
                    "type": "Monster",
                    "leads_to": [{"col": 0, "row": 2, "type": "Monster"}],
                }
            ],
            nodes=[
                {"col": 0, "row": 1, "type": "Monster", "children": [[0, 2]]},
                {"col": 0, "row": 2, "type": "Monster", "children": [[0, 1], [9, 9]]},
            ],
        )
        with self.assertRaises(RoutePlannerError):
            RoutePlannerPolicy(max_depth=4).recommend(state)

    def test_missing_child_never_receives_missing_bonus(self) -> None:
        state = map_state(
            [
                {
                    "index": 0,
                    "col": 0,
                    "row": 1,
                    "type": "Monster",
                    "leads_to": [{"col": 9, "row": 9, "type": "Elite"}],
                }
            ],
            nodes=[
                {"col": 0, "row": 1, "type": "Monster", "children": [[9, 9]]},
            ],
        )
        recommendation = RoutePlannerPolicy(max_depth=1).recommend(state)
        self.assertEqual(recommendation.primary.metrics["future_score"], 0.0)
        self.assertTrue(any("(9,9)" in warning for warning in recommendation.warnings))
        self.assertTrue(any("map.nodes" in warning for warning in recommendation.warnings))

    def test_one_level_conflicting_coordinate_and_self_cycle_are_rejected(self) -> None:
        conflicting = map_state(
            [
                {
                    "index": 0,
                    "col": 0,
                    "row": 1,
                    "type": "Monster",
                    "leads_to": [
                        {"col": 0, "row": 2, "type": "Elite"},
                        {"col": 0, "row": 2, "type": "RestSite"},
                    ],
                }
            ]
        )
        with self.assertRaises(AmbiguousCandidateError):
            RoutePlannerPolicy().recommend(conflicting)

        self_cycle = map_state(
            [
                {
                    "index": 0,
                    "col": 0,
                    "row": 1,
                    "type": "Monster",
                    "leads_to": [{"col": 0, "row": 1, "type": "Monster"}],
                }
            ]
        )
        with self.assertRaises(RoutePlannerError):
            RoutePlannerPolicy().recommend(self_cycle)

    def test_identical_duplicate_node_is_deduplicated_but_conflict_is_rejected(self) -> None:
        options, nodes = two_branch_graph()
        duplicate_state = map_state(options, nodes=nodes + [copy.deepcopy(nodes[0])])
        recommendation = RoutePlannerPolicy(max_depth=1).recommend(duplicate_state)
        self.assertTrue(any("nodes" in warning and "1" in warning for warning in recommendation.warnings))

        conflict_nodes = nodes + [
            {"col": 0, "row": 1, "type": "Elite", "children": [[0, 2]]}
        ]
        with self.assertRaises(AmbiguousCandidateError):
            RoutePlannerPolicy(max_depth=1).recommend(map_state(options, nodes=conflict_nodes))

    def test_wire_action_rejects_stale_or_non_map_action(self) -> None:
        options, nodes = two_branch_graph()
        state = map_state(options, nodes=nodes)
        self.assertEqual(
            RoutePlannerPolicy.wire_action_for(
                state, {"type": "map_choose_node", "index": 1}
            ),
            {"action": "choose_map_node", "index": 1},
        )
        with self.assertRaises(IllegalCandidateError):
            RoutePlannerPolicy.wire_action_for(
                state, {"type": "map_choose_node", "index": 99}
            )
        with self.assertRaises(IllegalCandidateError):
            RoutePlannerPolicy.wire_action_for(state, {"type": "shop_buy", "index": 1})

    def test_non_map_state_is_explicitly_rejected(self) -> None:
        with self.assertRaises(UnsupportedScreenError):
            RoutePlannerPolicy().recommend(
                {"state_type": "event", "event": {"options": []}}
            )


if __name__ == "__main__":
    unittest.main()
