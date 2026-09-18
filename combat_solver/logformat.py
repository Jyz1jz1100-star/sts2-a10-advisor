"""Live adapter: Combat Solver answers from the game's diagnostic logs.

Grammar **v1**, calibrated against a real installed-mod session log
(CombatSolver 0.25.3, 2026-09-02, ``%APPDATA%\\SlayTheSpire2\\logs\\godot.log``):

- every solver line carries the ``[CombatSolver/Test]`` marker (after an
  ``[INFO]``/``[ERROR]`` level tag and the ``[CombatSolver]`` logger name);
- ``SEARCH_REQUEST generation=N reason=... turn=T`` opens a search block;
- ``ACTION turn=K kind=PlayCard card_id=X occurrence=0 target_index=I ...`` /
  ``kind=UsePotion potion_id=P slot=S ...`` / ``kind=EndTurn`` materialize the
  route (``-1``/``-``/``None`` mean "no value");
- ``RESULT ... reused=True|False ... searched_turns=K ...
  battle_hp_lost_so_far=F projected_battle_hp_lost=P elapsed_ms=E
  total_elapsed_ms=E total_worker_allocated_bytes=B ... final_hp=H
  combat_ended_turn=C death_turn=- only_death_routes=False ...`` carries the
  prediction and cost stats (``reused=True`` lines are route re-emissions
  with zero cost and are excluded from solve-time/node/memory stats);
- ``TURN_OUTCOME turn=K hp_lost=L sold_hp=... max_block=... energy_left=...``
  completes the per-turn forecast of the block;
- ``SEARCH_FAILURE generation=N exception=<Exception ...>`` (``[ERROR]``
  level) and ``SEARCH_STALE generation=N`` are explicit failures;
- ``SEARCH_INTERIM_RESULT``/``FORECAST``/``COVERAGE``/``START`` and the rest
  are ignored for snapshot purposes.

One snapshot is emitted per completed block: when the RESULT arrives and the
TURN_OUTCOME count reaches ``searched_turns`` (or immediately when no
outcomes are expected), or at the next block boundary as a safety net. A
block whose REQUEST never got a RESULT flushes as a ``NO_ROUTE`` failure.

Records carry ``state_hash=None`` (the mod cannot compute the bridge's
canonical state hash) and bind by ``battle_turn`` with the freshness guard.

Grammar **v2** (CombatSolver 0.35.5 - 0.41.0, the per-combat JSON Lines
journal under ``logs\\CombatSolver\\<pid>-<guid>\\``) reads the same block
payloads through ``combat_solver.logv2``, which documents the turn-binding,
search re-arm, answer-echo and failure-taxonomy differences.  The v1 rules
above are unchanged by that work: they are pinned by replay against the 0.25.3,
0.29.1 and 0.31.0 sessions and must not be perturbed by v2 changes.

Selection is by content (``combat_solver.loggrammar.sniff_grammar``) or by the
explicit ``grammar=`` parameter, never by file name; a pinned reader that meets
a file of the other grammar reports it instead of reinterpreting it.
"""
from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from combat_solver.loggrammar import (
    CARD_KEYS as _CARD_KEYS,
    _EMPTY_DIGEST,
    GRAMMAR_V1,
    GRAMMAR_V2,
    GRAMMARS,
    KIND_TOKENS as _KIND_TOKENS,
    POTION_KEYS as _POTION_KEYS,
    SNIFF_UNDECIDED,
    SOLVER_LINE_MARKERS as _SOLVER_LINE_MARKERS,
    TURN_KEYS as _TURN_KEYS,
    _PendingAction,
    _first_int,
    _first_str,
    _kv_bool,
    _kv_index,
    _kv_int,
    _line_event,
    _prefix_digest,
    _RawLine,
    _SearchBlock,
    _utc_now,
    discover_v2_sources,
    group_route,
    is_solver_line,
    parse_kv_tokens,
    snapshot_payload,
    sniff_grammar,
)
from combat_solver.logranges import LogRangeError, capture_log_range
from combat_solver.logv2 import V2CombatParser, is_v2_process_boundary
from combat_solver.reader import DeployRecord, SourceEvent
from combat_solver.snapshot import (
    FAILURE_REASONS,
    RouteAction,
    SolverFailure,
    snapshot_from_json,
)

#: ``grammar=`` value that classifies every source file by content
GRAMMAR_AUTO = "auto"
#: re-exported: the marker vocabulary and line helpers live in ``loggrammar`` so
#: both grammars parse the same tokens
SOLVER_LINE_MARKERS = _SOLVER_LINE_MARKERS


def read_solver_settings(settings_path: str | Path) -> dict[str, int | str | None]:
    """Extract the budget-relevant fields from combat_solver_settings.json."""

    try:
        with Path(settings_path).open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {"tier": None, "time_budget_ms": None, "memory_limit_mb": None}
    deep_seconds = data.get("deepTimeLimitSeconds")
    no_gc_gb = data.get("noGcRegionBudgetGigabytes")
    return {
        "tier": data.get("performancePreset"),
        "time_budget_ms": (
            int(deep_seconds * 1000) if isinstance(deep_seconds, (int, float)) else None
        ),
        "memory_limit_mb": (
            int(no_gc_gb * 1024) if isinstance(no_gc_gb, (int, float)) else None
        ),
    }



class LogTailSource:
    """Tails solver log sources and emits snapshots/failures for both grammars.

    ``settings_path`` (optional) points at ``combat_solver_settings.json`` so
    budget tier/limits come from the mod's own persistence instead of the
    log lines. Historical log content is never replayed: files are first
    seen at EOF (live tailing only).

    ``grammar`` selects the codec for every source: ``"auto"`` (default)
    classifies each file by content, ``"v1"``/``"v2"`` pin it. A pinned reader
    that meets content sniffing as the other grammar yields a typed
    ``READER_DOWN`` failure for that file instead of reinterpreting it.
    """

    name = "logtail"

    def __init__(
        self,
        log_dir: Path | str,
        settings_path: Path | str | None = None,
        mod_version: str | None = None,
        replay: bool = False,
        grammar: str = GRAMMAR_AUTO,
    ):
        if grammar not in (GRAMMAR_AUTO, *GRAMMARS):
            raise ValueError(f"unknown log grammar {grammar!r}")
        self._log_dir = Path(log_dir)
        self._mod_version = mod_version
        self._replay = replay
        self._grammar = grammar
        settings = (
            read_solver_settings(settings_path)
            if settings_path is not None
            else {"tier": None, "time_budget_ms": None, "memory_limit_mb": None}
        )
        self._tier = settings.get("tier")
        self._time_budget_ms = settings.get("time_budget_ms")
        self._memory_limit_mb = settings.get("memory_limit_mb")
        self._offsets: dict[Path, int] = {}
        self._file_identities: dict[Path, tuple[int, int]] = {}
        self._prefix_digests: dict[Path, bytes] = {}
        self._started: set[Path] = set()
        self._active_path: Path | None = None
        self._block = _SearchBlock()
        self._emitted_signature: tuple | None = None
        self._emitted_reuse_turn: int | None = None
        # deployment bookkeeping (the mod's own execution records, only
        # present when the mod itself deployed the route)
        self._deploy_actions: dict[int, list[RouteAction]] = {}
        self._deploy_end_turn: dict[int, bool] = {}
        self._deploy_range_starts: dict[int, tuple[Path, int]] = {}
        self._last_search: tuple[Path, int, int] | None = None
        # grammar v2: one parser per journal file, because a v2 file is one combat
        self._file_grammar: dict[Path, str] = {}
        self._v2_parsers: dict[Path, V2CombatParser] = {}
        self._confirmed_battle_ids: set[str] = set()
        self._reported_rejections: set[Path] = set()
        self.stats: dict[str, Any] = {
            "v1_files": set(),
            "v2_files": set(),
            "rejected_files": set(),
        }

    @property
    def log_dir(self) -> Path:
        return self._log_dir

    @property
    def grammar(self) -> str:
        return self._grammar

    def _reset_parser_state(self) -> None:
        """Discard incomplete state when the producer file identity changes."""

        self._block = _SearchBlock()
        self._emitted_signature = None
        self._emitted_reuse_turn = None
        self._deploy_actions.clear()
        self._deploy_end_turn.clear()
        self._deploy_range_starts.clear()
        self._last_search = None

    def poll(self) -> list[SourceEvent]:
        events: list[SourceEvent] = []
        if not self._log_dir.is_dir():
            return events
        for path in sorted(self._log_dir.glob("*.log")):
            grammar = self._resolve_grammar(path, default=GRAMMAR_V1)
            if grammar is None:
                events.extend(self._reject_file(path))
                continue
            if grammar != GRAMMAR_V1:
                # a Godot-named file that carries journal envelopes is v2 content;
                # reading it with the v1 block machine would mis-bind every offset
                events.extend(self._poll_v2_file(path, claimed_battle_id=None))
                continue
            self.stats["v1_files"].add(path.name)
            fresh, reset = self._read_new(path)
            if (reset and self._active_path in (None, path)) or (
                fresh and self._active_path not in (None, path)
            ):
                self._reset_parser_state()
            if fresh:
                self._active_path = path
            for line in fresh:
                self._feed_line(line.text, events, line)
        if self._grammar != GRAMMAR_V1:
            events.extend(self._poll_v2())
        # End-of-stream flush: the last block of a battle may never see a
        # following SEARCH_REQUEST (combat ends first). Emitting here keeps
        # single-block sessions (and tests) correct; live battles always end
        # with a RESET/GC_COMBAT_LIFECYCLE_DETACHED line that arrives later,
        # so this cannot double-emit.
        self._flush_block(events)
        for parser in self._v2_parsers.values():
            parser.finish(events)
        return events

    # ------------------------------------------------------- grammar selection
    def _resolve_grammar(self, path: Path, *, default: str) -> str | None:
        """Classify one source once and freeze the answer for its lifetime.

        Returns ``None`` when the file cannot be classified safely: content that
        smells like a journal envelope but does not decode, or content of the
        other grammar while a grammar is pinned.
        """

        cached = self._file_grammar.get(path)
        if cached is not None:
            return cached
        sniffed = sniff_grammar(path)
        if sniffed == SNIFF_UNDECIDED:
            # nothing to reinterpret yet: keep the container default and re-sniff
            return default
        if sniffed is None:
            return None
        if self._grammar != GRAMMAR_AUTO and sniffed != self._grammar:
            return None
        self._file_grammar[path] = sniffed
        return sniffed

    def _reject_file(self, path: Path) -> list[SourceEvent]:
        """Report an unclassifiable source once per file, never silently."""

        if path in self._reported_rejections:
            return []
        self._reported_rejections.add(path)
        self.stats["rejected_files"].add(path.name)
        pinned = (
            f"reader is pinned to grammar {self._grammar}"
            if self._grammar != GRAMMAR_AUTO
            else "no grammar classifies its first record"
        )
        return [
            SourceEvent.failed(
                SolverFailure(
                    reason="READER_DOWN",
                    captured_at_utc=_utc_now(),
                    detail=(
                        f"{path.name}: refused to read ({pinned}); the file is not "
                        "parsed under any other grammar"
                    ),
                )
            )
        ]

    # --------------------------------------------------------------- grammar v2
    def _poll_v2(self) -> list[SourceEvent]:
        events: list[SourceEvent] = []
        for source in discover_v2_sources(self._log_dir):
            events.extend(self._poll_v2_file(source.path, source.claimed_battle_id))
        return events

    def _poll_v2_file(
        self, path: Path, claimed_battle_id: str | None
    ) -> list[SourceEvent]:
        events: list[SourceEvent] = []
        grammar = self._resolve_grammar(path, default=GRAMMAR_V2)
        if grammar is None:
            events.extend(self._reject_file(path))
            return events
        if grammar != GRAMMAR_V2:
            # a journal-named file holding plain Godot lines: not this codec's
            return events
        parser = self._v2_parsers.get(path)
        fresh, reset = self._read_new(path)
        if parser is not None and reset:
            self._v2_parsers.pop(path, None)
            parser = None
        if parser is None and (fresh or path in self._started):
            parser = self._v2_parsers[path] = V2CombatParser(
                path,
                log_dir=self._log_dir,
                mod_version=self._mod_version,
                tier=self._tier,
                time_budget_ms=self._time_budget_ms,
                memory_limit_mb=self._memory_limit_mb,
                confirmed_battle_ids=self._confirmed_battle_ids,
                report_abandoned_deploys=self._replay,
            )
        if parser is None:
            return events
        if fresh:
            self.stats["v2_files"].add(path.name)
        if claimed_battle_id and claimed_battle_id in self._confirmed_battle_ids:
            parser.set_battle_id(claimed_battle_id)
        for line in fresh:
            parser.feed_record(line, events)
        return events

    def file_grammar(self, path: Path | str) -> str | None:
        """The frozen classification of one source, ``None`` if never read."""

        return self._file_grammar.get(Path(path))

    def v2_stats(self) -> dict[str, dict[str, Any]]:
        """Per-file grammar v2 counters, for the offline replay report."""

        return {
            str(path): {
                key: (dict(value) if isinstance(value, dict) else value)
                for key, value in parser.stats.items()
            }
            for path, parser in self._v2_parsers.items()
        }

    def battle_ids(self) -> dict[str, str]:
        """Combat file name -> battle id confirmed by the producer's own records."""

        return {
            path.name: parser.battle_id
            for path, parser in self._v2_parsers.items()
            if parser.battle_id is not None
        }


    def _read_new(self, path: Path) -> tuple[list[_RawLine], bool]:
        """Read complete appended lines with real byte offsets.

        The cursor stops before an unterminated tail.  That tail is retried on
        the next poll, so a writer killed mid-UTF-8 sequence cannot create a
        synthetic marker or shift later offsets.  A digest of the entire
        consumed prefix detects rewrites anywhere before the cursor.
        """

        reset = False
        try:
            file_stat = path.stat()
            size = file_stat.st_size
            identity = (file_stat.st_dev, file_stat.st_ino)
            if path not in self._started:
                self._started.add(path)
                self._offsets[path] = 0 if self._replay else size
                self._file_identities[path] = identity
                self._prefix_digests[path] = _prefix_digest(
                    path, self._offsets[path]
                )
                if not self._replay:
                    return [], False
            offset = self._offsets.get(path, 0)
            if self._file_identities.get(path) != identity or size < offset:
                reset = True
            elif offset and (
                _prefix_digest(path, offset) != self._prefix_digests.get(path)
            ):
                reset = True
            if reset:
                self._file_identities[path] = identity
                # Persist this before early returns: an empty or unterminated
                # replacement must not retain the old cursor or parser state.
                offset = 0
                self._offsets[path] = 0
                self._prefix_digests[path] = _EMPTY_DIGEST
            if size == offset:
                return [], reset
            with path.open("rb") as handle:
                handle.seek(offset)
                payload = handle.read(size - offset)
            final_newline = payload.rfind(b"\n")
            if final_newline < 0:
                return [], reset
            complete = payload[: final_newline + 1]
            lines: list[_RawLine] = []
            relative = 0
            # Use LF-only boundaries, matching logranges._complete_lines.  A
            # lone CR remains content and cannot create a parser-only event.
            for body in complete.split(b"\n")[:-1]:
                end = relative + len(body) + 1
                lines.append(
                    _RawLine(
                        path=path,
                        byte_start=offset + relative,
                        byte_end=offset + end,
                        text=body.decode("utf-8", errors="replace"),
                    )
                )
                relative = end
            self._offsets[path] = offset + len(complete)
            self._prefix_digests[path] = _prefix_digest(path, self._offsets[path])
            return lines, reset
        except OSError:
            return [], reset

    # ------------------------------------------------------------------ feed
    def _feed_line(
        self,
        line: str,
        events: list[SourceEvent],
        source_line: _RawLine | None = None,
    ) -> None:
        if not is_solver_line(line):
            return
        kv = parse_kv_tokens(line)
        marker = _line_event(line)
        if marker == "SEARCH_REQUEST":
            # observed block order: REQUEST -> RESULT -> TURN_OUTCOME* ->
            # ACTION* -> next REQUEST; the route is complete only at the
            # next block boundary.
            self._flush_block(events)
            self._block = _SearchBlock(
                generation=_kv_int(kv, "generation"),
                request_turn=_kv_int(kv, "turn"),
            )
            if source_line is not None:
                self._last_search = (
                    source_line.path,
                    source_line.byte_start,
                    self._block.request_turn or -1,
                )
            return
        if marker == "RESET":
            self._flush_block(events)
            self._block = _SearchBlock()
            return
        if marker == "ACTION":
            self._feed_action(kv)
            return
        if marker == "DEPLOY_START":
            turn = _kv_int(kv, "turn")
            if turn is not None:
                self._deploy_actions[turn] = []
                self._deploy_end_turn[turn] = False
                if source_line is not None:
                    start = source_line.byte_start
                    if (
                        self._last_search is not None
                        and self._last_search[0] == source_line.path
                        and self._last_search[2] == turn
                    ):
                        start = self._last_search[1]
                    self._deploy_range_starts[turn] = (source_line.path, start)
            return
        if marker == "DEPLOY_ACTION":
            self._feed_deploy_action(kv, events)
            return
        if marker == "DEPLOY_END":
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
                source_line is not None
                and range_start is not None
                and range_start[0] == source_line.path
            ):
                try:
                    log_range = capture_log_range(
                        source_line.path,
                        range_start[1],
                        source_line.byte_end,
                        allowed_root=self._log_dir,
                    )
                except LogRangeError:
                    # Keep the deploy as a diagnostic event, but the tracker
                    # marks it unverified and the assessor will not accept it.
                    log_range = None
            events.append(
                SourceEvent.deployed(
                    DeployRecord(
                        turn=turn,
                        actions=actions,
                        end_turn=end_turn,
                        captured_at_utc=_utc_now(),
                        log_range=log_range,
                    )
                )
            )
            return
        if marker == "SEARCH_INTERIM_RESULT":
            return
        if marker == "RESULT":
            # Observed stream shape: within one battle the mod re-emits the
            # full route package (RESULT + TURN_OUTCOME* + ACTION*) after
            # each played card WITHOUT a new SEARCH_REQUEST, and `reused=True`
            # RESULTs repeat the identical package per turn. A second RESULT
            # closes the current block so the trailing ACTION lines rebind to
            # their own RESULT.
            if self._block.has_result:
                self._flush_block(events)
            self._block.result = kv
            return
        if marker == "TURN_OUTCOME":
            turn = _kv_int(kv, "turn")
            hp_lost = _kv_int(kv, "hp_lost")
            if turn is not None and hp_lost is not None:
                self._block.turn_losses[turn] = hp_lost
            return
        if marker == "SEARCH_FAILURE":
            turn = self._block.request_turn
            self._block = _SearchBlock()
            events.append(
                SourceEvent.failed(
                    SolverFailure(
                        reason="SEARCH_ERROR",
                        captured_at_utc=_utc_now(),
                        battle_turn=turn,
                        detail=line.strip(),
                    )
                )
            )
            return
        if marker == "SEARCH_STALE":
            events.append(
                SourceEvent.failed(
                    SolverFailure(
                        reason="STALE_STATE",
                        captured_at_utc=_utc_now(),
                        battle_turn=self._block.request_turn,
                        detail=line.strip(),
                    )
                )
            )
            return

    def _feed_action(self, kv: dict[str, str]) -> None:
        kind_raw = kv.get("kind")
        if kind_raw not in _KIND_TOKENS:
            return
        kind = _KIND_TOKENS[kind_raw]
        if kind == "potion":
            card_id = _first_str(kv, _POTION_KEYS)
        elif kind == "play":
            card_id = _first_str(kv, _CARD_KEYS)
        else:
            card_id = None
        self._block.actions.append(
            _PendingAction(
                kind=kind,
                card_id=card_id,
                target_index=_kv_index(kv, "target_index"),
                turn=_first_int(kv, _TURN_KEYS),
            )
        )

    def _feed_deploy_action(self, kv: dict[str, str], events: list[SourceEvent]) -> None:
        turn = _kv_int(kv, "turn")
        if turn is None:
            return
        potion = _first_str(kv, ("potion", "potion_id"))
        if potion is not None:
            action = RouteAction(kind="potion", card_id=potion,
                                 target_index=_kv_index(kv, "target_index"))
        else:
            card = _first_str(kv, ("card", "card_id"))
            if card is None:
                return
            action = RouteAction(kind="play", card_id=card,
                                 target_index=_kv_index(kv, "target_index"))
        self._deploy_actions.setdefault(turn, []).append(action)

    def _maybe_emit(self, events: list[SourceEvent]) -> None:  # noqa: ARG002
        # Emission happens exclusively at block boundaries (_flush_block):
        # ACTION lines trail RESULT/TURN_OUTCOME, so any earlier emission
        # would produce snapshots without routes.
        return

    def _flush_block(self, events: list[SourceEvent]) -> None:
        block = self._block
        if block.has_result:
            events.extend(self._emit_snapshot())
        elif block.request_turn is not None:
            events.append(
                SourceEvent.failed(
                    SolverFailure(
                        reason="NO_ROUTE",
                        captured_at_utc=_utc_now(),
                        battle_turn=block.request_turn,
                        detail=f"SEARCH_REQUEST generation={block.generation} got no RESULT",
                    )
                )
            )
        self._block = _SearchBlock()

    # ------------------------------------------------------------------ emit
    def _emit_snapshot(self) -> list[SourceEvent]:
        block = self._block
        result = block.result or {}
        reused = _kv_bool(result, "reused")
        turn = block.request_turn
        if reused and block.actions:
            # A reused package replays the surviving suffix of the original
            # route; its first action is the instruction for the CURRENT turn,
            # which is the turn this snapshot must bind to.
            first = block.actions[0]
            if first.turn is not None:
                turn = first.turn
        if turn is None and block.actions:
            turns = [a.turn for a in block.actions if a.turn is not None]
            turn = min(turns) if turns else None
        if turn is None:
            return [
                SourceEvent.failed(
                    SolverFailure(
                        reason="PARSE_ERROR",
                        captured_at_utc=_utc_now(),
                        detail="solver block without a turn label",
                    )
                )
            ]
        signature = block.route_signature()
        reused = _kv_bool(result, "reused")
        # Duplicate suppression: a `reused=True` block for the same turn whose
        # route is byte-identical to the last emission is a log echo of the
        # same answer, not a new solver result. Fresh searches and blocks with
        # changed routes always pass.
        if (
            signature is not None
            and reused
            and signature == self._emitted_signature
            and turn == self._emitted_reuse_turn
        ):
            return []
        self._emitted_signature = signature
        self._emitted_reuse_turn = turn if reused else None
        steps = _group_route(block.actions, block.turn_losses)
        payload = snapshot_payload(
            block,
            turn=turn,
            steps=steps,
            tier=self._tier,
            time_limit_ms=self._time_budget_ms,
            memory_limit_mb=self._memory_limit_mb,
            reader=self.name,
            mod_version=self._mod_version,
            source_file=None,
        )
        if payload is None:
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
        return [SourceEvent.of(snapshot)]


# ``_group_route``/``_line_event``/``_utc_now`` are the shared grammar helpers;
# they stay importable from this module because the v1 block machine uses them.
_group_route = group_route


def failure_from_line(line: str) -> SolverFailure | None:
    """Grammar v1 only: a failure declared by a ``failure=``/``reason=`` token."""

    kv = parse_kv_tokens(line)
    for reason in FAILURE_REASONS:
        if kv.get("failure") == reason or kv.get("reason") == reason:
            return SolverFailure(
                reason=reason, captured_at_utc=_utc_now(), detail=line.strip()
            )
    return None


__all__ = [
    "LogTailSource",
    "read_solver_settings",
    "is_solver_line",
    "parse_kv_tokens",
    "failure_from_line",
    "GRAMMAR_AUTO",
    "GRAMMAR_V1",
    "GRAMMAR_V2",
    "SOLVER_LINE_MARKERS",
]
