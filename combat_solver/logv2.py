"""Grammar v2 reader: the CombatSolver JSON Lines journal.

This module owns the *content* half of the v2 reading path.  ``loggrammar``
provides the container codec (envelope unwrap, sniffing, per-combat discovery)
and the shared line grammar; ``logformat.LogTailSource`` owns the byte cursor and
hands each decoded logical line to the parser here, one :class:`V2CombatParser`
per ``combat-*.jsonl`` file, because a v2 file *is* one combat.

Differences from the v1 block machine that are load-bearing, all of them
established against the 13 real sessions on this machine (CombatSolver
0.35.5 - 0.41.0, see docs/COMBAT_SOLVER_0_41_DRIFT_2026-09-19.md):

* **Turn binding.**  A v2 ``RESULT`` block always carries the full route from
  turn 1 (verified: the first ``ACTION`` sub-line of all 73 blocks is turn=1,
  including every ``reused=True`` block), so v1's "re-bind a reused package to
  its first action's turn" rule would label every turn of a battle as turn 1.
  The turn a block answers is taken from its request marker instead
  (``SEARCH_REQUEST`` / ``SEARCH_REUSED`` / ``TURN_SETUP_SEARCH_START``, all of
  which carry ``turn=``), and a block whose request carries no turn is an error.
* **Re-armed searches.**  The mod emits several ``SEARCH_REQUEST`` lines for the
  same turn while it waits for the game to settle (observed 6 requests for turn 1
  in one combat, one answer).  Consecutive requests for the *same* turn replace
  the pending request; only a different turn, a combat ``RESET`` or the end of
  the stream retires an unanswered request as ``NO_ROUTE``.
* **Answer echoes.**  A turn-setup search publishes its RESULT twice in a row
  (preview, then accepted) with byte-identical payloads.  An emission whose
  ``(turn, route, reused_from_turn)`` equals the previous one is suppressed.
* **Failure taxonomy.**  See :data:`combat_solver.loggrammar.V2_UNREACHABLE_REASONS`.
  An answer with ``only_death_routes=True`` keeps its snapshot (the prediction is
  real and censoring it would flatter the measured error) *and* additionally
  reports ``NO_ROUTE`` for the turn, because a death route is not adoptable.  A
  deploy opened and never closed at end of stream is ``CRASH``.  An undecodable
  envelope is ``PARSE_ERROR``.  A budget marker in an unanswered request window
  upgrades that window's ``NO_ROUTE`` to ``TIMEOUT``.
* **Evidence channel.**  ``[CombatSolver/Evidence]`` records are the mod's own
  *predictions about its own simulation replay* - never evidence of execution.
  The only place they beat scraping ``ACTION`` lines is provenance of the route
  body: a route is taken from a ``ROUTE_ACTION`` trace only when exactly one
  trace announced during the answer's request window replays completely *and*
  its ``(turn, kind, card, target)`` signature equals the published ``ACTION``
  route, which is also the only way to tell candidate traces apart.  A trace in
  the window whose ``ROUTE_REPLAY`` diverged suppresses the snapshot: the mod
  could not reproduce its own answer.  Neither case ever upgrades a deploy.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from combat_solver.loggrammar import (
    CARD_KEYS,
    EVIDENCE_MARKER,
    GRAMMAR_V2,
    KIND_TOKENS,
    POTION_KEYS,
    TURN_KEYS,
    V2_BUDGET_MARKERS,
    V2_COMBAT_LOG_BEGIN_RE,
    V2_COMBAT_LOG_END_RE,
    V2_REQUEST_MARKERS,
    _first_int,
    _first_str,
    _kv_bool,
    _kv_index,
    _kv_int,
    _line_event,
    _PendingAction,
    _RawLine,
    _SearchBlock,
    _TraceActions,
    _utc_now,
    decode_v2_record,
    EnvelopeError,
    group_route,
    is_evidence_line,
    is_solver_line,
    parse_evidence_payload,
    parse_kv_tokens,
    route_signature,
    snapshot_payload,
)
from combat_solver.logranges import LogRangeError, capture_log_range
from combat_solver.reader import DeployRecord, SourceEvent
from combat_solver.snapshot import (
    FAILURE_REASONS,
    RouteAction,
    SolverFailure,
    snapshot_from_json,
)

#: v1-only failure markers; their appearance in a v2 journal is a producer the
#: reader has no calibrated mapping for, so it is reported, never reinterpreted
V1_ONLY_FAILURE_MARKERS = ("SEARCH_FAILURE", "SEARCH_STALE")

#: ``ROUTE_ACTION.action`` fields the reader must see to trust a trace
#: kept explicit so a producer rename is detected instead of silently skipped
_OPTIONAL_ACTION_FIELDS = ("CardId", "PotionId", "TargetIndex", "CardOccurrence")


#: process-state counters the producer stamps into every RESULT line; two
#: publications of the *same* answer (turn-setup preview, then accepted) differ
#: only here, so echo detection compares the RESULT with these removed
V2_INSTANTANEOUS_RESULT_KEYS = frozenset(
    {
        "managed_live_bytes",
        "managed_heap_bytes",
        "managed_fragmented_bytes",
        "process_working_set_bytes",
        "process_private_bytes",
    }
)


def result_identity(kv: dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in kv.items()
        if key not in V2_INSTANTANEOUS_RESULT_KEYS
    }


class V2CombatParser:
    """Block machine for one ``combat-*.jsonl`` file."""

    name = "logtail"

    def __init__(
        self,
        path: Path,
        *,
        log_dir: Path,
        mod_version: str | None,
        tier: str | None = None,
        time_budget_ms: int | None = None,
        memory_limit_mb: int | None = None,
        confirmed_battle_ids: set[str] | None = None,
        report_abandoned_deploys: bool = True,
    ):
        self._path = path
        self._log_dir = log_dir
        self._mod_version = mod_version
        self._tier = tier
        self._time_budget_ms = time_budget_ms
        self._memory_limit_mb = memory_limit_mb
        self._block = _SearchBlock()
        self._emitted: tuple | None = None
        self._deploy_actions: dict[int, list[RouteAction]] = {}
        self._deploy_end_turn: dict[int, bool] = {}
        self._deploy_range_starts: dict[int, tuple[Path, int]] = {}
        self._pending_deploy_turn: int | None = None
        self._last_search: tuple[Path, int, int] | None = None
        self._battle_id: str | None = None
        self._report_abandoned_deploys = report_abandoned_deploys
        #: ids confirmed by a COMBAT_LOG_BEGIN record; shared between the journal
        #: files of one poll because process.jsonl confirms what combat files claim
        self._confirmed_battle_ids = (
            confirmed_battle_ids if confirmed_battle_ids is not None else set()
        )
        self.stats: Counter[str] = Counter()
        self.stats["failures"] = Counter()
        self.stats["combat_log_begin"] = 0
        self.stats["combat_log_end"] = 0
        self.stats["combat_log_end_reasons"] = Counter()

    # ------------------------------------------------------------- identity
    def _note_boundary(self, boundary: tuple[str, str]) -> None:
        kind, value = boundary
        if kind == "begin":
            self.stats["combat_log_begin"] += 1
            self._confirmed_battle_ids.add(value)
            return
        self.stats["combat_log_end"] += 1
        self.stats["combat_log_end_reasons"][value] += 1

    def set_battle_id(self, battle_id: str | None) -> None:
        """Adopt a battle identity only once ``process.jsonl`` confirmed it."""

        if battle_id is not None and self._battle_id is None:
            self._battle_id = battle_id

    @property
    def battle_id(self) -> str | None:
        return self._battle_id

    # ------------------------------------------------------------------ feed
    def feed_record(self, line: _RawLine, events: list[SourceEvent]) -> None:
        """Decode one physical journal record and feed its logical lines.

        The record's physical byte range is passed through unchanged, so a range
        captured from it still addresses the real file that was read.
        """

        self.stats["records"] += 1
        try:
            record = decode_v2_record(line.text)
        except EnvelopeError as exc:
            self.stats["envelope_errors"] += 1
            self.stats["failures"]["PARSE_ERROR"] += 1
            events.append(
                SourceEvent.failed(
                    SolverFailure(
                        reason="PARSE_ERROR",
                        captured_at_utc=_utc_now(),
                        detail=(
                            f"journal record at bytes [{line.byte_start},{line.byte_end})"
                            f" of {line.path.name} is not a decodable envelope: {exc}"
                        ),
                    )
                )
            )
            return
        if record.level == "error":
            # counted, never mapped: an error level is not a solver failure class
            # and 0.41.0 emits none, so inventing one would launder a guess
            self.stats["error_level_records"] += 1
        for text in record.lines:
            self.feed(text, events, line)

    def feed(self, text: str, events: list[SourceEvent], line: _RawLine) -> None:
        self.stats["logical_lines"] += 1
        boundary = is_v2_process_boundary(text)
        if boundary is not None:
            self._note_boundary(boundary)
            return
        if is_evidence_line(text):
            self._feed_evidence(text, events, line)
            return
        if not is_solver_line(text):
            return
        self.stats["solver_lines"] += 1
        marker = _line_event(text)
        if marker is None:
            return
        kv = parse_kv_tokens(text)
        if marker in V2_REQUEST_MARKERS:
            self._feed_request(marker, kv, events, line)
            return
        if marker == "RESET":
            self._feed_reset(events)
            return
        if marker == "ACTION":
            self._feed_action(kv)
            return
        if marker == "DEPLOY_START":
            self._feed_deploy_start(kv, events, line)
            return
        if marker == "DEPLOY_ACTION":
            self._feed_deploy_action(kv)
            return
        if marker == "DEPLOY_END":
            self._feed_deploy_end(kv, events, line)
            return
        if marker == "SEARCH_INTERIM_RESULT":
            return
        if marker == "RESULT":
            if self._block.has_result:
                # A v2 block is self-contained (one record carries RESULT +
                # TURN_OUTCOME* + ACTION*), so a second RESULT inside the same
                # request window is either the producer re-publishing the answer
                # it already gave (observed: turn-setup preview then accepted,
                # identical apart from the live process-memory stamp) or an answer
                # we cannot bind.  Only the provably identical case keeps the
                # window's turn label; anything else is left unbound and fails
                # closed as PARSE_ERROR.
                echoed = result_identity(kv) == result_identity(self._block.result)
                request_turn = self._block.request_turn if echoed else None
                request_marker = self._block.request_marker if echoed else None
                generation = self._block.generation if echoed else None
                self._flush_block(events)
                if echoed:
                    self.stats["result_echo_records"] += 1
                    self._block = _SearchBlock(
                        generation=generation,
                        request_turn=request_turn,
                        request_marker=request_marker,
                    )
            self._block.result = kv
            return
        if marker == "TURN_OUTCOME":
            turn = _kv_int(kv, "turn")
            hp_lost = _kv_int(kv, "hp_lost")
            if turn is not None and hp_lost is not None:
                self._block.turn_losses[turn] = hp_lost
            return
        if marker in V2_BUDGET_MARKERS:
            self.stats["budget_markers"] += 1
            self._block.budget_exhausted = True
            return
        if marker in V1_ONLY_FAILURE_MARKERS:
            self.stats["unmapped_v1_failure_markers"] += 1
            events.append(
                SourceEvent.failed(
                    SolverFailure(
                        reason="PARSE_ERROR",
                        captured_at_utc=_utc_now(),
                        battle_turn=self._block.request_turn,
                        detail=(
                            f"grammar v1 failure marker {marker} in a grammar v2 "
                            "journal: no calibrated mapping"
                        ),
                    )
                )
            )
            return
        return

    def finish(self, events: list[SourceEvent]) -> None:
        """End-of-stream flush, called after every poll.

        An abandoned deploy is only reportable once the stream really ends, i.e.
        in replay: while a game is running, ``DEPLOY_START`` without
        ``DEPLOY_END`` is the normal mid-turn state and reporting it would both
        invent a crash and discard the range the next poll needs.
        """

        self._flush_block(events)
        if self._report_abandoned_deploys:
            self._abandon_open_deploy(events)

    def _feed_request(
        self,
        marker: str,
        kv: dict[str, str],
        events: list[SourceEvent],
        line: _RawLine,
    ) -> None:
        turn = _kv_int(kv, "turn")
        if turn is None:
            self.stats["failures"]["PARSE_ERROR"] += 1
            events.append(
                SourceEvent.failed(
                    SolverFailure(
                        reason="PARSE_ERROR",
                        captured_at_utc=_utc_now(),
                        detail=f"v2 request marker {marker} without a turn label",
                    )
                )
            )
            return
        open_block = self._block
        if (
            open_block.request_turn == turn
            and not open_block.has_result
            and open_block.request_marker is not None
        ):
            # The mod re-armed the search for the turn it is already searching:
            # the earlier request was never a question the advisor could have
            # been waiting on, so it must not be retired as a missing route.
            open_block.request_marker = marker
            open_block.generation = _kv_int(kv, "generation")
        else:
            self._flush_block(events)
            self._block = _SearchBlock(
                generation=_kv_int(kv, "generation"),
                request_turn=turn,
                request_marker=marker,
            )
        self._last_search = (line.path, line.byte_start, turn)

    def _feed_reset(self, events: list[SourceEvent]) -> None:
        self.stats["resets"] += 1
        self._flush_block(events)
        # An open deploy is deliberately kept: 0.41.0 writes the combat's
        # ``RESET`` (detached lifecycle) *before* the last DEPLOY_END, so
        # treating RESET as abandonment would fabricate a crash on every
        # battle that ends normally.
        self._block = _SearchBlock()
        self._emitted = None

    def _feed_action(self, kv: dict[str, str]) -> None:
        kind_raw = kv.get("kind")
        if kind_raw not in KIND_TOKENS:
            self.stats["unknown_action_kinds"] += 1
            return
        kind = KIND_TOKENS[kind_raw]
        if kind == "potion":
            card_id = _first_str(kv, POTION_KEYS)
        elif kind == "play":
            card_id = _first_str(kv, CARD_KEYS)
        else:
            card_id = None
        if kind in ("play", "potion") and card_id is None:
            self.stats["unknown_action_kinds"] += 1
            return
        self._block.actions.append(
            _PendingAction(
                kind=kind,
                card_id=card_id,
                target_index=_kv_index(kv, "target_index"),
                turn=_first_int(kv, TURN_KEYS),
            )
        )

    def _feed_evidence(
        self, text: str, events: list[SourceEvent], line: _RawLine
    ) -> None:  # noqa: ARG002 (line is kept for parity with the solver feed)
        tag = _line_event(text, (EVIDENCE_MARKER,))
        payload = parse_evidence_payload(text)
        if payload is None:
            self.stats["failures"]["PARSE_ERROR"] += 1
            events.append(
                SourceEvent.failed(
                    SolverFailure(
                        reason="PARSE_ERROR",
                        captured_at_utc=_utc_now(),
                        battle_turn=self._block.request_turn,
                        detail=f"unparseable [CombatSolver/Evidence] {tag} payload",
                    )
                )
            )
            return
        trace_id = payload.get("traceId")
        if not isinstance(trace_id, str) or not trace_id:
            self.stats["failures"]["PARSE_ERROR"] += 1
            events.append(
                SourceEvent.failed(
                    SolverFailure(
                        reason="PARSE_ERROR",
                        captured_at_utc=_utc_now(),
                        battle_turn=self._block.request_turn,
                        detail=f"[CombatSolver/Evidence] {tag} without a traceId",
                    )
                )
            )
            return
        trace = self._block.traces.get(trace_id)
        if trace is None:
            trace = _TraceActions(trace_id=trace_id)
            self._block.traces[trace_id] = trace
            self.stats["evidence_traces"] += 1
        if tag == "ROUTE_ACTION":
            self._feed_route_action(trace, payload)
            return
        if tag == "ROUTE_HEALTH":
            # ``change.Target`` indexes the *enemy* taking the change, so this
            # stream cannot supply the player-side per-turn loss TURN_OUTCOME
            # gives; it is counted, never substituted.
            self.stats["route_health"] += 1
            return
        if tag == "ROUTE_REPLAY":
            self.stats["route_replays"] += 1
            count = payload.get("actionCount")
            steps = payload.get("completedSteps")
            trace.replay_seen = True
            trace.action_count = count if isinstance(count, int) else None
            trace.completed_steps = steps if isinstance(steps, int) else None
            trace.first_difference = payload.get("firstScalarDifference")
            if not trace.complete():
                self.stats["replay_divergences"] += 1
            return
        self.stats["unmapped_evidence_tags"] += 1

    def _feed_route_action(self, trace: _TraceActions, payload: dict[str, Any]) -> None:
        self.stats["route_actions"] += 1
        action = payload.get("action")
        index = payload.get("index")
        if not isinstance(action, dict) or not isinstance(index, int):
            self.stats["failures"]["PARSE_ERROR"] += 1
            self.stats["unusable_route_actions"] += 1
            return
        kind_raw = action.get("Kind")
        turn = action.get("Turn")
        if kind_raw not in KIND_TOKENS or not isinstance(turn, int) or turn < 1:
            self.stats["unusable_route_actions"] += 1
            return
        kind = KIND_TOKENS[kind_raw]
        if kind == "potion":
            card_id = action.get("PotionId") or None
        elif kind == "play":
            card_id = action.get("CardId") or None
        else:
            card_id = None
        if kind in ("play", "potion") and not isinstance(card_id, str):
            self.stats["unusable_route_actions"] += 1
            return
        target_index = action.get("TargetIndex")
        trace.actions.append(
            _PendingAction(
                kind=kind,
                card_id=card_id,
                target_index=target_index
                if isinstance(target_index, int) and target_index >= 0
                else None,
                turn=turn,
                note=f"evidence:{trace.trace_id}#{index}",
            )
        )

    # ---------------------------------------------------------------- deploys
    def _feed_deploy_start(
        self,
        kv: dict[str, str],
        events: list[SourceEvent],
        line: _RawLine,
    ) -> None:
        turn = _kv_int(kv, "turn")
        if turn is None:
            self.stats["failures"]["PARSE_ERROR"] += 1
            events.append(
                SourceEvent.failed(
                    SolverFailure(
                        reason="PARSE_ERROR",
                        captured_at_utc=_utc_now(),
                        detail="DEPLOY_START without a turn label",
                    )
                )
            )
            return
        self._abandon_open_deploy(events)
        self._deploy_actions[turn] = []
        self._deploy_end_turn[turn] = False
        start = line.byte_start
        if (
            self._last_search is not None
            and self._last_search[0] == line.path
            and self._last_search[2] == turn
        ):
            start = self._last_search[1]
        self._deploy_range_starts[turn] = (line.path, start)
        self._pending_deploy_turn = turn

    def _feed_deploy_action(self, kv: dict[str, str]) -> None:
        turn = _kv_int(kv, "turn")
        if turn is None:
            return
        potion = _first_str(kv, ("potion", "potion_id"))
        if potion is not None:
            action = RouteAction(
                kind="potion",
                card_id=potion,
                target_index=_kv_index(kv, "target_index"),
            )
        else:
            card = _first_str(kv, ("card", "card_id"))
            if card is None:
                return
            action = RouteAction(
                kind="play",
                card_id=card,
                target_index=_kv_index(kv, "target_index"),
            )
        self._deploy_actions.setdefault(turn, []).append(action)

    def _feed_deploy_end(
        self,
        kv: dict[str, str],
        events: list[SourceEvent],
        line: _RawLine,
    ) -> None:
        turn = _kv_int(kv, "turn")
        if turn is None:
            return
        actions = tuple(self._deploy_actions.pop(turn, []))
        end_turn = bool(self._deploy_end_turn.pop(turn, False)) or (
            _kv_bool(kv, "end_turn") or False
        )
        log_range = None
        range_start = self._deploy_range_starts.pop(turn, None)
        if (
            range_start is not None
            and range_start[0] == line.path
            and line.byte_end > range_start[1]
        ):
            try:
                log_range = capture_log_range(
                    line.path,
                    range_start[1],
                    line.byte_end,
                    allowed_root=self._log_dir,
                    grammar=GRAMMAR_V2,
                )
            except LogRangeError:
                # Keep the deploy as a diagnostic event: the tracker marks it
                # unverified and the assessor will not accept it.
                log_range = None
        self._pending_deploy_turn = None
        self.stats["deploys"] += 1
        if log_range is None:
            self.stats["deploys_without_range"] += 1
        events.append(
            SourceEvent.deployed(
                DeployRecord(
                    turn=turn,
                    actions=actions,
                    end_turn=end_turn,
                    captured_at_utc=_utc_now(),
                    log_range=log_range,
                    battle_log_id=self._battle_id,
                )
            )
        )

    def _abandon_open_deploy(self, events: list[SourceEvent]) -> None:
        turn = self._pending_deploy_turn
        if turn is None:
            return
        self._pending_deploy_turn = None
        self._deploy_actions.pop(turn, None)
        self._deploy_end_turn.pop(turn, None)
        self._deploy_range_starts.pop(turn, None)
        self.stats["failures"]["CRASH"] += 1
        events.append(
            SourceEvent.failed(
                SolverFailure(
                    reason="CRASH",
                    captured_at_utc=_utc_now(),
                    battle_turn=turn,
                    detail=(
                        f"DEPLOY_START turn={turn} was never followed by a "
                        "DEPLOY_END before the combat journal ended"
                    ),
                )
            )
        )

    # ----------------------------------------------------------------- blocks
    def _flush_block(self, events: list[SourceEvent]) -> None:
        block = self._block
        if block.has_result:
            events.extend(self._emit_snapshot())
        elif block.request_turn is not None:
            reason = "TIMEOUT" if block.budget_exhausted else "NO_ROUTE"
            self.stats["failures"][reason] += 1
            events.append(
                SourceEvent.failed(
                    SolverFailure(
                        reason=reason,
                        captured_at_utc=_utc_now(),
                        battle_turn=block.request_turn,
                        detail=(
                            f"{block.request_marker} turn={block.request_turn} got "
                            "no RESULT"
                            + (" (search budget exhausted)" if block.budget_exhausted else "")
                        ),
                    )
                )
            )
        self._block = _SearchBlock()

    def _emit_snapshot(self) -> list[SourceEvent]:
        block = self._block
        result = block.result or {}
        turn = block.request_turn
        if turn is None:
            self.stats["failures"]["PARSE_ERROR"] += 1
            return [
                SourceEvent.failed(
                    SolverFailure(
                        reason="PARSE_ERROR",
                        captured_at_utc=_utc_now(),
                        detail="grammar v2 RESULT block without a turn-labelled request",
                    )
                )
            ]
        signature = block.route_signature()
        echo = (turn, signature, _kv_int(result, "reused_from_turn"))
        if signature is not None and echo == self._emitted:
            self.stats["suppressed_echoes"] += 1
            return []
        # a route the mod could not replay against its own simulation is not an
        # answer at all; suppress it instead of publishing something unverified
        blocked = self._replay_divergence(block)
        if blocked is not None:
            self._emitted = echo
            return blocked
        steps, actions = self._route_for(block)
        if not actions:
            self.stats["empty_routes"] += 1
        reused = _kv_bool(result, "reused") or False
        payload = snapshot_payload(
            block,
            turn=turn,
            steps=steps,
            tier=self._tier,
            time_limit_ms=self._time_budget_ms,
            memory_limit_mb=self._memory_limit_mb,
            reader=self.name,
            mod_version=self._mod_version,
            source_file=self._path.name,
        )
        if payload is None:
            self.stats["failures"]["PARSE_ERROR"] += 1
            return [
                SourceEvent.failed(
                    SolverFailure(
                        reason="PARSE_ERROR",
                        captured_at_utc=_utc_now(),
                        battle_turn=turn,
                        detail="RESULT without any prediction field",
                    )
                )
            ]
        try:
            snapshot = snapshot_from_json(payload)
        except Exception as exc:
            self.stats["failures"]["PARSE_ERROR"] += 1
            return [
                SourceEvent.failed(
                    SolverFailure(
                        reason="PARSE_ERROR",
                        captured_at_utc=_utc_now(),
                        battle_turn=turn,
                        detail=f"block did not satisfy the snapshot contract: {exc}",
                    )
                )
            ]
        self._emitted = echo
        self.stats["snapshots"] += 1
        events = [SourceEvent.of(snapshot)]
        death = self._death_route_answer(block, result)
        if death is not None:
            events.append(death)
        return events

    def _death_route_answer(
        self, block: _SearchBlock, result: dict[str, str]
    ) -> SourceEvent | None:
        """Flag an answer whose own prediction is "the player dies".

        The snapshot is still emitted: dropping it would remove exactly the
        battles where the solver predicted a loss, which would flatter the
        measured prediction error.  The turn is additionally reported as
        ``NO_ROUTE`` because a death route is not something the advisor may
        adopt, and ``FAILURE_REASONS`` has no softer turn-level reason.
        """

        if (_kv_bool(result, "only_death_routes") or False) is not True:
            return None
        self.stats["death_route_answers"] += 1
        self.stats["failures"]["NO_ROUTE"] += 1
        return SourceEvent.failed(
            SolverFailure(
                reason="NO_ROUTE",
                captured_at_utc=_utc_now(),
                battle_turn=block.request_turn,
                detail=(
                    "solver answer offers only routes where the player dies: "
                    f"only_death_routes=True death_turn={result.get('death_turn')} "
                    f"final_hp={result.get('final_hp')} score={result.get('score')}"
                ),
            )
        )

    def _replay_divergence(self, block: _SearchBlock) -> list[SourceEvent] | None:
        diverged = [
            trace.trace_id
            for trace in block.traces.values()
            if trace.replay_seen and not trace.complete()
        ]
        if diverged:
            self.stats["failures"]["NO_ROUTE"] += 1
            return [
                SourceEvent.failed(
                    SolverFailure(
                        reason="NO_ROUTE",
                        captured_at_utc=_utc_now(),
                        battle_turn=block.request_turn,
                        detail=(
                            "the mod's own route replay did not reproduce the "
                            "published route (traceId=" + ",".join(sorted(diverged)) + ")"
                        ),
                    )
                )
            ]
        if not block.traces:
            # absence of the evidence channel is not a failure: pre-0.38 journals
            # predate it, so it is disclosed instead of being punished
            self.stats["answers_without_replay_validation"] += 1
        return None

    def _route_for(self, block: _SearchBlock) -> tuple[list[Any], list[_PendingAction]]:
        """Prefer an evidence trace, but only when it is provably the published one."""

        scraped = block.actions
        signature = route_signature(scraped) if scraped else None
        matches = [
            trace
            for trace in block.traces.values()
            if trace.actions and trace.complete() and route_signature(trace.actions) == signature
        ]
        if len(matches) == 1:
            self.stats["evidence_adopted"] += 1
            return group_route(matches[0].actions, block.turn_losses), matches[0].actions
        if len(matches) > 1:
            self.stats["evidence_ambiguous"] += 1
        elif signature is not None and block.traces:
            self.stats["evidence_no_match"] += 1
        return group_route(scraped, block.turn_losses), scraped


def is_v2_process_boundary(text: str) -> tuple[str, str] | None:
    """Return ``("begin"|"end", id-or-reason)`` for a process.jsonl boundary line."""

    stripped = text.strip()
    match = V2_COMBAT_LOG_BEGIN_RE.match(stripped)
    if match:
        return ("begin", match.group(1))
    match = V2_COMBAT_LOG_END_RE.match(stripped)
    if match:
        return ("end", match.group(1))
    return None


__all__ = [
    "FAILURE_REASONS",
    "V2_INSTANTANEOUS_RESULT_KEYS",
    "result_identity",
    "V1_ONLY_FAILURE_MARKERS",
    "V2CombatParser",
    "is_v2_process_boundary",
]
