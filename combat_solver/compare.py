"""Per-battle comparison and gated aggregation for the Combat Solver track.

Unit of comparison is one battle. For every player turn the harness holds:
- the latest solver snapshot bound to that turn's starting state hash, or a
  typed failure;
- the executed action sequence inferred from state deltas;
- the player HP observed at the end of the turn.

Route deviation is computed on the comparable projection of (kind, card_id,
target_index); HP agreement on |predicted − actual| at both per-turn and
battle granularity. Coverage and failure rates carry Wilson 95% intervals via
training.wilson; coverage gates use lower bounds and failure gates use upper
bounds.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from training.wilson import wilson_interval

from combat_solver.executed import ExecutedTurn
from combat_solver.snapshot import (
    RouteStep,
    SolverFailure,
    SolverSnapshot,
)


@dataclass(frozen=True)
class DeviationInfo:
    deviated: bool
    first_divergence_index: int | None
    executed_len: int
    route_len: int


@dataclass
class TurnOutcome:
    turn: int
    snapshot: SolverSnapshot | None = None
    failure: SolverFailure | None = None
    executed: ExecutedTurn | None = None
    route_step: RouteStep | None = None
    actual_hp_end: int | None = None
    deviation: DeviationInfo | None = None
    # Canonical bridge decision/state identity for this turn.  Older durable
    # records may omit it; evidence adapters must then fail closed rather than
    # guess which run/turn an execution belongs to.
    decision_id: str | None = None

    @property
    def solver_present(self) -> bool:
        return self.snapshot is not None

    @property
    def comparable(self) -> bool:
        return (
            self.snapshot is not None
            and self.executed is not None
            and not self.executed.ambiguous
            and self.deviation is not None
        )


@dataclass
class BattleRecord:
    battle_id: str
    seed: int | None
    act: int | None
    floor: int | None
    enemies: tuple[str, ...]
    hp_start: int
    hp_end: int
    outcome: str  # "win" | "loss" | "unknown"
    turns: list[TurnOutcome] = field(default_factory=list)
    battle_snapshot: SolverSnapshot | None = None
    failures: list[SolverFailure] = field(default_factory=list)
    prediction_complete: bool = False
    #: combat-end heal adjustment applied to hp_end, e.g.
    #: {"relic_id": "BURNING_BLOOD", "amount": 6} — None when unadjusted
    heal_adjustment: dict[str, Any] | None = None
    # The authoritative run identity observed with the battle anchor.  This is
    # optional for backwards-compatible diagnostics, but required by the
    # whole-run deploy-log evidence adapter.
    run_id: str | None = None

    @property
    def actual_hp_loss(self) -> int:
        return self.hp_start - self.hp_end

    @property
    def predicted_hp_loss(self) -> int | None:
        """Battle-granular prediction, only when the horizon reaches battle end.

        Prediction source, in priority order: the snapshot's explicit
        battle-level ``predicted.hp_end`` (the log adapter's ``final_hp``),
        falling back to the final route step's ``predicted_hp_end``.
        """
        if not self.prediction_complete:
            return None
        snapshot = self.battle_snapshot
        if snapshot is None:
            return None
        predicted_end = snapshot.predicted.hp_end
        if predicted_end is None and snapshot.route:
            predicted_end = snapshot.route[-1].predicted_hp_end
        if predicted_end is None:
            return None
        return self.hp_start - predicted_end

    @property
    def predicted_horizon_turns(self) -> int | None:
        snapshot = self.battle_snapshot
        if snapshot is None or not snapshot.route:
            return None
        return max(step.turn for step in snapshot.route)

    @property
    def abs_hp_error(self) -> int | None:
        predicted = self.predicted_hp_loss
        if predicted is None:
            return None
        return abs(predicted - self.actual_hp_loss)

    def turn_stats(self) -> dict[str, int]:
        comparable = [t for t in self.turns if t.comparable]
        deviated = [t for t in comparable if t.deviation is not None and t.deviation.deviated]
        absent = [t for t in self.turns if not t.solver_present]
        ambiguous = [
            t
            for t in self.turns
            if t.executed is not None and t.executed.ambiguous and t.snapshot is not None
        ]
        return {
            "turns_total": len(self.turns),
            "turns_solver_present": len(self.turns) - len(absent),
            "turns_solver_absent": len(absent),
            "turns_comparable": len(comparable),
            "turns_deviated": len(deviated),
            "turns_ambiguous": len(ambiguous),
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "battle_id": self.battle_id,
            "run_id": self.run_id,
            "seed": self.seed,
            "act": self.act,
            "floor": self.floor,
            "enemies": list(self.enemies),
            "hp_start": self.hp_start,
            "hp_end": self.hp_end,
            "actual_hp_loss": self.actual_hp_loss,
            "heal_adjustment": self.heal_adjustment,
            "outcome": self.outcome,
            "predicted_hp_loss": self.predicted_hp_loss,
            "prediction_complete": self.prediction_complete,
            "predicted_horizon_turns": self.predicted_horizon_turns,
            "abs_hp_error": self.abs_hp_error,
            "turns": self.turn_stats(),
            "turn_details": [
                {
                    "turn": t.turn,
                    "decision_id": t.decision_id,
                    "solver_present": t.solver_present,
                    "failure_reason": t.failure.reason if t.failure else None,
                    "route_len": len(t.route_step.actions) if t.route_step else None,
                    "executed_len": len(t.executed.actions) if t.executed else None,
                    "executed_ambiguous": t.executed.ambiguous if t.executed else None,
                    "executed_source": t.executed.source if t.executed else None,
                    "deviated": t.deviation.deviated if t.deviation else None,
                    "first_divergence_index": (
                        t.deviation.first_divergence_index if t.deviation else None
                    ),
                    "predicted_hp_end": (
                        t.route_step.predicted_hp_end if t.route_step else None
                    ),
                    "actual_hp_end": t.actual_hp_end,
                    "turn_hp_error": (
                        abs(t.route_step.predicted_hp_end - t.actual_hp_end)
                        if t.route_step
                        and t.route_step.predicted_hp_end is not None
                        and t.actual_hp_end is not None
                        else None
                    ),
                }
                for t in self.turns
            ],
            "failures": [f.to_json() for f in self.failures],
            "battle_snapshot": (
                {
                    "state_hash": self.battle_snapshot.state_hash,
                    "budget": self.battle_snapshot.budget.to_json(),
                    "candidates": len(self.battle_snapshot.candidates),
                }
                if self.battle_snapshot
                else None
            ),
        }


def _strip_trailing_end_turn(seq: list[tuple[str, str | None, int | None]]) -> list[tuple[str, str | None, int | None]]:
    """A battle won before end-turn must not count as a route deviation."""
    end = len(seq)
    while end > 0 and seq[end - 1] == ("end_turn", None, None):
        end -= 1
    return seq[:end]


def compare_turn(route_step: RouteStep | None, executed: ExecutedTurn | None) -> DeviationInfo | None:
    if route_step is None or executed is None:
        return None
    route = _strip_trailing_end_turn([a.comparable() for a in route_step.actions])
    actual = _strip_trailing_end_turn([a.comparable() for a in executed.actions])
    first: int | None = None
    for i in range(max(len(route), len(actual))):
        left = route[i] if i < len(route) else None
        right = actual[i] if i < len(actual) else None
        if left != right:
            first = i
            break
    deviated = first is not None
    return DeviationInfo(
        deviated=deviated,
        first_divergence_index=first,
        executed_len=len(actual),
        route_len=len(route),
    )


def build_battle_record(
    *,
    battle_id: str,
    run_id: str | None = None,
    seed: int | None,
    act: int | None,
    floor: int | None,
    enemies: Sequence[str],
    hp_start: int,
    hp_end: int,
    outcome: str,
    turns: Iterable[TurnOutcome],
    battle_snapshot: SolverSnapshot | None,
    failures: Iterable[SolverFailure] = (),
    heal_adjustment: dict[str, Any] | None = None,
) -> BattleRecord:
    record = BattleRecord(
        battle_id=battle_id,
        run_id=run_id,
        seed=seed,
        act=act,
        floor=floor,
        enemies=tuple(enemies),
        hp_start=hp_start,
        hp_end=hp_end,
        outcome=outcome,
        battle_snapshot=battle_snapshot,
        heal_adjustment=heal_adjustment,
    )
    record.turns = list(turns)
    record.failures = list(failures)
    for turn in record.turns:
        turn.deviation = compare_turn(turn.route_step, turn.executed)
    if record.battle_snapshot and record.battle_snapshot.route and record.turns:
        final_route_turn = max(step.turn for step in record.battle_snapshot.route)
        last_actual_turn = record.turns[-1].turn
        record.prediction_complete = final_route_turn >= last_actual_turn
    return record


def _percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * q
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _rate(numerator: int, denominator: int) -> tuple[float | None, float | None, float | None]:
    if denominator <= 0:
        return None, None, None
    low, high = wilson_interval(numerator, denominator)
    return numerator / denominator, low, high


def _coverage(
    available: int, total: int
) -> dict[str, float | int | list[float | None] | None]:
    """Return one consistently-shaped coverage/rate payload.

    Coverage is deliberately reported with its denominator.  A naked rate is
    easy to misread when a reader silently dropped turns or battles, and a
    zero-denominator result must remain ``None`` so a gate can fail closed.
    """
    rate, low, high = _rate(available, total)
    return {
        "available": available,
        "total": total,
        "unavailable": max(total - available, 0),
        "rate": rate,
        "wilson95": [low, high],
    }


def _safe_interval_bound(
    summary: dict[str, Any], key: str, index: int
) -> float | None:
    """Extract a Wilson interval bound without allowing malformed data to pass.

    Aggregated summaries are persisted JSON and may be inspected or composed
    by callers other than :func:`aggregate_battles`.  Gate evaluation should
    therefore treat a missing, truncated, non-numeric, or non-finite interval
    as unavailable rather than raising or accidentally accepting it.
    """
    interval = summary.get(key)
    # A Wilson interval is always a two-sided pair.  Requiring both bounds
    # prevents a truncated JSON value such as ``[0.99]`` from masquerading as
    # a valid lower (or upper) bound.
    if not isinstance(interval, (list, tuple)) or len(interval) < 2 or len(interval) <= index:
        return None
    value = interval[index]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _safe_stat(summary: dict[str, Any], section: str, stat: str) -> float | None:
    value = summary.get(section)
    if not isinstance(value, dict):
        return None
    value = value.get(stat)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _safe_value(summary: dict[str, Any], key: str) -> float | None:
    """Read a scalar summary value for a gate, failing closed on bad JSON."""
    value = summary.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


#: Failure reasons that mean the solver itself failed on a state it was
#: asked about. Any battle containing one of these counts as failed even
#: when other turns produced usable routes. (NO_ROUTE on isolated turns and
#: STALE_STATE are routine operational events, tracked per turn/reason, and
#: only make a battle "failed" when the whole battle got no route.)
SOLVER_LEVEL_FAILURE_REASONS = frozenset({"SEARCH_ERROR", "TIMEOUT", "CRASH"})


def _turn_coverage_counts(record: BattleRecord) -> tuple[int, int, int]:
    """Return ``(total, route_available, no_route)`` turn-level counts.

    A source failure is normally attached both to ``record.failures`` and to
    its turn outcome by :class:`BattleTracker`.  Deduplicate those two views.
    A labelled failure for a turn that has no outcome yet is still a solver
    request and is included as a route-unavailable turn; otherwise a partial
    ``NO_ROUTE`` would disappear from both coverage and failure metrics.  An
    unlabelled failure contributes one conservative synthetic turn.
    """
    turns = record.turns
    reasons_by_turn: dict[int, set[str]] = {}
    represented_ids: set[int] = set()
    for turn in turns:
        if turn.failure is not None:
            represented_ids.add(id(turn.failure))
            reasons_by_turn.setdefault(turn.turn, set()).add(turn.failure.reason)

    failure_only_turns: dict[int, set[str]] = {}
    unlabelled_reasons: list[str] = []
    for failure in record.failures:
        if id(failure) in represented_ids:
            continue
        if failure.battle_turn is not None:
            reasons_by_turn.setdefault(failure.battle_turn, set()).add(failure.reason)
            if not any(turn.turn == failure.battle_turn for turn in turns):
                failure_only_turns.setdefault(failure.battle_turn, set()).add(
                    failure.reason
                )
        else:
            unlabelled_reasons.append(failure.reason)

    route_available = sum(1 for turn in turns if turn.route_step is not None)
    total = len(turns) + len(failure_only_turns) + len(unlabelled_reasons)
    no_route = sum(
        1 for turn in turns if "NO_ROUTE" in reasons_by_turn.get(turn.turn, set())
    )
    no_route += sum(
        1 for reasons in failure_only_turns.values() if "NO_ROUTE" in reasons
    )
    no_route += sum(1 for reason in unlabelled_reasons if reason == "NO_ROUTE")
    return total, route_available, min(no_route, total)


def _execution_counts(record: BattleRecord) -> dict[str, int | bool]:
    """Summarize execution provenance for one battle.

    ``inferred`` is not equivalent to ``deploy_log``: state-delta inference is
    intentionally conservative and can be ambiguous.  Automated acceptance
    therefore uses the clean, all-turn deploy-log battle population, while
    manual diagnostics can still inspect the inferred records.
    """
    turns = record.turns
    total, _, _ = _turn_coverage_counts(record)
    deploy_log_turns = sum(
        1
        for turn in turns
        if (
            turn.executed is not None
            and turn.executed.source == "deploy_log"
            and isinstance(turn.executed.source_evidence, dict)
        )
    )
    unverified_deploy_turns = sum(
        1
        for turn in turns
        if (
            turn.executed is not None
            and turn.executed.source.startswith("deploy_log")
            and not (
                turn.executed.source == "deploy_log"
                and isinstance(turn.executed.source_evidence, dict)
            )
        )
    )
    inferred_turns = sum(
        1
        for turn in turns
        if turn.executed is not None and turn.executed.source == "inferred"
    )
    ambiguous_turns = sum(
        1
        for turn in turns
        if turn.executed is not None and turn.executed.ambiguous
    )
    missing_turns = max(total - len([turn for turn in turns if turn.executed is not None]), 0)
    has_deploy_log = deploy_log_turns > 0
    has_inferred = inferred_turns > 0
    has_ambiguous = ambiguous_turns > 0
    # A battle is clean only when every expected turn has an authoritative,
    # unambiguous deployment record.  This is stricter than turn coverage and
    # prevents one inferred turn from hiding inside an otherwise good battle.
    clean_deploy_log_battle = (
        total > 0
        and missing_turns == 0
        and deploy_log_turns == total
        and ambiguous_turns == 0
    )
    return {
        "total_turns": total,
        "deploy_log_turns": deploy_log_turns,
        "unverified_deploy_turns": unverified_deploy_turns,
        "inferred_turns": inferred_turns,
        "ambiguous_turns": ambiguous_turns,
        "missing_turns": missing_turns,
        "has_deploy_log": has_deploy_log,
        "has_inferred": has_inferred,
        "has_ambiguous": has_ambiguous,
        "clean_deploy_log_battle": clean_deploy_log_battle,
    }


def aggregate_battles(records: Sequence[BattleRecord]) -> dict[str, Any]:
    n = len(records)
    hp_predictions = [r for r in records if r.predicted_hp_loss is not None]
    # ``predicted_hp_loss`` is the subset usable for the numeric HP-error
    # metric.  Prediction coverage itself also includes a complete battle
    # prediction that only contains the contract's ``win`` field.
    battle_predictions = [
        r
        for r in records
        if r.prediction_complete
        and r.battle_snapshot is not None
        and r.battle_snapshot.predicted.populated()
    ]
    hp_errors = [r.abs_hp_error for r in hp_predictions if r.abs_hp_error is not None]
    turn_hp_errors = [
        abs(t.route_step.predicted_hp_end - t.actual_hp_end)
        for r in records
        for t in r.turns
        if t.route_step and t.route_step.predicted_hp_end is not None and t.actual_hp_end is not None
    ]
    comparable_turns = sum(r.turn_stats()["turns_comparable"] for r in records)
    deviated_turns = sum(r.turn_stats()["turns_deviated"] for r in records)
    ambiguous_turns = sum(r.turn_stats()["turns_ambiguous"] for r in records)
    absent_turns = sum(r.turn_stats()["turns_solver_absent"] for r in records)
    turn_coverage = [_turn_coverage_counts(record) for record in records]
    n_turns = sum(total for total, _, _ in turn_coverage)
    route_available_turns = sum(available for _, available, _ in turn_coverage)
    route_unavailable_turns = n_turns - route_available_turns
    no_route_turns = sum(no_route for _, _, no_route in turn_coverage)
    execution = [_execution_counts(record) for record in records]
    deploy_log_turns = sum(int(item["deploy_log_turns"]) for item in execution)
    unverified_deploy_turns = sum(
        int(item["unverified_deploy_turns"]) for item in execution
    )
    inferred_execution_turns = sum(int(item["inferred_turns"]) for item in execution)
    ambiguous_execution_turns = sum(int(item["ambiguous_turns"]) for item in execution)
    missing_execution_turns = sum(int(item["missing_turns"]) for item in execution)
    deploy_log_battles = sum(
        1 for item in execution if bool(item["clean_deploy_log_battle"])
    )
    battles_with_deploy_log = sum(1 for item in execution if bool(item["has_deploy_log"]))
    inferred_execution_battles = sum(1 for item in execution if bool(item["has_inferred"]))
    ambiguous_execution_battles = sum(
        1 for item in execution if bool(item["has_ambiguous"])
    )
    execution_missing_battles = sum(1 for item in execution if int(item["missing_turns"]) > 0)

    failed_battles = [
        r
        for r in records
        if r.turn_stats()["turns_solver_present"] == 0
        or any(f.reason in SOLVER_LEVEL_FAILURE_REASONS for f in r.failures)
    ]
    # Deviation denominator: only battles with at least one comparable turn.
    # Battles without comparable turns carry no deviation information and
    # must not dilute the rate by counting as "not deviated".
    comparable_battles = [r for r in records if any(t.comparable for t in r.turns)]
    deviated_battles = [
        r
        for r in comparable_battles
        if any(t.comparable and t.deviation.deviated for t in r.turns)
    ]

    # Deduplicate: the battle-start snapshot is often also a turn's snapshot.
    all_snapshots = list(
        dict.fromkeys(
            s
            for r in records
            for s in [t.snapshot for t in r.turns if t.snapshot is not None]
            + ([r.battle_snapshot] if r.battle_snapshot is not None else [])
        )
    )
    fresh_snapshots = [s for s in all_snapshots if s.budget.elapsed_ms is not None]

    solve_ms = [s.budget.elapsed_ms for s in fresh_snapshots]
    short_ms = [s.budget.short_elapsed_ms for s in fresh_snapshots if s.budget.short_elapsed_ms is not None]
    deep_ms = [s.budget.deep_elapsed_ms for s in fresh_snapshots if s.budget.deep_elapsed_ms is not None]
    peak_mem = [s.budget.peak_memory_mb for s in fresh_snapshots if s.budget.peak_memory_mb is not None]
    proc_ws = [s.budget.process_working_set_mb for s in all_snapshots if s.budget.process_working_set_mb is not None]
    candidates = [
        len(r.battle_snapshot.candidates)
        for r in records
        if r.battle_snapshot is not None
    ]

    failure_reasons: dict[str, int] = {}
    for record in records:
        for failure in record.failures:
            failure_reasons[failure.reason] = failure_reasons.get(failure.reason, 0) + 1

    rate, rate_low, rate_high = _rate(len(deviated_battles), len(comparable_battles))
    fail_rate, fail_low, fail_high = _rate(len(failed_battles), n)
    prediction_coverage = _coverage(len(battle_predictions), n)
    hp_prediction_coverage = _coverage(len(hp_predictions), n)
    comparable_battle_coverage = _coverage(len(comparable_battles), n)
    route_availability = _coverage(route_available_turns, n_turns)
    no_route_rate, no_route_low, no_route_high = _rate(no_route_turns, n_turns)
    deploy_log_turn_coverage = _coverage(deploy_log_turns, n_turns)
    deploy_log_battle_coverage = _coverage(deploy_log_battles, n)
    inferred_turn_rate, inferred_turn_low, inferred_turn_high = _rate(
        inferred_execution_turns, n_turns
    )
    ambiguous_execution_rate, ambiguous_execution_low, ambiguous_execution_high = _rate(
        ambiguous_execution_turns, n_turns
    )

    def _stats(values: Sequence[float]) -> dict[str, float | None]:
        if not values:
            return {"mean": None, "p50": None, "p90": None, "p95": None, "max": None}
        mean = sum(values) / len(values)
        return {
            "mean": mean,
            "p50": _percentile(values, 0.50),
            "p90": _percentile(values, 0.90),
            "p95": _percentile(values, 0.95),
            "max": max(values),
        }

    return {
        "n_battles": n,
        "n_battles_solver_absent": sum(
            1 for r in failed_battles if r.turn_stats()["turns_solver_present"] == 0
        ),
        "n_battles_solver_error": sum(
            1
            for r in failed_battles
            if any(f.reason in SOLVER_LEVEL_FAILURE_REASONS for f in r.failures)
        ),
        "failure_rate": fail_rate,
        "failure_rate_wilson95": [fail_low, fail_high],
        "failure_reasons": failure_reasons,
        "battle_abs_hp_error": _stats([float(v) for v in hp_errors]),
        "turn_abs_hp_error": _stats([float(v) for v in turn_hp_errors]),
        # Keep the historical numeric-HP count key stable.  The broader
        # complete battle-prediction population (which can contain only the
        # contract's ``win`` field) has its own explicit count.
        "n_battles_with_prediction": len(hp_errors),
        "n_battles_with_battle_prediction": len(battle_predictions),
        "n_battles_with_hp_prediction": len(hp_predictions),
        "prediction_coverage": prediction_coverage,
        "prediction_coverage_rate": prediction_coverage["rate"],
        "prediction_coverage_rate_wilson95": prediction_coverage["wilson95"],
        "prediction_coverage_wilson95": prediction_coverage["wilson95"],
        "hp_prediction_coverage": hp_prediction_coverage,
        "hp_prediction_coverage_rate": hp_prediction_coverage["rate"],
        "hp_prediction_coverage_rate_wilson95": hp_prediction_coverage["wilson95"],
        "route_deviation_rate": rate,
        "route_deviation_rate_wilson95": [rate_low, rate_high],
        "route_deviation_battles": len(deviated_battles),
        "n_battles_comparable": len(comparable_battles),
        "n_battles_not_comparable": n - len(comparable_battles),
        "comparable_battle_coverage": comparable_battle_coverage,
        "comparable_battle_coverage_rate": comparable_battle_coverage["rate"],
        "comparable_battle_coverage_rate_wilson95": comparable_battle_coverage["wilson95"],
        "comparable_battle_coverage_wilson95": comparable_battle_coverage["wilson95"],
        "n_turns": n_turns,
        "n_turns_route_available": route_available_turns,
        "n_turns_route_unavailable": route_unavailable_turns,
        "route_available_turns": route_available_turns,
        "route_unavailable_turns": route_unavailable_turns,
        "route_availability": route_availability,
        "route_availability_rate": route_availability["rate"],
        "route_availability_rate_wilson95": route_availability["wilson95"],
        "route_availability_wilson95": route_availability["wilson95"],
        "n_turns_no_route": no_route_turns,
        "no_route_turns": no_route_turns,
        "no_route_turn_rate": no_route_rate,
        "no_route_turn_rate_wilson95": [no_route_low, no_route_high],
        "no_route_rate": no_route_rate,
        "no_route_rate_wilson95": [no_route_low, no_route_high],
        "n_turns_deploy_log": deploy_log_turns,
        "n_turns_unverified_deploy_log": unverified_deploy_turns,
        "n_turns_inferred": inferred_execution_turns,
        "n_turns_ambiguous_execution": ambiguous_execution_turns,
        "n_turns_missing_execution": missing_execution_turns,
        "deploy_log_turn_coverage": deploy_log_turn_coverage,
        "deploy_log_turn_coverage_rate": deploy_log_turn_coverage["rate"],
        "deploy_log_turn_coverage_rate_wilson95": deploy_log_turn_coverage["wilson95"],
        "deploy_log_battle_coverage": deploy_log_battle_coverage,
        "deploy_log_battle_coverage_rate": deploy_log_battle_coverage["rate"],
        "deploy_log_battle_coverage_rate_wilson95": deploy_log_battle_coverage["wilson95"],
        # The generic automated coverage alias intentionally means *clean
        # whole-battle* coverage.  A battle containing one inferred/ambiguous
        # turn cannot be accepted as an automated sample.
        "deploy_log_coverage_rate": deploy_log_battle_coverage["rate"],
        "deploy_log_coverage_rate_wilson95": deploy_log_battle_coverage["wilson95"],
        "n_battles_deploy_log": deploy_log_battles,
        "n_battles_with_deploy_log": battles_with_deploy_log,
        "n_battles_inferred": inferred_execution_battles,
        "n_battles_ambiguous_execution": ambiguous_execution_battles,
        "n_battles_missing_execution": execution_missing_battles,
        "inferred_turn_rate": inferred_turn_rate,
        "inferred_turn_rate_wilson95": [inferred_turn_low, inferred_turn_high],
        "ambiguous_execution_turn_rate": ambiguous_execution_rate,
        "ambiguous_execution_turn_rate_wilson95": [
            ambiguous_execution_low,
            ambiguous_execution_high,
        ],
        "execution_source": {
            "total_turns": n_turns,
            "deploy_log_turns": deploy_log_turns,
            "unverified_deploy_turns": unverified_deploy_turns,
            "inferred_turns": inferred_execution_turns,
            "ambiguous_turns": ambiguous_execution_turns,
            "missing_turns": missing_execution_turns,
            "deploy_log_battles": deploy_log_battles,
            "battles_with_deploy_log": battles_with_deploy_log,
            "inferred_battles": inferred_execution_battles,
            "ambiguous_battles": ambiguous_execution_battles,
            "missing_battles": execution_missing_battles,
        },
        "turn_level": {
            "comparable": comparable_turns,
            "deviated": deviated_turns,
            "ambiguous": ambiguous_turns,
            "solver_absent": absent_turns,
            "route_available": route_available_turns,
            "route_unavailable": route_unavailable_turns,
            "no_route": no_route_turns,
            "deploy_log": deploy_log_turns,
            "unverified_deploy_log": unverified_deploy_turns,
            "inferred": inferred_execution_turns,
            "ambiguous_execution": ambiguous_execution_turns,
            "missing_execution": missing_execution_turns,
            "turn_deviation_rate": _rate(deviated_turns, comparable_turns)[0],
        },
        "solve_ms": _stats([float(v) for v in solve_ms]),
        "solve_short_ms": _stats([float(v) for v in short_ms]),
        "solve_deep_ms": _stats([float(v) for v in deep_ms]),
        "peak_memory_mb": _stats([float(v) for v in peak_mem]),
        "process_working_set_mb": _stats([float(v) for v in proc_ws]),
        "candidates_per_battle": _stats([float(v) for v in candidates]),
        "outcome_counts": {
            "win": sum(1 for r in records if r.outcome == "win"),
            "loss": sum(1 for r in records if r.outcome == "loss"),
            "unknown": sum(1 for r in records if r.outcome == "unknown"),
        },
        "mean_actual_hp_loss": (
            sum(r.actual_hp_loss for r in records) / n if n else None
        ),
    }


@dataclass(frozen=True)
class GateResult:
    name: str
    metric: str
    value: float | None
    limit: float
    passed: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "metric": self.metric,
            "value": self.value,
            "limit": self.limit,
            "passed": self.passed,
        }


#: Aggregated metrics that a gate may bind. Rates use the Wilson 95% *upper*
#: bound so a small-n pass cannot be an artifact of luck.
GATE_METRICS = {
    "failure_rate_upper95": lambda s: _safe_interval_bound(s, "failure_rate_wilson95", 1),
    "route_deviation_rate_upper95": lambda s: _safe_interval_bound(
        s, "route_deviation_rate_wilson95", 1
    ),
    "mean_abs_hp_error": lambda s: _safe_stat(s, "battle_abs_hp_error", "mean"),
    "p90_abs_hp_error": lambda s: _safe_stat(s, "battle_abs_hp_error", "p90"),
    # Legacy performance gate names remain accepted for existing batch files.
    "p95_solve_ms": lambda s: _safe_stat(s, "solve_ms", "p95"),
    "peak_memory_mb": lambda s: _safe_stat(s, "peak_memory_mb", "max"),
    # Fresh-search classes and process-level memory are the preferred names.
    "p95_short_solve_ms": lambda s: _safe_stat(s, "solve_short_ms", "p95"),
    "p95_deep_solve_ms": lambda s: _safe_stat(s, "solve_deep_ms", "p95"),
    "peak_process_working_set_mb": lambda s: _safe_stat(
        s, "process_working_set_mb", "max"
    ),
    # Coverage gates use Wilson lower bounds; direct rate aliases are useful
    # for callers that intentionally choose a point-estimate threshold.
    "prediction_coverage_lower95": lambda s: _safe_interval_bound(
        s, "prediction_coverage_rate_wilson95", 0
    ),
    "prediction_coverage_rate_lower95": lambda s: _safe_interval_bound(
        s, "prediction_coverage_rate_wilson95", 0
    ),
    "prediction_coverage": lambda s: s.get("prediction_coverage_rate"),
    "prediction_coverage_rate": lambda s: s.get("prediction_coverage_rate"),
    "comparable_battle_coverage_lower95": lambda s: _safe_interval_bound(
        s, "comparable_battle_coverage_rate_wilson95", 0
    ),
    "comparable_battle_coverage_rate_lower95": lambda s: _safe_interval_bound(
        s, "comparable_battle_coverage_rate_wilson95", 0
    ),
    "comparable_battle_coverage": lambda s: s.get("comparable_battle_coverage_rate"),
    "comparable_battle_coverage_rate": lambda s: s.get(
        "comparable_battle_coverage_rate"
    ),
    "route_availability_lower95": lambda s: _safe_interval_bound(
        s, "route_availability_rate_wilson95", 0
    ),
    "route_availability_rate_lower95": lambda s: _safe_interval_bound(
        s, "route_availability_rate_wilson95", 0
    ),
    "route_availability": lambda s: s.get("route_availability_rate"),
    "route_availability_rate": lambda s: s.get("route_availability_rate"),
    "route_available_rate": lambda s: s.get("route_availability_rate"),
    "deploy_log_turn_coverage_lower95": lambda s: _safe_interval_bound(
        s, "deploy_log_turn_coverage_rate_wilson95", 0
    ),
    "deploy_log_battle_coverage_lower95": lambda s: _safe_interval_bound(
        s, "deploy_log_battle_coverage_rate_wilson95", 0
    ),
    # Automated acceptance uses clean whole-battle coverage.  The turn-level
    # metric remains available for diagnostics and is reported separately.
    "automated_deploy_log_coverage_lower95": lambda s: _safe_interval_bound(
        s, "deploy_log_battle_coverage_rate_wilson95", 0
    ),
    "automated_deploy_log_turn_coverage_lower95": lambda s: _safe_interval_bound(
        s, "deploy_log_turn_coverage_rate_wilson95", 0
    ),
    "automated_inferred_turns": lambda s: _safe_value(s, "n_turns_inferred"),
    "automated_ambiguous_turns": lambda s: _safe_value(
        s, "n_turns_ambiguous_execution"
    ),
    "automated_missing_execution_turns": lambda s: _safe_value(
        s, "n_turns_missing_execution"
    ),
    "no_route_turn_rate_upper95": lambda s: _safe_interval_bound(
        s, "no_route_turn_rate_wilson95", 1
    ),
    # Short alias retained for integrations that call the metric simply
    # ``no_route_rate``.
    "no_route_rate_upper95": lambda s: _safe_interval_bound(
        s, "no_route_turn_rate_wilson95", 1
    ),
    "no_route_turn_rate": lambda s: s.get("no_route_turn_rate"),
    "no_route_rate": lambda s: s.get("no_route_turn_rate"),
}


MIN_GATE_METRICS = frozenset(
    {
        "prediction_coverage_lower95",
        "prediction_coverage_rate_lower95",
        "prediction_coverage",
        "prediction_coverage_rate",
        "comparable_battle_coverage_lower95",
        "comparable_battle_coverage_rate_lower95",
        "comparable_battle_coverage",
        "comparable_battle_coverage_rate",
        "route_availability_lower95",
        "route_availability_rate_lower95",
        "route_availability",
        "route_availability_rate",
        "route_available_rate",
        "deploy_log_turn_coverage_lower95",
        "deploy_log_battle_coverage_lower95",
        "automated_deploy_log_coverage_lower95",
        "automated_deploy_log_turn_coverage_lower95",
    }
)


def evaluate_gates(summary: dict[str, Any], gates: dict[str, float]) -> list[GateResult]:
    results: list[GateResult] = []
    for name, limit in gates.items():
        extractor = GATE_METRICS.get(name)
        if extractor is None:
            raise ValueError(
                f"unknown gate {name!r}; known gates: {sorted(GATE_METRICS)}"
            )
        try:
            value = extractor(summary)
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            # A malformed or partial summary is not evidence of quality.
            value = None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            value = None
        elif not math.isfinite(float(value)):
            value = None
        if isinstance(limit, bool) or not isinstance(limit, (int, float)):
            passed = False
        elif not math.isfinite(float(limit)) or value is None:
            passed = False
        elif name in MIN_GATE_METRICS:
            passed = float(value) >= float(limit)
        else:
            passed = float(value) <= float(limit)
        results.append(
            GateResult(
                name=name,
                metric=name,
                value=value,
                limit=limit,
                passed=passed,
            )
        )
    return results
