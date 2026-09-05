"""Behavioral tests for the Combat Solver comparison engine and the battle
session tracker (full state sequence -> battle record -> gated summary)."""
from __future__ import annotations

import json
import tempfile
import tomllib
import unittest
from pathlib import Path

from bridge.trace_controller import decision_id

from combat_solver.compare import (
    aggregate_battles,
    build_battle_record,
    compare_turn,
    evaluate_gates,
)
from combat_solver.reader import JsonlSource, SourceEvent
from combat_solver.session import BattleTracker

from tests.test_combat_solver_contract import snapshot_payload
from tests.test_combat_solver_states import (
    enemy,
    monster_state,
    non_combat_state,
    play_card,
)


def _write_snapshot(source_path: Path, payload: dict) -> None:
    with source_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


class CompareTurnTests(unittest.TestCase):
    def _executed(self, actions):
        from combat_solver.executed import ExecutedTurn
        from combat_solver.snapshot import RouteAction

        return ExecutedTurn(
            turn=1, actions=tuple(RouteAction(**a) for a in actions), ambiguous=False
        )

    def _route(self, actions):
        from combat_solver.compare import RouteStep
        from combat_solver.snapshot import RouteAction

        return RouteStep(
            turn=1,
            actions=tuple(RouteAction(**a) for a in actions),
            predicted_hp_end=55,
        )

    def test_identical_route_is_not_deviated(self) -> None:
        actions = [{"kind": "play", "card_id": "STRIKE", "target_index": 0}]
        info = compare_turn(self._route(actions), self._executed(actions))
        self.assertFalse(info.deviated)
        self.assertIsNone(info.first_divergence_index)

    def test_trailing_end_turn_mismatch_is_not_deviated(self) -> None:
        route = [
            {"kind": "play", "card_id": "STRIKE", "target_index": 0},
            {"kind": "end_turn"},
        ]
        executed = [{"kind": "play", "card_id": "STRIKE", "target_index": 0}]
        info = compare_turn(self._route(route), self._executed(executed))
        self.assertFalse(info.deviated)

    def test_wrong_target_deviates_at_index(self) -> None:
        route = [{"kind": "play", "card_id": "STRIKE", "target_index": 0}]
        executed = [{"kind": "play", "card_id": "STRIKE", "target_index": 1}]
        info = compare_turn(self._route(route), self._executed(executed))
        self.assertTrue(info.deviated)
        self.assertEqual(info.first_divergence_index, 0)

    def test_missing_action_deviates(self) -> None:
        route = [
            {"kind": "play", "card_id": "STRIKE", "target_index": 0},
            {"kind": "play", "card_id": "STRIKE", "target_index": 0},
        ]
        executed = [{"kind": "play", "card_id": "STRIKE", "target_index": 0}]
        info = compare_turn(self._route(route), self._executed(executed))
        self.assertTrue(info.deviated)
        self.assertEqual(info.first_divergence_index, 1)


def _battle_record(
    *,
    predicted: int | None = None,
    horizon_complete: bool = True,
    deviated: bool = False,
    hp: tuple[int, int] = (60, 52),
):
    from combat_solver.compare import TurnOutcome
    from combat_solver.executed import ExecutedTurn
    from combat_solver.snapshot import RouteAction, RouteStep

    if deviated:
        route_actions = (RouteAction(kind="play", card_id="STRIKE", target_index=0),)
        executed_actions = (RouteAction(kind="play", card_id="STRIKE", target_index=1),)
    else:
        route_actions = ()
        executed_actions = ()
    turns = [
        TurnOutcome(
            turn=1,
            executed=ExecutedTurn(turn=1, actions=executed_actions, ambiguous=False),
            route_step=RouteStep(turn=1, actions=route_actions, predicted_hp_end=hp[1]),
            actual_hp_end=hp[1],
        )
    ]
    battle_snapshot = None
    if predicted is not None:
        route_end = (hp[0] - predicted) if horizon_complete else (hp[0] - 1)
        battle_snapshot = _snapshot(
            snapshot_payload(
                state_hash="local-sha256:" + "1" * 64,
                route=[{"turn": 1, "actions": [], "predicted_hp_end": route_end}],
                predicted={"hp_loss": predicted},
            )
        )
        turns[0].snapshot = battle_snapshot
        if not horizon_complete:
            # a second actual turn the route horizon never reached
            turns.append(
                TurnOutcome(
                    turn=2,
                    executed=ExecutedTurn(turn=2, actions=(), ambiguous=True),
                    actual_hp_end=hp[1],
                )
            )
    return build_battle_record(
        battle_id="b-1",
        seed=1_600_000_000,
        act=1,
        floor=2,
        enemies=("CULTIST_0",),
        hp_start=hp[0],
        hp_end=hp[1],
        outcome="win",
        turns=turns,
        battle_snapshot=battle_snapshot,
    )


def _snapshot(payload):
    from combat_solver.snapshot import snapshot_from_json

    return snapshot_from_json(payload)


class AggregationTests(unittest.TestCase):
    def test_battle_prediction_requires_complete_horizon(self) -> None:
        record = _battle_record(predicted=8, horizon_complete=True)
        self.assertEqual(record.predicted_hp_loss, 8)
        self.assertEqual(record.abs_hp_error, 0)  # actual loss 60-52 = 8
        record2 = _battle_record(predicted=8, horizon_complete=False)
        self.assertIsNone(record2.predicted_hp_loss)
        self.assertIsNone(record2.abs_hp_error)

    def test_aggregate_metrics_and_rates(self) -> None:
        ok = _battle_record(predicted=8, hp=(60, 52))
        deviated = _battle_record(predicted=8, hp=(60, 52), deviated=True)
        summary = aggregate_battles([ok, deviated])
        self.assertEqual(summary["n_battles"], 2)
        self.assertEqual(summary["route_deviation_battles"], 1)
        rate_low, rate_high = summary["route_deviation_rate_wilson95"]
        self.assertIsNotNone(rate_low)
        self.assertLessEqual(summary["route_deviation_rate"], rate_high)
        self.assertEqual(summary["n_battles_with_prediction"], 2)
        self.assertEqual(summary["battle_abs_hp_error"]["mean"], 0.0)
        self.assertEqual(summary["outcome_counts"]["win"], 2)

    def test_gates_pass_and_fail_on_wilson_upper(self) -> None:
        summary = aggregate_battles(
            [_battle_record(predicted=8) for _ in range(10)]
        )
        passed = evaluate_gates(summary, {"route_deviation_rate_upper95": 0.5})
        self.assertTrue(all(g.passed for g in passed))
        failed = evaluate_gates(summary, {"route_deviation_rate_upper95": 0.01})
        self.assertFalse(all(g.passed for g in failed))

    def test_unknown_gate_rejected(self) -> None:
        with self.assertRaises(ValueError):
            evaluate_gates({}, {"made_up_gate": 1.0})

    def test_coverage_metrics_expose_partial_no_route_and_missing_turns(self) -> None:
        from combat_solver.compare import BattleRecord, TurnOutcome
        from combat_solver.snapshot import SolverFailure

        good = _battle_record(predicted=8)
        partial_no_route = _battle_record(predicted=8)
        partial_no_route.failures.append(
            SolverFailure(reason="NO_ROUTE", captured_at_utc="t", battle_turn=1)
        )
        no_route = BattleRecord(
            battle_id="b-no-route",
            seed=None,
            act=1,
            floor=2,
            enemies=("CULTIST_0",),
            hp_start=60,
            hp_end=60,
            outcome="win",
            turns=[
                TurnOutcome(
                    turn=1,
                    failure=SolverFailure(
                        reason="NO_ROUTE", captured_at_utc="t", battle_turn=1
                    ),
                    actual_hp_end=60,
                )
            ],
        )
        incomplete = _battle_record(predicted=8, horizon_complete=False)

        summary = aggregate_battles([good, partial_no_route, no_route, incomplete])

        # The complete battle prediction count is separate from the legacy
        # numeric HP-error population; an incomplete horizon cannot pass by
        # being silently omitted from the denominator.
        self.assertEqual(summary["n_battles_with_prediction"], 2)
        self.assertEqual(summary["n_battles_with_hp_prediction"], 2)
        self.assertEqual(summary["prediction_coverage"]["available"], 2)
        self.assertEqual(summary["prediction_coverage"]["total"], 4)
        self.assertEqual(summary["prediction_coverage_rate"], 0.5)

        # Good + partial + incomplete's first turn are comparable; the
        # no-route turn and incomplete horizon turn are excluded.
        self.assertEqual(summary["n_battles_comparable"], 3)
        self.assertEqual(summary["comparable_battle_coverage"]["available"], 3)
        self.assertEqual(summary["comparable_battle_coverage"]["total"], 4)

        # Two of five observed turns carry a route, while both explicit
        # NO_ROUTE events remain visible even though one battle has another
        # usable route and therefore does not raise failure_rate.
        self.assertEqual(summary["n_turns"], 5)
        self.assertEqual(summary["route_available_turns"], 3)
        self.assertEqual(summary["route_unavailable_turns"], 2)
        self.assertEqual(summary["n_turns_no_route"], 2)
        self.assertEqual(summary["no_route_turn_rate"], 2 / 5)
        self.assertEqual(summary["failure_rate"], 1 / 4)
        self.assertEqual(summary["n_battles_solver_absent"], 1)

    def test_coverage_and_no_route_gates_use_expected_wilson_direction(self) -> None:
        good = _battle_record(predicted=8)
        partial_no_route = _battle_record(predicted=8)
        from combat_solver.snapshot import SolverFailure

        partial_no_route.failures.append(
            SolverFailure(reason="NO_ROUTE", captured_at_utc="t", battle_turn=1)
        )
        summary = aggregate_battles([good, partial_no_route])

        passed = evaluate_gates(
            summary,
            {
                "prediction_coverage_lower95": 0.1,
                "comparable_battle_coverage_lower95": 0.1,
                "route_availability_lower95": 0.1,
                "no_route_turn_rate_upper95": 1.0,
            },
        )
        self.assertTrue(all(g.passed for g in passed))

        failed = evaluate_gates(
            summary,
            {
                "prediction_coverage_lower95": 0.99,
                "comparable_battle_coverage_rate": 1.01,
                "no_route_turn_rate_upper95": 0.01,
            },
        )
        self.assertEqual([g.passed for g in failed], [False, False, False])

    def test_missing_or_malformed_gate_metrics_fail_closed(self) -> None:
        gate_names = (
            "prediction_coverage_lower95",
            "comparable_battle_coverage_lower95",
            "route_availability_lower95",
            "no_route_turn_rate_upper95",
            "p95_short_solve_ms",
            "p95_deep_solve_ms",
            "peak_process_working_set_mb",
        )
        for name in gate_names:
            with self.subTest(name=name):
                result = evaluate_gates({}, {name: 1.0})
                self.assertFalse(result[0].passed)

        # A truncated interval and non-finite point estimate must also be
        # treated as missing, rather than raising or passing accidentally.
        malformed = {
            "prediction_coverage_rate_wilson95": [0.99],
            "comparable_battle_coverage_rate": float("nan"),
        }
        results = evaluate_gates(
            malformed,
            {
                "prediction_coverage_lower95": 0.5,
                "comparable_battle_coverage_rate": 0.5,
            },
        )
        self.assertEqual([g.passed for g in results], [False, False])

    def test_config_uses_explicit_resource_and_coverage_gates(self) -> None:
        config_path = Path(__file__).parents[1] / "config" / "combat_solver.toml"
        with config_path.open("rb") as handle:
            config = tomllib.load(handle)
        gates = config["gates"]
        for name in (
            "prediction_coverage_lower95",
            "comparable_battle_coverage_lower95",
            "route_availability_lower95",
            "no_route_turn_rate_upper95",
            "p95_short_solve_ms",
            "p95_deep_solve_ms",
            "peak_process_working_set_mb",
        ):
            self.assertIn(name, gates)
            self.assertIsNotNone(gates[name])
        # The old names remain code-compatible, but the preregistered config
        # must use the disambiguated fresh-search/process metrics.
        self.assertNotIn("p95_solve_ms", gates)
        self.assertNotIn("peak_memory_mb", gates)

        automated_gates = config["automated_gates"]
        for name in (
            "automated_deploy_log_coverage_lower95",
            "automated_inferred_turns",
            "automated_ambiguous_turns",
            "automated_missing_execution_turns",
        ):
            self.assertIn(name, automated_gates)
            self.assertIsNotNone(automated_gates[name])
        self.assertNotIn("automated_deploy_log_coverage_lower95", gates)

    def test_execution_provenance_metrics_do_not_treat_inferred_as_automated(
        self,
    ) -> None:
        from combat_solver.executed import ExecutedTurn

        def with_execution(record, *, source: str, ambiguous: bool = False):
            executed = record.turns[0].executed
            record.turns[0].executed = ExecutedTurn(
                turn=executed.turn,
                actions=executed.actions,
                ambiguous=ambiguous,
                notes=executed.notes,
                source=source,
            )
            return record

        records = [
            with_execution(_battle_record(predicted=8), source="deploy_log")
            for _ in range(8)
        ]
        records.append(with_execution(_battle_record(predicted=8), source="inferred"))
        records.append(
            with_execution(
                _battle_record(predicted=8), source="inferred", ambiguous=True
            )
        )

        summary = aggregate_battles(records)
        self.assertEqual(summary["n_turns_deploy_log"], 8)
        self.assertEqual(summary["n_turns_inferred"], 2)
        self.assertEqual(summary["n_turns_ambiguous_execution"], 1)
        self.assertEqual(summary["n_turns_missing_execution"], 0)
        self.assertEqual(summary["n_battles_deploy_log"], 8)
        self.assertEqual(summary["n_battles_with_deploy_log"], 8)
        self.assertEqual(summary["n_battles_inferred"], 2)
        self.assertEqual(summary["n_battles_ambiguous_execution"], 1)
        self.assertEqual(summary["deploy_log_turn_coverage_rate"], 0.8)
        self.assertEqual(summary["deploy_log_battle_coverage_rate"], 0.8)

        # The Wilson lower bound is independently gateable and the explicit
        # zero-count gates make any inferred/ambiguous automated turn fail.
        results = evaluate_gates(
            summary,
            {
                "automated_deploy_log_coverage_lower95": 0.4,
                "automated_inferred_turns": 0.0,
                "automated_ambiguous_turns": 0.0,
                "automated_missing_execution_turns": 0.0,
            },
        )
        self.assertEqual([gate.passed for gate in results], [True, False, False, True])

        # A clean all-deploy batch can satisfy the same lower-bound gate.
        clean = aggregate_battles(
            [
                with_execution(_battle_record(predicted=8), source="deploy_log")
                for _ in range(10)
            ]
        )
        clean_results = evaluate_gates(
            clean,
            {
                "automated_deploy_log_coverage_lower95": 0.7,
                "automated_inferred_turns": 0.0,
                "automated_ambiguous_turns": 0.0,
                "automated_missing_execution_turns": 0.0,
            },
        )
        self.assertTrue(all(gate.passed for gate in clean_results))


class SessionTests(unittest.TestCase):
    def _tracker_with_snapshot(self, tmp: Path, payload: dict) -> BattleTracker:
        source_path = tmp / "snapshots.jsonl"
        _write_snapshot(source_path, payload)
        return BattleTracker(source=JsonlSource(source_path))

    def test_full_battle_flow_with_cross_turn_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            state1 = monster_state(
                round_no=1,
                hp=60,
                energy=3,
                hand=[play_card("STRIKE", 0, cost=1)],
                enemies=[enemy("CULTIST_0", hp=48)],
            )
            state2 = monster_state(
                round_no=2,
                hp=55,
                energy=2,
                hand=[],
                enemies=[enemy("CULTIST_0", hp=42)],
            )
            close = non_combat_state("rewards", hp=55)
            payload = snapshot_payload(
                state_hash=decision_id(state1),
                route=[
                    {
                        "turn": 1,
                        "actions": [
                            {"kind": "play", "card_id": "STRIKE", "target_index": 0},
                            {"kind": "end_turn"},
                        ],
                        "predicted_hp_end": 55,
                    },
                    {
                        "turn": 2,
                        "actions": [{"kind": "end_turn"}],
                        "predicted_hp_end": 50,
                    },
                ],
                predicted={"hp_loss": 10, "hp_end": 50},
            )
            tracker = self._tracker_with_snapshot(tmp, payload)
            self.assertIsNone(tracker.feed(state1))
            self.assertTrue(tracker.stats()["battle_open"])
            self.assertIsNone(tracker.feed(state2))
            record = tracker.feed(close)
            self.assertIsNotNone(record)
            self.assertEqual(record.battle_id, "battle-0001-f2")
            self.assertEqual(record.seed, 1_600_000_000)
            self.assertEqual(record.outcome, "win")
            self.assertEqual(len(record.turns), 2)
            self.assertEqual(record.actual_hp_loss, 5)
            # turn 1 followed the route (play+end_turn with trailing end_turn match)
            self.assertFalse(record.turns[0].deviation.deviated)
            self.assertIsNotNone(record.turns[0].snapshot)
            # final turn was truncated by battle end -> ambiguous, not deviated
            self.assertTrue(record.turns[1].executed.ambiguous)
            self.assertIsNotNone(record.turns[1].route_step)
            self.assertEqual(record.predicted_hp_loss, 10)
            self.assertTrue(record.prediction_complete)
            self.assertEqual(record.abs_hp_error, 5)
            self.assertEqual(tracker.stats()["orphan_snapshots"], 0)

    def test_state_hash_binding_beats_turn_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            state1 = monster_state(round_no=1, hp=60)
            payload = snapshot_payload(
                state_hash=decision_id(state1),
                route=[
                    {
                        "turn": 1,
                        "actions": [{"kind": "end_turn"}],
                        "predicted_hp_end": 60,
                    }
                ],
                predicted={"hp_end": 60},
            )
            tracker = self._tracker_with_snapshot(tmp, payload)
            tracker.feed(state1)
            self.assertIsNotNone(tracker._open.anchor.snapshot)

    def test_orphan_snapshot_counted_not_bound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            payload = snapshot_payload(state_hash="c" * 64, battle_turn=5)
            tracker = self._tracker_with_snapshot(tmp, payload)
            tracker.feed(monster_state(round_no=1, hp=60))
            self.assertIsNone(tracker._open.anchor.snapshot)
            self.assertEqual(tracker.stats()["orphan_snapshots"], 1)

    def test_solver_absent_battle_counts_for_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            tracker = BattleTracker(source=JsonlSource(tmp / "missing.jsonl"))
            tracker.feed(monster_state(round_no=1, hp=60))
            record = tracker.feed(non_combat_state("rewards", hp=50))
            self.assertIsNotNone(record)
            stats = record.turn_stats()
            self.assertEqual(stats["turns_solver_absent"], 1)
            summary = aggregate_battles([record])
            self.assertEqual(summary["failure_rate"], 1.0)
            gates = evaluate_gates(summary, {"failure_rate_upper95": 0.05})
            self.assertFalse(all(g.passed for g in gates))


class RegressionTests2026_09_02(unittest.TestCase):
    """Five pre-smoke regression gates requested by the user.

    1. same-turn route suffix must not overwrite the initial full route;
    2. final_hp (snapshot.predicted.hp_end) must feed the battle HP error;
    3. a battle with routes but a solver-level search error still counts as
       failed;
    4. battles without comparable turns must not dilute the deviation rate;
    5. short/deep latency and process working set are tracked separately.
    """

    # ---------------------------------------------------------- test 1
    def test_same_turn_suffix_must_not_overwrite_initial_full_route(self) -> None:
        state1 = monster_state(round_no=1, hp=60)
        state2 = monster_state(round_no=2, hp=56)
        full_route = snapshot_payload(
            state_hash=decision_id(state1),
            route=[
                {
                    "turn": 1,
                    "actions": [
                        {"kind": "play", "card_id": "STRIKE", "target_index": 0},
                        {"kind": "end_turn"},
                    ],
                    "predicted_hp_lost": 2,
                },
                {"turn": 2, "actions": [{"kind": "end_turn"}], "predicted_hp_lost": 4},
            ],
            predicted={"hp_end": 50},
        )
        # a trimmed suffix re-emitted after the first card was played: the
        # remaining turn-1 actions differ from the original full route
        suffix_route = snapshot_payload(
            battle_turn=1,
            route=[
                {
                    "turn": 1,
                    "actions": [
                        {"kind": "play", "card_id": "BACKFLIP", "target_index": None},
                        {"kind": "end_turn"},
                    ],
                    "predicted_hp_lost": 2,
                },
                {"turn": 2, "actions": [{"kind": "end_turn"}], "predicted_hp_lost": 4},
            ],
            predicted={"hp_end": 50},
        )
        suffix_route["provenance"]["captured_at_utc"] = "2099-01-01T00:00:00Z"
        with tempfile.TemporaryDirectory() as tmp:
            source_path = Path(tmp) / "snapshots.jsonl"
            _write_snapshot(source_path, full_route)
            tracker = BattleTracker(source=JsonlSource(source_path))
            tracker.feed(state1)  # opens battle; the full route binds first
            _write_snapshot(source_path, suffix_route)
            self.assertIsNone(tracker.feed(state2))  # closes turn 1
            record = tracker.feed(non_combat_state("rewards", hp=56))
            self.assertIsNotNone(record)
            turn1 = record.turns[0]
            # baseline must be the INITIAL full route's turn-1 step
            self.assertEqual(
                [a.card_id for a in turn1.route_step.actions], ["STRIKE", None]
            )
            self.assertIsNotNone(turn1.snapshot)
            self.assertEqual(turn1.snapshot.predicted.hp_end, 50)
            # battle-level prediction source stays the initial full route
            self.assertEqual(record.battle_snapshot.predicted.hp_end, 50)

    # ---------------------------------------------------------- test 2
    def test_final_hp_flows_into_battle_hp_error(self) -> None:
        from combat_solver.compare import TurnOutcome
        from combat_solver.executed import ExecutedTurn

        snapshot = _snapshot(
            snapshot_payload(
                state_hash="local-sha256:" + "2" * 64,
                # log-adapter shape: no step-level predicted_hp_end, only
                # predicted_hp_lost per step plus the snapshot-level final_hp
                route=[
                    {"turn": 1, "actions": [], "predicted_hp_lost": 8},
                    {"turn": 2, "actions": [], "predicted_hp_lost": 4},
                ],
                predicted={"hp_end": 48, "hp_loss": 12},
            )
        )
        record = build_battle_record(
            battle_id="b-final-hp",
            seed=1_600_000_000,
            act=1,
            floor=2,
            enemies=("CULTIST_0",),
            hp_start=60,
            hp_end=56,
            outcome="win",
            turns=[
                TurnOutcome(
                    turn=1,
                    snapshot=snapshot,
                    executed=ExecutedTurn(turn=1, actions=(), ambiguous=False),
                    actual_hp_end=56,
                ),
                TurnOutcome(
                    turn=2,
                    snapshot=snapshot,
                    executed=ExecutedTurn(turn=2, actions=(), ambiguous=False),
                    actual_hp_end=56,
                ),
            ],
            battle_snapshot=snapshot,
        )
        # prediction horizon (turn 2) covers the last actual turn -> complete
        self.assertTrue(record.prediction_complete)
        # 60 - final_hp(48) = 12 predicted vs 4 actual -> error 8
        self.assertEqual(record.predicted_hp_loss, 12)
        self.assertEqual(record.abs_hp_error, 8)
        summary = aggregate_battles([record])
        self.assertEqual(summary["n_battles_with_prediction"], 1)
        self.assertEqual(summary["battle_abs_hp_error"]["mean"], 8.0)

    # ---------------------------------------------------------- test 3
    def test_search_error_counts_battle_failed_despite_routes(self) -> None:
        from combat_solver.compare import SOLVER_LEVEL_FAILURE_REASONS
        from combat_solver.snapshot import SolverFailure

        self.assertIn("SEARCH_ERROR", SOLVER_LEVEL_FAILURE_REASONS)
        good = _battle_record(predicted=8)  # routes, no failures -> not failed
        with_error = _battle_record(predicted=8)
        with_error.failures.append(
            SolverFailure(reason="SEARCH_ERROR", captured_at_utc="t", battle_turn=3)
        )
        with_no_route_note = _battle_record(predicted=8)
        with_no_route_note.failures.append(
            SolverFailure(reason="NO_ROUTE", captured_at_utc="t", battle_turn=2)
        )
        summary = aggregate_battles([good, with_error, with_no_route_note])
        # only the SEARCH_ERROR battle is failed; the NO_ROUTE note on a
        # battle with routes does not escalate
        self.assertEqual(summary["n_battles_solver_error"], 1)
        self.assertEqual(summary["n_battles_solver_absent"], 0)
        self.assertEqual(summary["failure_rate"], 1 / 3)
        low, high = summary["failure_rate_wilson95"]
        self.assertIsNotNone(low)
        gates = evaluate_gates(summary, {"failure_rate_upper95": 0.05})
        self.assertFalse(gates[0].passed)

    # ---------------------------------------------------------- test 4
    def test_battles_without_comparable_turns_do_not_improve_deviation_rate(
        self,
    ) -> None:
        comparable_clean = _battle_record(predicted=8)
        no_comparable = _battle_record(predicted=8)
        for t in no_comparable.turns:
            if t.executed is not None:
                t.executed = type(t.executed)(
                    turn=t.executed.turn,
                    actions=t.executed.actions,
                    ambiguous=True,
                    notes=("ambiguous",),
                )
        summary = aggregate_battles([comparable_clean, no_comparable])
        # denominator excludes the not-comparable battle: 0 deviated / 1 comparable
        self.assertEqual(summary["n_battles_comparable"], 1)
        self.assertEqual(summary["n_battles_not_comparable"], 1)
        self.assertEqual(summary["route_deviation_rate"], 0.0)
        deviating = _battle_record(predicted=8, deviated=True)
        summary2 = aggregate_battles([comparable_clean, no_comparable, deviating])
        self.assertEqual(summary2["n_battles_comparable"], 2)
        self.assertEqual(summary2["route_deviation_rate"], 0.5)

    # ---------------------------------------------------------- test 5
    def test_short_deep_latency_and_working_set_tracked_separately(self) -> None:
        from combat_solver.compare import BattleRecord, TurnOutcome
        from combat_solver.logformat import LogTailSource

        result_fresh = (
            "[INFO] [CombatSolver] [CombatSolver/Test] RESULT phase=Short "
            "deep_triggered=False reused=False expanded=1000 searched_turns=2 "
            "projected_battle_hp_lost=5 total_elapsed_ms=1119 "
            "total_worker_allocated_bytes=486933496 short_elapsed_ms=900 "
            "deep_elapsed_ms=219 process_working_set_bytes=1962164224 final_hp=55"
        )
        result_reused = (
            "[INFO] [CombatSolver] [CombatSolver/Test] RESULT phase=Short "
            "deep_triggered=False reused=True reused_from_turn=1 expanded=0 "
            "searched_turns=1 projected_battle_hp_lost=5 total_elapsed_ms=0 "
            "total_worker_allocated_bytes=0 short_elapsed_ms=0 deep_elapsed_ms=0 "
            "process_working_set_bytes=1998114816 final_hp=55"
        )
        request = (
            "[INFO] [CombatSolver] [CombatSolver/Test] SEARCH_REQUEST "
            "generation=1 reason=AutoTurnStart turn=1"
        )
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "logs"
            log_dir.mkdir()
            (log_dir / "godot.log").write_text(
                "\n".join(
                    [request, result_fresh]
                    + [
                        "[CombatSolver/Test] ACTION turn=1 kind=EndTurn",
                        "[CombatSolver/Test] ACTION turn=2 kind=EndTurn",
                    ]
                    + [result_reused]
                    + [
                        "[CombatSolver/Test] ACTION turn=1 kind=EndTurn",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            source = LogTailSource(log_dir, replay=True)
            snapshots = [e.snapshot for e in source.poll() if e.snapshot is not None]
            self.assertEqual(len(snapshots), 2)
            fresh = snapshots[0].budget
            self.assertEqual(fresh.short_elapsed_ms, 900)
            self.assertEqual(fresh.deep_elapsed_ms, 219)
            self.assertEqual(fresh.process_working_set_mb, 1871)
            echo = snapshots[1].budget
            self.assertIsNone(echo.short_elapsed_ms)
            self.assertIsNone(echo.deep_elapsed_ms)
            self.assertEqual(echo.process_working_set_mb, 1905)
            # aggregation keeps the classes separate
            records = [
                BattleRecord(
                    battle_id=f"b{i}",
                    seed=None,
                    act=1,
                    floor=2,
                    enemies=(),
                    hp_start=60,
                    hp_end=55,
                    outcome="win",
                    turns=[TurnOutcome(turn=1, snapshot=s)],
                    battle_snapshot=s,
                )
                for i, s in enumerate(snapshots)
            ]
            summary = aggregate_battles(records)
            self.assertEqual(summary["solve_short_ms"]["mean"], 900.0)
            self.assertEqual(summary["solve_deep_ms"]["mean"], 219.0)
            # reused snapshots contribute to working set but not latency
            self.assertEqual(summary["solve_ms"]["mean"], 1119.0)
            self.assertEqual(summary["process_working_set_mb"]["max"], 1905.0)
            gates = evaluate_gates(
                summary,
                {
                    "p95_short_solve_ms": 1000.0,
                    "p95_deep_solve_ms": 300.0,
                    "peak_process_working_set_mb": 2048.0,
                },
            )
            self.assertTrue(all(g.passed for g in gates))


    # ------------------------------------------------- automated deploy path
    def test_deploy_log_drives_executed_and_deviation(self) -> None:
        from combat_solver.executed import ExecutedTurn
        from combat_solver.reader import DeployRecord
        from combat_solver.session import BattleTracker
        from combat_solver.snapshot import RouteAction

        state1 = monster_state(
            round_no=1,
            hp=60,
            energy=3,
            hand=[play_card("STRIKE", 0, cost=1)],
            enemies=[enemy("CULTIST_0", hp=48)],
        )
        state2 = monster_state(round_no=2, hp=57)
        close = non_combat_state("rewards", hp=57)
        route = snapshot_payload(
            state_hash=decision_id(state1),
            route=[
                {
                    "turn": 1,
                    "actions": [
                        {"kind": "play", "card_id": "STRIKE", "target_index": 0},
                        {"kind": "end_turn"},
                    ],
                    "predicted_hp_lost": 3,
                },
                {"turn": 2, "actions": [{"kind": "end_turn"}], "predicted_hp_lost": 0},
            ],
            predicted={"hp_end": 57},
        )
        with tempfile.TemporaryDirectory() as tmp:
            source_path = Path(tmp) / "snapshots.jsonl"
            _write_snapshot(source_path, route)
            tracker = BattleTracker(source=JsonlSource(source_path))
            tracker.feed(state1)
            # the mod deployed exactly the displayed route
            tracker._absorb(
                SourceEvent.deployed(
                    DeployRecord(
                        turn=1,
                        actions=(RouteAction(kind="play", card_id="STRIKE", target_index=0),),
                        end_turn=True,
                        captured_at_utc="2099-01-01T00:00:00Z",
                    )
                )
            )
            tracker.feed(state2)  # closes turn 1
            record = tracker.feed(close)
            self.assertIsNotNone(record)
            turn1 = record.turns[0]
            self.assertEqual(turn1.executed.source, "deploy_log")
            self.assertFalse(turn1.executed.ambiguous)
            self.assertEqual(
                [a.comparable() for a in turn1.executed.actions],
                [("play", "STRIKE", 0), ("end_turn", None, None)],
            )
            self.assertFalse(turn1.deviation.deviated)
            # per-turn HP prediction derived from predicted_hp_lost
            self.assertEqual(turn1.route_step.predicted_hp_end, 57)
            self.assertEqual(turn1.actual_hp_end, 57)
            # battle-level HP prediction flows from final_hp
            self.assertEqual(record.predicted_hp_loss, 3)
            self.assertEqual(record.abs_hp_error, 0)

    def test_deploy_log_mismatch_counts_as_deviation(self) -> None:
        from combat_solver.reader import DeployRecord
        from combat_solver.session import BattleTracker
        from combat_solver.snapshot import RouteAction

        state1 = monster_state(round_no=1, hp=60)
        state2 = monster_state(round_no=2, hp=56)
        close = non_combat_state("rewards", hp=56)
        route = snapshot_payload(
            state_hash=decision_id(state1),
            route=[
                {
                    "turn": 1,
                    "actions": [{"kind": "play", "card_id": "STRIKE", "target_index": 0}],
                    "predicted_hp_lost": 4,
                }
            ],
            predicted={"hp_end": 56},
        )
        with tempfile.TemporaryDirectory() as tmp:
            source_path = Path(tmp) / "snapshots.jsonl"
            _write_snapshot(source_path, route)
            tracker = BattleTracker(source=JsonlSource(source_path))
            tracker.feed(state1)
            tracker._absorb(
                SourceEvent.deployed(
                    DeployRecord(
                        turn=1,
                        actions=(RouteAction(kind="play", card_id="DEFEND", target_index=None),),
                        end_turn=True,
                        captured_at_utc="2099-01-01T00:00:00Z",
                    )
                )
            )
            record = tracker.feed(close)
            turn1 = record.turns[0]
            self.assertEqual(turn1.executed.source, "deploy_log")
            self.assertTrue(turn1.deviation.deviated)
            self.assertEqual(turn1.deviation.first_divergence_index, 0)


    # ------------------------------------------------- smoke-5 audit findings
    def test_hand_select_does_not_split_a_battle(self) -> None:
        # observed live: a mid-combat hand_select at round 7 split one
        # Exoskeleton fight (rounds 2-8) into two battle records
        state1 = monster_state(round_no=2, hp=48)
        hand_select = {
            "state_type": "hand_select",
            "battle": {"round": 7, "turn": "player", "is_play_phase": False},
            "player": {"hp": 31},
            "run": {"act": 3, "floor": 30},
        }
        state2 = monster_state(round_no=8, hp=31)
        close = non_combat_state("rewards", hp=38)
        tracker = BattleTracker(
            source=JsonlSource(Path("Z:/missing.jsonl")), skip_midbattle=False
        )
        tracker.feed(state1)
        tracker.feed(hand_select)
        self.assertIsNotNone(tracker._open, "hand_select must not close the battle")
        self.assertIsNone(tracker.feed(state2))
        record = tracker.feed(close)
        self.assertIsNotNone(record)
        self.assertEqual(len(record.turns), 2)
        self.assertEqual([t.turn for t in record.turns], [2, 8])

    def test_won_battle_hp_adjusted_for_combat_end_heal(self) -> None:
        from combat_solver.session import COMBAT_END_HEALS

        self.assertEqual(COMBAT_END_HEALS["BURNING_BLOOD"], 6)
        state1 = monster_state(round_no=1, hp=60)
        state1["player"]["relics"] = [
            {"id": "BURNING_BLOOD", "name": "燃烧之血"},
        ]
        close = non_combat_state("rewards", hp=60)  # last monster poll: 60 post-heal
        with tempfile.TemporaryDirectory() as tmp:
            source_path = Path(tmp) / "s.jsonl"
            _write_snapshot(
                source_path,
                snapshot_payload(
                    state_hash=decision_id(state1),
                    route=[{"turn": 1, "actions": [], "predicted_hp_lost": 10}],
                    predicted={"hp_end": 54},  # solver predicts pre-heal HP
                ),
            )
            tracker = BattleTracker(source=JsonlSource(source_path))
            tracker.feed(state1)
            record = tracker.feed(close)
        self.assertIsNotNone(record)
        self.assertEqual(record.outcome, "win")
        self.assertEqual(record.heal_adjustment, {"relic_id": "BURNING_BLOOD", "amount": 6})
        self.assertEqual(record.hp_end, 54)  # 60 - 6 pre-heal
        self.assertEqual(record.actual_hp_loss, 6)
        self.assertEqual(record.abs_hp_error, 0)

    def test_midbattle_attach_is_skipped_by_default(self) -> None:
        # observed live: the smoke batch attached to a fight already at
        # round 2; its first turn had no route baseline (deviation artifact)
        state_r2 = monster_state(round_no=2, hp=48)
        close = non_combat_state("rewards", hp=31)
        tracker = BattleTracker(source=JsonlSource(Path("Z:/missing.jsonl")))
        tracker.feed(state_r2)
        record = tracker.feed(close)
        self.assertIsNone(record)
        self.assertEqual(tracker.stats()["skipped_midbattle"], 1)
        # an opt-out keeps the record
        tracker2 = BattleTracker(
            source=JsonlSource(Path("Z:/missing.jsonl")), skip_midbattle=False
        )
        tracker2.feed(state_r2)
        record2 = tracker2.feed(close)
        self.assertIsNotNone(record2)

    def test_loss_and_unknown_battles_are_not_heal_adjusted(self) -> None:
        state1 = monster_state(round_no=1, hp=60)
        state1["player"]["relics"] = [{"id": "BURNING_BLOOD", "name": "燃烧之血"}]
        tracker = BattleTracker(source=JsonlSource(Path("Z:/missing.jsonl")))
        tracker.feed(state1)
        record = tracker.feed(non_combat_state("game_over", hp=0))
        self.assertEqual(record.outcome, "loss")
        self.assertIsNone(record.heal_adjustment)
        # hp_end comes from the last monster state, not the closing screen
        self.assertEqual(record.hp_end, 60)


if __name__ == "__main__":
    unittest.main()
