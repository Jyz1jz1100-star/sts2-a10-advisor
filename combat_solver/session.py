"""Battle session tracking: bind solver snapshots to live states and build
per-battle comparison records.

The tracker consumes only read-only inputs: polled STS2MCP states and
SourceEvents from a SolverSource. It never sends anything to the game.

Binding rules, in priority order:
1. a snapshot whose state_hash equals the current turn-anchor's hash binds to
   that anchor;
2. a snapshot whose battle_turn equals the anchor turn binds to the anchor
   (the live mod cannot compute our decision ids; turn labels are the
   fallback identity, latest wins);
3. a snapshot labelled for an earlier turn (cross-turn routes) binds to the
   battle and may still provide route steps for later turns;
4. anything else stays an orphan and is counted, never force-bound.

Route steps for a closed turn prefer the anchor's own snapshot and fall back
to the newest battle-bound snapshot that covers the turn.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from bridge.trace_controller import decision_id

from combat_solver.compare import BattleRecord, TurnOutcome, build_battle_record
from combat_solver.executed import ExecutedTurn, infer_executed_turn
from combat_solver.reader import DeployRecord, SourceEvent, SolverSource
from combat_solver.snapshot import (
    RouteAction,
    RouteStep,
    SolverFailure,
    SolverSnapshot,
)

_WIN_STATE_TYPES = {"rewards", "treasure", "map", "event", "rest_site", "shop"}
_LOSS_STATE_TYPES = {"game_over"}
#: mid-combat selection screens: the state_type flips away from "monster"
#: while the fight continues (observed live: a hand_select at round 7 split
#: one Exoskeleton fight into two battle records). They must never close an
#: open battle.
_COMBAT_INTERNAL_STATE_TYPES = {"hand_select", "card_select"}
#: relics that heal at combat end. The last monster-state poll can land after
#: the heal is applied (observed live: every won battle's final-turn HP was
#: exactly +6 above the solver's prediction with Burning Blood equipped), so
#: a won battle's HP is adjusted back to the pre-heal value to match what the
#: solver's ``final_hp`` predicts (damage taken, not net of end-of-combat
#: healing).
COMBAT_END_HEALS = {"BURNING_BLOOD": 6}


def is_player_turn_state(state: dict[str, Any]) -> bool:
    battle = state.get("battle") or {}
    return (
        state.get("state_type") == "monster"
        and battle.get("turn") == "player"
        and battle.get("is_play_phase") is True
    )


def is_combat_state(state: dict[str, Any]) -> bool:
    return state.get("state_type") == "monster"


@dataclass
class _TurnAnchor:
    turn: int
    state_hash: str
    state: dict[str, Any]
    hp_start: int
    snapshot: SolverSnapshot | None = None
    failures: list[SolverFailure] = field(default_factory=list)


@dataclass
class _OpenBattle:
    battle_id: str
    run_id: str | None
    act: int | None
    floor: int | None
    seed: int | None
    enemies: tuple[str, ...]
    hp_start: int
    anchor: _TurnAnchor
    opened_at_utc: str
    turns: list[TurnOutcome] = field(default_factory=list)
    snapshots: list[SolverSnapshot] = field(default_factory=list)
    failures: list[SolverFailure] = field(default_factory=list)
    # the mod's own execution records per turn (full-auto/execute modes)
    deploys: dict[int, DeployRecord] = field(default_factory=dict)
    attached_mid: bool = False
    last_monster_hp: int = 0
    last_monster_state: dict[str, Any] = field(default_factory=dict)
    battle_snapshot: SolverSnapshot | None = None


class BattleTracker:
    def __init__(
        self,
        source: SolverSource,
        battle_id_prefix: str = "battle",
        battle_id_start: int = 0,
        now_fn: Callable[[], str] | None = None,
        skip_midbattle: bool = True,
    ):
        self._source = source
        self._prefix = battle_id_prefix
        self._now_fn = now_fn or _utc_now
        self._skip_midbattle = skip_midbattle
        if isinstance(battle_id_start, bool) or not isinstance(battle_id_start, int):
            raise TypeError("battle_id_start must be an integer")
        if battle_id_start < 0:
            raise ValueError("battle_id_start cannot be negative")
        # A resumed runner passes the highest sequence already committed to
        # battles.jsonl.  IDs therefore continue monotonically instead of
        # replaying csb-0001 and silently double-counting the sample.
        self._battle_seq = battle_id_start
        self._open: _OpenBattle | None = None
        self.records: list[BattleRecord] = []
        self.orphan_snapshots = 0
        self.orphan_failures = 0
        # battles the harness attached to mid-fight (first observed round > 1):
        # their first turns lack route baselines and deploy records, so they
        # are closed but not recorded
        self.skipped_midbattle = 0
        # Events that arrived before any battle was open (the solver can emit
        # a route the instant combat starts, before the harness's first monster
        # poll). They are retried until a battle opens; once a battle is open,
        # anything that cannot bind is orphaned immediately.
        self._pending: list[SourceEvent] = []

    # ------------------------------------------------------------------ source
    def drain_source(self) -> None:
        events = self._pending + self._source.poll()
        self._pending = []
        unbound: list[SourceEvent] = []
        for event in events:
            if not self._absorb(event):
                unbound.append(event)
        if self._open is None:
            self._pending = unbound
        else:
            for event in unbound:
                if event.snapshot is not None:
                    self.orphan_snapshots += 1
                else:
                    self.orphan_failures += 1

    def _absorb(self, event: SourceEvent) -> bool:
        if event.snapshot is not None:
            return self._try_bind_snapshot(event.snapshot)
        if event.failure is not None:
            return self._try_bind_failure(event.failure)
        if event.deploy is not None:
            return self._try_bind_deploy(event.deploy)
        return False

    def _try_bind_deploy(self, deploy: DeployRecord) -> bool:
        battle = self._open
        if battle is None:
            return False
        if not self._is_fresh_time(deploy.captured_at_utc, battle):
            return False
        battle.deploys[deploy.turn] = deploy
        return True

    @staticmethod
    def _is_fresh_time(captured: str, battle: _OpenBattle) -> bool:
        opened = _parse_iso(battle.opened_at_utc)
        captured_dt = _parse_iso(captured)
        if captured_dt is None or opened is None:
            return True
        return captured_dt >= opened

    def _try_bind_snapshot(self, snapshot: SolverSnapshot) -> bool:
        battle = self._open
        if battle is None:
            return False
        anchor = battle.anchor
        if snapshot.state_hash == anchor.state_hash:
            # First-wins: the route displayed when the turn started is the
            # deviation baseline. Later same-turn records (the mod re-emits a
            # trimmed suffix after every played card) must NOT overwrite it.
            if anchor.snapshot is None:
                anchor.snapshot = snapshot
            battle.snapshots.append(snapshot)
            if battle.battle_snapshot is None:
                battle.battle_snapshot = snapshot
            return True
        if snapshot.battle_turn == anchor.turn and self._is_fresh(snapshot, battle):
            if anchor.snapshot is None:
                anchor.snapshot = snapshot  # first wins; see comment above
            battle.snapshots.append(snapshot)
            if battle.battle_snapshot is None:
                battle.battle_snapshot = snapshot
            return True
        if (
            snapshot.battle_turn is not None
            and snapshot.battle_turn < anchor.turn
            and self._is_fresh(snapshot, battle)
        ):
            # cross-turn route computed at an earlier turn
            battle.snapshots.append(snapshot)
            return True
        return False

    @staticmethod
    def _is_fresh(snapshot: SolverSnapshot, battle: _OpenBattle) -> bool:
        """Refuse turn-label binding for snapshots predating battle open.

        Guards against a previous battle's late snapshot latching onto the
        next battle that happens to share a turn label. Hash binding is exact
        and needs no guard.
        """
        captured = _parse_iso(snapshot.provenance.captured_at_utc)
        opened = _parse_iso(battle.opened_at_utc)
        if captured is None or opened is None:
            captured_raw = snapshot.provenance.captured_at_utc
            if not captured_raw or not battle.opened_at_utc:
                return True
            try:
                return captured_raw >= battle.opened_at_utc
            except TypeError:
                return True
        return captured >= opened

    def _try_bind_failure(self, failure: SolverFailure) -> bool:
        battle = self._open
        if battle is None:
            return False
        if failure.state_hash is None or failure.state_hash == battle.anchor.state_hash:
            battle.anchor.failures.append(failure)
            battle.failures.append(failure)
            return True
        return False

    # ------------------------------------------------------------------- feed
    def feed(self, state: dict[str, Any]) -> BattleRecord | None:
        """Absorb one polled state; return a record when a battle just closed."""
        self.drain_source()
        if is_combat_state(state):
            self._note_combat(state)
            return None
        if state.get("state_type") in _COMBAT_INTERNAL_STATE_TYPES:
            return None  # mid-combat selection screen: the fight continues
        if self._open is not None:
            return self._close_battle(state)
        return None

    def _note_combat(self, state: dict[str, Any]) -> None:
        battle = self._open
        hp = _player_hp(state)
        if battle is not None:
            battle.last_monster_hp = hp
            battle.last_monster_state = state
            # A battle can be opened by a state projection that omits the run
            # envelope and receive the concrete identity on the next poll.
            # Preserve that explicit identity, but never overwrite a bound
            # run with a conflicting value.
            run = state.get("run") or {}
            observed_run_id = (
                run.get("run_id")
                if isinstance(run, dict)
                and isinstance(run.get("run_id"), str)
                and run.get("run_id")
                else None
            )
            if battle.run_id is None and observed_run_id is not None:
                battle.run_id = observed_run_id
            elif (
                battle.run_id is not None
                and observed_run_id is not None
                and battle.run_id != observed_run_id
            ):
                # There is no safe winner for a contradictory run envelope;
                # clear the binding so the durable evidence adapter blocks
                # the record instead of relabelling it.
                battle.run_id = None
        if not is_player_turn_state(state):
            return
        state_hash = decision_id(state)
        round_no = (state.get("battle") or {}).get("round")
        if not isinstance(round_no, int):
            return
        if battle is None:
            run = state.get("run") or {}
            enemies = tuple(
                e.get("entity_id")
                for e in (state.get("battle") or {}).get("enemies") or []
                if isinstance(e, dict) and e.get("entity_id")
            )
            self._battle_seq += 1
            battle_id = f"{self._prefix}-{self._battle_seq:04d}-f{run.get('floor')}"
            self._open = _OpenBattle(
                battle_id=battle_id,
                run_id=(
                    run.get("run_id")
                    if isinstance(run.get("run_id"), str) and run.get("run_id")
                    else None
                ),
                act=_as_int(run.get("act")),
                floor=_as_int(run.get("floor")),
                seed=_as_int(run.get("seed")),
                enemies=enemies,
                hp_start=hp,
                anchor=_TurnAnchor(
                    turn=round_no, state_hash=state_hash, state=state, hp_start=hp
                ),
                opened_at_utc=self._now_fn(),
                last_monster_hp=hp,
                last_monster_state=state,
                attached_mid=self._skip_midbattle and round_no > 1,
            )
            self.drain_source()
            return
        if state_hash == battle.anchor.state_hash:
            return  # duplicate poll of the same turn-start state
        if battle.anchor.turn == round_no:
            return  # same player turn re-polled with cosmetic changes
        if round_no > battle.anchor.turn:
            self._close_turn(next_state=state, ambiguous_final=False)
            battle.anchor = _TurnAnchor(
                turn=round_no, state_hash=state_hash, state=state, hp_start=hp
            )
            self.drain_source()

    def _close_turn(self, next_state: dict[str, Any], *, ambiguous_final: bool) -> None:
        battle = self._open
        if battle is None:
            return
        anchor = battle.anchor
        hp_end = _player_hp(next_state)
        deploy = battle.deploys.get(anchor.turn)
        if deploy is not None:
            # the mod's own deployment record is the exact ground truth of
            # what was played (full-auto / execute modes); it supersedes
            # state-delta inference
            actions = list(deploy.actions)
            if deploy.end_turn:
                actions.append(RouteAction(kind="end_turn"))
            executed = ExecutedTurn(
                turn=anchor.turn,
                actions=tuple(actions),
                ambiguous=False,
                notes=("executed per the solver deploy log",),
                # A legacy/manual DeployRecord without byte provenance stays
                # useful diagnostically but is not acceptance-grade evidence.
                source=(
                    "deploy_log" if deploy.log_range is not None
                    else "deploy_log_unverified"
                ),
                source_evidence=(
                    deploy.log_range.to_json() if deploy.log_range is not None else None
                ),
            )
        else:
            executed = infer_executed_turn(anchor.state, next_state, anchor.turn)
        if ambiguous_final and executed.source != "deploy_log":
            # a truncated final turn is only uncertain when we must infer the
            # actions from sparse state polls; the mod's own deploy log is
            # authoritative regardless of polling gaps
            executed = ExecutedTurn(
                turn=anchor.turn,
                actions=executed.actions,
                ambiguous=True,
                notes=executed.notes
                + ("final turn: battle closed without an intermediate state",),
                source=executed.source,
            )
        route_step, provider = self._route_step_for(anchor, anchor.turn)
        route_step = self._with_derived_hp_end(battle, anchor, route_step, hp_end)
        battle.turns.append(
            TurnOutcome(
                turn=anchor.turn,
                snapshot=provider,
                failure=anchor.failures[0] if anchor.failures else None,
                executed=executed,
                route_step=route_step,
                actual_hp_end=hp_end,
                decision_id=anchor.state_hash,
            )
        )

    def _with_derived_hp_end(
        self,
        battle: _OpenBattle,
        anchor: _TurnAnchor,
        route_step: RouteStep | None,
        actual_hp_end: int,
    ) -> RouteStep | None:
        """Derive per-turn ``predicted_hp_end`` from the solver's per-turn loss.

        The log adapter records ``predicted_hp_lost`` per turn (TURN_OUTCOME
        lines) but not absolute HP. The tracker knows actual HP at the
        previous turn boundary, so the prediction becomes comparable:
        predicted_hp_end = previous actual hp - predicted loss.
        """
        if route_step is None or route_step.predicted_hp_lost is None:
            return route_step
        if route_step.predicted_hp_end is not None:
            return route_step
        previous_hp = (
            battle.turns[-1].actual_hp_end if battle.turns else battle.hp_start
        )
        return RouteStep(
            turn=route_step.turn,
            actions=route_step.actions,
            predicted_hp_end=previous_hp - route_step.predicted_hp_lost,
            predicted_hp_lost=route_step.predicted_hp_lost,
        )

    def _route_step_for(
        self, anchor: _TurnAnchor, turn: int
    ) -> tuple[RouteStep | None, SolverSnapshot | None]:
        if anchor.snapshot is not None:
            for step in anchor.snapshot.route:
                if step.turn == turn:
                    return step, anchor.snapshot
        battle = self._open
        if battle is not None:
            # Arrival order: the FIRST snapshot carrying a step for this turn
            # is the route as first displayed for it (the initial full route),
            # never a later same-turn suffix.
            for snapshot in battle.snapshots:
                for step in snapshot.route:
                    if step.turn == turn:
                        return step, snapshot
        return None, None

    def _close_battle(self, closing_state: dict[str, Any]) -> BattleRecord | None:
        battle = self._open
        if battle is None:
            return None
        # The battle may end during the player's own turn (enemies die) with
        # no intermediate poll; such a final turn is marked ambiguous instead
        # of pretending the route was (not) followed.
        final_truncated = battle.last_monster_state is battle.anchor.state
        self._close_turn(
            next_state=battle.last_monster_state, ambiguous_final=final_truncated
        )
        if battle.attached_mid:
            self.skipped_midbattle += 1
            self._open = None
            return None
        outcome = _classify_outcome(closing_state)
        hp_end = battle.last_monster_hp
        heal_adjustment = None
        if outcome == "win":
            heal = 0
            heal_relic = None
            relics = (battle.anchor.state.get("player") or {}).get("relics") or []
            for relic in relics:
                relic_id = relic.get("id") if isinstance(relic, dict) else None
                amount = COMBAT_END_HEALS.get(relic_id)
                if amount:
                    heal += amount
                    heal_relic = relic_id
            if heal and hp_end > 0:
                hp_end = max(0, hp_end - heal)
                heal_adjustment = {"relic_id": heal_relic, "amount": heal}
        record = build_battle_record(
            battle_id=battle.battle_id,
            run_id=battle.run_id,
            seed=battle.seed,
            act=battle.act,
            floor=battle.floor,
            enemies=battle.enemies,
            hp_start=battle.hp_start,
            hp_end=hp_end,
            outcome=outcome,
            turns=battle.turns,
            battle_snapshot=battle.battle_snapshot,
            failures=battle.failures,
            heal_adjustment=heal_adjustment,
        )
        self.records.append(record)
        self._open = None
        return record

    # ------------------------------------------------------------------ stats
    def stats(self) -> dict[str, Any]:
        return {
            "battles_closed": len(self.records),
            "battle_open": self._open is not None,
            "bound_snapshots": len(self._open.snapshots) if self._open else 0,
            "deployed_turns": len(self._open.deploys) if self._open else 0,
            "pending_events": len(self._pending),
            "skipped_midbattle": self.skipped_midbattle,
            "orphan_snapshots": self.orphan_snapshots,
            "orphan_failures": self.orphan_failures,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _player_hp(state: dict[str, Any]) -> int:
    hp = (state.get("player") or {}).get("hp")
    return hp if isinstance(hp, int) else 0


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text and text.isascii() and text.isdecimal():
            try:
                return int(text)
            except ValueError:
                return None
    return None


def _classify_outcome(closing_state: dict[str, Any]) -> str:
    state_type = closing_state.get("state_type")
    if state_type in _LOSS_STATE_TYPES:
        return "loss"
    if state_type in _WIN_STATE_TYPES:
        return "win"
    return "unknown"
