"""Immutable byte-range evidence for Combat Solver diagnostic logs.

The game log does not carry a run id.  A range is therefore only producer
evidence: it becomes run evidence after ``BattleTracker`` binds the parsed
deploy to a concrete bridge-observed battle.  Consumers must re-read and
verify the range before accepting that binding.

Ranges are always physical byte offsets into the file that was read, aligned to
newline boundaries, for both grammars.  Grammar v2 only changes *which* marker
tokens are counted: its records are JSON envelopes, so a marker that the producer
embedded inside a multi-line ``Message`` is separated from its channel tag by the
two-character escape ``\\n`` rather than by whitespace, and its reuse announcements
are ``SEARCH_REUSED`` lines that v1 never had.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from combat_solver.loggrammar import GRAMMAR_V1, GRAMMAR_V2, GRAMMARS


MARKER_EVENTS = (
    "SEARCH_REQUEST",
    "DEPLOY_START",
    "DEPLOY_ACTION",
    "DEPLOY_END",
)
#: v2 adds the two request markers v1 never had: the reuse announcement (the
#: only turn-labelled request for every turn after a combat's first search) and
#: the turn-setup search start
MARKER_EVENTS_V2 = (
    "SEARCH_REQUEST",
    "SEARCH_REUSED",
    "TURN_SETUP_SEARCH_START",
    "DEPLOY_START",
    "DEPLOY_ACTION",
    "DEPLOY_END",
)
_SOLVER_EVENT_RE = re.compile(
    rb"\[CombatSolver/(?:Test|Debug)\]\s+"
    rb"(SEARCH_REQUEST|DEPLOY_START|DEPLOY_ACTION|DEPLOY_END)\b"
)
_SOLVER_EVENT_RE_V2 = re.compile(
    rb"\[CombatSolver/(?:Test|Debug)\](?:\s|\\n)+"
    rb"(TURN_SETUP_SEARCH_START|SEARCH_REQUEST|SEARCH_REUSED"
    rb"|DEPLOY_START|DEPLOY_ACTION|DEPLOY_END)\b"
)
_REQUEST_MARKERS_V1 = ("SEARCH_REQUEST",)
_REQUEST_MARKERS_V2 = ("SEARCH_REQUEST", "SEARCH_REUSED", "TURN_SETUP_SEARCH_START")
_MARKER_RES = {GRAMMAR_V1: _SOLVER_EVENT_RE, GRAMMAR_V2: _SOLVER_EVENT_RE_V2}
_MARKER_EVENTS = {GRAMMAR_V1: MARKER_EVENTS, GRAMMAR_V2: MARKER_EVENTS_V2}
_TOKEN_RE = re.compile(rb"([A-Za-z_][A-Za-z0-9_\[\]]*)=([^\s,;]+)")
#: a v2 token value ends at the JSON string quote, the escaped newline that
#: separates embedded payload lines, or the record's closing brace
_V2_TRAILING_RE = re.compile(r'(?:\\n|"|})+$')
_SHA256_RE = re.compile(r"^[0-9A-F]{64}$")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class LogRangeError(ValueError):
    """A log range is malformed, unavailable, or no longer byte-identical."""


def marker_events_for(grammar: str) -> tuple[str, ...]:
    try:
        return _MARKER_EVENTS[grammar]
    except KeyError:
        raise LogRangeError(f"unknown log grammar {grammar!r}") from None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _complete_lines(payload: bytes) -> list[bytes]:
    """Return LF-terminated line bodies; torn tails and lone CR are ignored."""

    return payload.split(b"\n")[:-1]


def _token_values(line: bytes, grammar: str) -> dict[str, str]:
    tokens = {
        key.decode("ascii"): value.decode("utf-8", errors="replace")
        for key, value in _TOKEN_RE.findall(line)
    }
    if grammar == GRAMMAR_V1:
        return tokens
    return {key: _V2_TRAILING_RE.sub("", value) for key, value in tokens.items()}


def scan_markers(
    payload: bytes, *, grammar: str = GRAMMAR_V1
) -> tuple[dict[str, int], list[tuple[str, dict[str, str]]]]:
    counts = {name: 0 for name in marker_events_for(grammar)}
    events: list[tuple[str, dict[str, str]]] = []
    pattern = _MARKER_RES[grammar]
    for line in _complete_lines(payload):
        match = pattern.search(line)
        if match is None:
            continue
        marker = match.group(1).decode("ascii")
        counts[marker] += 1
        events.append((marker, _token_values(line, grammar)))
    return counts, events


@dataclass(frozen=True)
class LogRange:
    source_path: str
    byte_start: int
    byte_end: int
    sha256: str
    marker_counts: dict[str, int]
    grammar: str = GRAMMAR_V1

    def to_json(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "byte_start": self.byte_start,
            "byte_end": self.byte_end,
            "sha256": self.sha256,
            "marker_counts": dict(self.marker_counts),
            "grammar": self.grammar,
        }

    @classmethod
    def from_json(cls, raw: Any) -> "LogRange":
        if not isinstance(raw, Mapping):
            raise LogRangeError("log range must be an object")
        source_path = raw.get("source_path")
        byte_start = raw.get("byte_start")
        byte_end = raw.get("byte_end")
        digest = raw.get("sha256")
        counts = raw.get("marker_counts")
        # Ranges captured before grammar v2 existed carry no marker and are v1 by
        # construction; an unknown marker is a different producer, not v1.
        grammar = raw.get("grammar", GRAMMAR_V1)
        if grammar not in GRAMMARS:
            raise LogRangeError("grammar must be one of the known log grammars")
        if not isinstance(source_path, str) or not source_path.strip():
            raise LogRangeError("source_path must be non-empty")
        if not _is_int(byte_start) or not _is_int(byte_end):
            raise LogRangeError("byte offsets must be integers")
        if byte_start < 0 or byte_end <= byte_start:
            raise LogRangeError("invalid half-open byte range")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise LogRangeError("sha256 must be 64 uppercase hexadecimal characters")
        expected = marker_events_for(grammar)
        if not isinstance(counts, Mapping) or set(counts) != set(expected):
            raise LogRangeError(
                f"marker_counts must contain the exact marker set for grammar {grammar}"
            )
        normalized: dict[str, int] = {}
        for name in expected:
            value = counts.get(name)
            if not _is_int(value) or value < 0:
                raise LogRangeError(f"invalid marker count for {name}")
            normalized[name] = value
        return cls(
            source_path=source_path,
            byte_start=byte_start,
            byte_end=byte_end,
            sha256=digest,
            marker_counts=normalized,
            grammar=grammar,
        )


def _resolved_regular_file(path: Path, allowed_root: Path | None) -> Path:
    try:
        is_link = path.is_symlink()
    except (OSError, ValueError) as exc:
        raise LogRangeError(f"log source is unavailable: {exc}") from exc
    if is_link:
        raise LogRangeError("log source symlinks are not accepted")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise LogRangeError(f"log source is unavailable: {exc}") from exc
    if allowed_root is not None:
        try:
            root = allowed_root.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise LogRangeError("log source escapes the allowed root") from exc
    try:
        mode = resolved.stat().st_mode
    except (OSError, ValueError) as exc:
        raise LogRangeError(f"cannot stat log source: {exc}") from exc
    if not stat.S_ISREG(mode):
        raise LogRangeError("log source is not a regular file")
    return resolved


def _stat_expected(path: Path) -> os.stat_result:
    try:
        return path.stat()
    except (OSError, ValueError) as exc:
        raise LogRangeError(f"cannot stat log source: {exc}") from exc


def _open_verified(path: Path, expected: os.stat_result) -> int:
    """Open without following a final symlink and confirm file identity."""

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | _NOFOLLOW
    try:
        fd = os.open(path, flags)
    except (OSError, ValueError) as exc:
        raise LogRangeError(f"cannot open log source: {exc}") from exc
    try:
        info = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise LogRangeError(f"cannot stat log source: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise LogRangeError("log source is not a regular file")
    if (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino):
        os.close(fd)
        raise LogRangeError("log source identity changed before read")
    return fd


def _read_exact(fd: int, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        block = os.read(fd, remaining)
        if not block:
            break
        chunks.append(block)
        remaining -= len(block)
    return b"".join(chunks)


def _read_slice(fd: int, start: int, end: int) -> bytes:
    try:
        size = os.fstat(fd).st_size
        if start < 0 or end <= start or end > size:
            raise LogRangeError(
                f"log range [{start},{end}) is outside file size {size}"
            )
        prefix_start = start - 1 if start > 0 else start
        os.lseek(fd, prefix_start, os.SEEK_SET)
        chunk = _read_exact(fd, end - prefix_start)
    except LogRangeError:
        raise
    except OSError as exc:
        raise LogRangeError(f"cannot read log source: {exc}") from exc
    if start > 0:
        if chunk[:1] != b"\n":
            raise LogRangeError(
                "log range must start at BOF or immediately after a newline"
            )
        payload = chunk[1:]
    else:
        payload = chunk
    if len(payload) != end - start:
        raise LogRangeError("short read while verifying log range")
    if payload[-1:] != b"\n":
        raise LogRangeError("log range must end immediately after a newline")
    return payload


def capture_log_range(
    source_path: Path | str,
    byte_start: int,
    byte_end: int,
    *,
    allowed_root: Path | str | None = None,
    grammar: str = GRAMMAR_V1,
) -> LogRange:
    if not _is_int(byte_start) or not _is_int(byte_end):
        raise LogRangeError("byte offsets must be integers")
    marker_events_for(grammar)  # reject an unknown grammar before touching disk
    root = Path(allowed_root) if allowed_root is not None else None
    source = _resolved_regular_file(Path(source_path), root)
    fd = _open_verified(source, _stat_expected(source))
    try:
        payload = _read_slice(fd, byte_start, byte_end)
    finally:
        os.close(fd)
    counts, _events = scan_markers(payload, grammar=grammar)
    return LogRange(
        source_path=str(source),
        byte_start=byte_start,
        byte_end=byte_end,
        sha256=hashlib.sha256(payload).hexdigest().upper(),
        marker_counts=counts,
        grammar=grammar,
    )


def verify_log_range(
    raw_range: LogRange | Mapping[str, Any],
    *,
    allowed_root: Path | str,
) -> tuple[LogRange, bytes, list[tuple[str, dict[str, str]]]]:
    evidence = raw_range if isinstance(raw_range, LogRange) else LogRange.from_json(raw_range)
    source = _resolved_regular_file(Path(evidence.source_path), Path(allowed_root))
    fd = _open_verified(source, _stat_expected(source))
    try:
        payload = _read_slice(fd, evidence.byte_start, evidence.byte_end)
    finally:
        os.close(fd)
    digest = hashlib.sha256(payload).hexdigest().upper()
    if digest != evidence.sha256:
        raise LogRangeError("log range SHA-256 mismatch")
    counts, events = scan_markers(payload, grammar=evidence.grammar)
    if counts != evidence.marker_counts:
        raise LogRangeError("log range marker counts mismatch")
    return evidence, payload, events


def validate_deploy_grammar(
    events: list[tuple[str, dict[str, str]]],
    *,
    turn: int,
    grammar: str = GRAMMAR_V1,
) -> None:
    if not _is_int(turn) or turn < 1:
        raise LogRangeError("deploy turn must be a positive integer")
    expected = str(turn)
    # v2 only ever announces a reuse for the turn it answers, so SEARCH_REUSED
    # carries the same binding weight v1's SEARCH_REQUEST carried.
    request_markers = (
        _REQUEST_MARKERS_V1 if grammar == GRAMMAR_V1 else _REQUEST_MARKERS_V2
    )
    phase = "search"
    seen_start = False
    seen_end = False
    matching_request = False
    for marker, tokens in events:
        if marker in request_markers:
            if phase != "search":
                raise LogRangeError(f"{marker} is out of order")
            if tokens.get("turn") == expected:
                matching_request = True
        elif marker == "DEPLOY_START":
            if phase != "search":
                raise LogRangeError("DEPLOY_START is out of order")
            if tokens.get("turn") != expected:
                raise LogRangeError("DEPLOY_START turn does not match bound turn")
            phase = "action"
            seen_start = True
        elif marker == "DEPLOY_ACTION":
            if phase != "action":
                raise LogRangeError("DEPLOY_ACTION is out of order")
            if tokens.get("turn") != expected:
                raise LogRangeError("DEPLOY_ACTION turn does not match bound turn")
        elif marker == "DEPLOY_END":
            if phase != "action":
                raise LogRangeError("DEPLOY_END is out of order")
            if tokens.get("turn") != expected:
                raise LogRangeError("DEPLOY_END turn does not match bound turn")
            phase = "end"
            seen_end = True
    if not seen_start or not seen_end:
        raise LogRangeError("deploy range must contain exactly one DEPLOY_START/DEPLOY_END")
    if not matching_request:
        raise LogRangeError("deploy range lacks a matching SEARCH_REQUEST")


def ranges_overlap(left: LogRange, right: LogRange) -> bool:
    try:
        left_path = Path(left.source_path).resolve(strict=False)
        right_path = Path(right.source_path).resolve(strict=False)
    except OSError:
        return False
    return (
        left_path == right_path
        and left.byte_start < right.byte_end
        and right.byte_start < left.byte_end
    )


__all__ = [
    "MARKER_EVENTS",
    "MARKER_EVENTS_V2",
    "LogRange",
    "LogRangeError",
    "capture_log_range",
    "marker_events_for",
    "ranges_overlap",
    "scan_markers",
    "validate_deploy_grammar",
    "verify_log_range",
]
