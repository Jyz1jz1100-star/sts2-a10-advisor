"""Keep Combat Solver's full-auto mode enabled.

The Combat Solver panel does not expose a machine-facing API.  The only
observable contract is the ``[CombatSolver/Test]`` log stream, so this module
uses a small, deliberately conservative state machine:

* a ``SEARCH_REQUEST ... turn=1`` followed by ``UI_STATE state=ready`` starts
  a fresh battle epoch;
* the epoch is considered healthy only after
  ``FULL_AUTO_DEPLOY turn=1`` is observed;
* if that event does not arrive before the bounded confirmation timeout, the
  keeper requests one toggle click, then waits for a log confirmation before
  allowing another click;
* an explicit ``FULL_AUTO enabled=false`` still causes an immediate recovery,
  but it is not the only recovery path.

This matters because the real failure mode is not necessarily an ``enabled``
line.  When full-auto is off, the mod can search a route, fail to deploy it,
switch to ``control_mode=manual_plus_solver`` after the first continuation
divergence, and only later be toggled on by a human.  The turn-one deployment
watchdog catches that sequence before it becomes a manual-plus-solver run.

The tailer also treats a newly created/truncated log as a new source.  That
keeps stale mode/battle state from crossing a game restart or log rotation.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

# Recalibrated live against Combat Solver 0.29.1 at a 1026x768 client:
# the full-auto toggle sits at (256, 408) when the panel is at (8, 261.5).
# The toggle is on the panel's BOTTOM action row, so the row's height varies
# with the route count; unsuccessful recoveries therefore walk a small
# vertical offset list until the log confirms the toggle.
BUTTON_RATIO = (0.2495, 0.5313)
GAME_WINDOW_TITLE = "Slay the Spire 2"
# The ratio was calibrated with the mod's default persisted panel position.
# The settings/log position is applied as a delta at click time when the user
# drags the overlay.  If no trustworthy position is available, the click
# path refuses to run rather than guessing at a screen coordinate.
CALIBRATED_OVERLAY_POSITION = (8.0, 261.5)
# Vertical deltas (viewport px) tried on successive unconfirmed clicks. The
# toggle lives on the panel's bottom action row, and the panel height depends
# on its content mode: compact (button ~y408) and detailed (button ~y302) were
# both observed live at the same overlay position.
CLICK_Y_OFFSET_STEPS = (0.0, -106.0, -26.0, -52.0, -132.0, -78.0, 26.0)

# The mod's first search can take seconds on the VeryHigh preset.  The timer
# starts only once the result is ready, so this is short enough to catch a
# missing turn-one deployment without clicking while a search is still being
# built.  Keep these public so tests and operators can use the same contract.
BATTLE_CONFIRM_TIMEOUT = 5.0
CLICK_CONFIRM_TIMEOUT = 3.0
CLICK_RETRY_INTERVAL = 2.0
CLICK_DEBOUNCE = 0.75

_LINE_RE = re.compile(r"\bFULL_AUTO\s+enabled=(?P<enabled>\w+)", re.IGNORECASE)
_FULL_AUTO_DEPLOY_RE = re.compile(
    r"\bFULL_AUTO_DEPLOY\s+turn=(?P<turn>\d+)\b", re.IGNORECASE
)
_SEARCH_REQUEST_RE = re.compile(r"\bSEARCH_REQUEST\b", re.IGNORECASE)
_UI_READY_RE = re.compile(
    r"\bUI_STATE\s+state=ready\b", re.IGNORECASE
)
_RESET_RE = re.compile(r"\bRESET\b(?:\s+reason=|\s*$)", re.IGNORECASE)
_TURN_FIELD_RE = re.compile(r"\bturn=(?P<turn>\d+)\b", re.IGNORECASE)
_GENERATION_FIELD_RE = re.compile(
    r"\bgeneration=(?P<generation>\d+)\b", re.IGNORECASE
)
_UI_POSITION_RE = re.compile(
    r"\bUI_POSITION_LOADED\b.*?\bx=(?P<x>-?\d+(?:\.\d+)?)\b"
    r".*?\by=(?P<y>-?\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)

# Kept as a named regex for callers which used the old helper during smoke
# tests.  Matching is now done through the more tolerant field parser below.
_BATTLE_START_RE = re.compile(
    r"\bSEARCH_REQUEST\b(?=.*\bturn=1\b)", re.IGNORECASE
)


def _field_int(pattern: re.Pattern[str], line: str) -> int | None:
    match = pattern.search(line)
    if not match:
        return None
    group = match.groupdict().get("turn")
    return int(group) if group is not None else int(match.group(1))


def _enabled_value(line: str) -> bool | None:
    match = _LINE_RE.search(line)
    if not match:
        return None
    value = match.group("enabled").lower()
    if value in {"true", "on", "yes", "1"}:
        return True
    if value in {"false", "off", "no", "0"}:
        return False
    return None


def _valid_overlay_position(value: object) -> tuple[float, float] | None:
    """Return a finite ``(x, y)`` overlay position, or ``None``."""
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        return None
    try:
        x, y = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    # Viewport coordinates outside this broad bound are never meaningful for
    # STS2 and could produce a misleading script literal.
    if abs(x) > 100_000 or abs(y) > 100_000:
        return None
    return x, y


def read_overlay_position_from_settings(
    path: Path | None = None,
) -> tuple[float, float] | None:
    """Read Combat Solver's persisted overlay position, if trustworthy.

    The mod settings file is local diagnostic state, not a control channel.
    Malformed/missing settings deliberately return ``None``; the caller then
    waits for the runtime ``UI_POSITION_LOADED`` log marker instead of using a
    guessed coordinate.
    """
    settings_path = (
        Path(path)
        if path is not None
        else Path.home()
        / "AppData"
        / "Roaming"
        / "SlayTheSpire2"
        / "combat_solver_settings.json"
    )
    try:
        with settings_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    return _valid_overlay_position(
        (payload.get("overlayPositionX"), payload.get("overlayPositionY"))
    )


def should_click(line: str, last_known_on: bool | None) -> bool:
    """Legacy pure decision helper.

    The live keeper below intentionally does *not* use this helper for battle
    recovery: a stale ``last_known_on`` value cannot prove that this battle
    received a turn-one deployment.  The helper remains source-compatible for
    small scripts and retains its original explicit-line semantics.
    """
    enabled = _enabled_value(line)
    if enabled is not None:
        return not enabled
    if _BATTLE_START_RE.search(line):
        return last_known_on is not True
    return False


@dataclass
class _BattleEpoch:
    generation: int | None
    search_fingerprint: str
    search_at: float
    ready_at: float | None = None
    mode_enabled: bool | None = None
    deploy_turn_one: bool = False
    deploy_any: bool = False
    timeout_reached: bool = False


class FullAutoKeeper:
    """Log-driven full-auto state machine.

    ``on_line`` and ``tick`` return ``True`` once when a toggle click should
    be attempted.  The caller must call :meth:`click_started` before invoking
    the OS click and then call :meth:`click_failed` if that attempt failed.
    A successful click is confirmed by a later ``FULL_AUTO enabled=true`` or
    ``FULL_AUTO_DEPLOY`` event; until then duplicate requests are suppressed.

    ``clock`` is injectable to make timeout, delayed-confirmation, and
    duplicate-suppression tests deterministic.
    """

    def __init__(
        self,
        *,
        battle_timeout: float = BATTLE_CONFIRM_TIMEOUT,
        click_confirm_timeout: float = CLICK_CONFIRM_TIMEOUT,
        retry_interval: float = CLICK_RETRY_INTERVAL,
        click_debounce: float = CLICK_DEBOUNCE,
        overlay_position: tuple[float, float] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if battle_timeout < 0 or click_confirm_timeout < 0:
            raise ValueError("timeouts must be non-negative")
        if retry_interval < 0 or click_debounce < 0:
            raise ValueError("retry_interval and click_debounce must be non-negative")
        self.battle_timeout = float(battle_timeout)
        self.click_confirm_timeout = float(click_confirm_timeout)
        self.retry_interval = float(retry_interval)
        self.click_debounce = float(click_debounce)
        self._clock = clock
        self._overlay_position = _valid_overlay_position(overlay_position)

        self._battle: _BattleEpoch | None = None
        self._last_known_on: bool | None = None
        self._request_pending = False
        self._pending_reason: str | None = None
        self._click_in_flight = False
        self._click_started_at: float | None = None
        self._click_confirmation_seen = False
        self._click_offset_step = 0
        self._last_click_at: float | None = None
        self._next_retry_at: float = 0.0
        self._forced_recovery = False
        self._verification_warning = False
        self._log_epoch = 0

    # -------------------------------------------------------------- properties
    @property
    def last_known_on(self) -> bool | None:
        return self._last_known_on

    @property
    def battle_generation(self) -> int | None:
        return self._battle.generation if self._battle else None

    @property
    def battle_active(self) -> bool:
        return self._battle is not None

    @property
    def battle_ready(self) -> bool:
        return bool(self._battle and self._battle.ready_at is not None)

    @property
    def turn_one_deployed(self) -> bool:
        return bool(self._battle and self._battle.deploy_turn_one)

    @property
    def deploy_seen(self) -> bool:
        return bool(self._battle and self._battle.deploy_any)

    @property
    def click_in_flight(self) -> bool:
        return self._click_in_flight

    @property
    def click_requested(self) -> bool:
        return self._request_pending

    @property
    def pending_reason(self) -> str | None:
        return self._pending_reason

    @property
    def verification_warning(self) -> bool:
        """Whether an enabled line arrived without a turn-one deployment."""
        return self._verification_warning

    @property
    def log_epoch(self) -> int:
        return self._log_epoch

    @property
    def overlay_position(self) -> tuple[float, float] | None:
        """Current panel origin from settings or the runtime log marker."""
        return self._overlay_position

    @property
    def click_confirmation_seen(self) -> bool:
        """Whether this physical click has a post-click log confirmation."""
        return self._click_confirmation_seen

    def set_overlay_position(self, position: object) -> bool:
        """Set a validated panel origin; return ``False`` for bad input."""
        parsed = _valid_overlay_position(position)
        if parsed is None:
            return False
        self._overlay_position = parsed
        return True

    # --------------------------------------------------------------- lifecycle
    def reset_for_log(self) -> None:
        """Forget all mode/battle state after rotation or a game restart."""
        self._battle = None
        self._last_known_on = None
        self._request_pending = False
        self._pending_reason = None
        self._click_in_flight = False
        self._click_started_at = None
        self._click_confirmation_seen = False
        self._last_click_at = None
        self._next_retry_at = 0.0
        self._forced_recovery = False
        self._verification_warning = False
        self._log_epoch += 1

    def _now(self, now: float | None) -> float:
        return self._clock() if now is None else float(now)

    # ------------------------------------------------------------- event logic
    def _start_battle(self, line: str, now: float, generation: int | None) -> None:
        fingerprint = " ".join(line.split())
        current = self._battle
        if current is not None:
            if generation is not None and current.generation == generation:
                return
            if generation is None and current.search_fingerprint == fingerprint:
                return
        self._battle = _BattleEpoch(
            generation=generation,
            search_fingerprint=fingerprint,
            search_at=now,
        )
        # A previous battle's true state is deliberately not copied into the
        # epoch.  It is stale evidence until this battle deploys turn one.
        self._forced_recovery = False
        self._verification_warning = False

    def _ready(self, now: float) -> None:
        if self._battle is not None and self._battle.ready_at is None:
            self._battle.ready_at = now

    def _apply_enabled(self, enabled: bool, now: float) -> None:
        self._last_known_on = enabled
        if self._battle is not None:
            self._battle.mode_enabled = enabled
        if enabled:
            # This is direct confirmation that the toggle landed.  Keep the
            # click in flight until deployment (or the confirmation timeout),
            # so a delayed deploy cannot cause an extra toggle.
            if self._request_pending:
                # A human/operator may have toggled the panel in the small
                # window after the timeout was noticed but before the bridge
                # executed its queued click.  Do not execute a second toggle.
                self._request_pending = False
                self._pending_reason = None
            if self._click_in_flight:
                self._click_confirmation_seen = True
                self._verification_warning = False
        else:
            self._forced_recovery = True
            if self._click_in_flight:
                # A toggle can legally turn an already-on button off.  Treat
                # that log line as proof and schedule the corrective click,
                # still respecting the debounce window.
                self._click_in_flight = False
                self._click_started_at = None
                self._next_retry_at = max(
                    self._next_retry_at, now + self.click_debounce
                )

    def _apply_deploy(self, turn: int | None, now: float) -> None:
        if self._battle is None:
            # A watcher can start just after a deploy line.  Preserve the
            # global evidence, but do not invent a battle epoch from it.
            self._last_known_on = True
            return
        self._last_known_on = True
        self._battle.mode_enabled = True
        self._battle.deploy_any = True
        if self._click_in_flight:
            self._click_confirmation_seen = True
        if self._request_pending:
            # A deployment is stronger evidence than a queued timeout
            # request.  The mode is already on; consuming the pending request
            # avoids toggling it back off.
            self._request_pending = False
            self._pending_reason = None
        if turn == 1:
            self._battle.deploy_turn_one = True
            self._forced_recovery = False
            self._verification_warning = False
            self._click_in_flight = False
            self._click_started_at = None
            self._request_pending = False
            self._pending_reason = None
        elif self._click_in_flight:
            # A late deployment proves that full-auto is currently on.  It is
            # too late to recreate turn one, but toggling again would turn it
            # off; stop retrying and retain the audit warning instead.
            self._click_in_flight = False
            self._click_started_at = None
            self._verification_warning = True

    def on_line(self, line: str, now: float | None = None) -> bool:
        """Consume one log line and return whether a click is due."""
        moment = self._now(now)

        if _RESET_RE.search(line):
            self._battle = None
            self._forced_recovery = False
            self._verification_warning = False

        if _SEARCH_REQUEST_RE.search(line):
            turn = _field_int(_TURN_FIELD_RE, line)
            if turn == 1:
                generation = _field_int(_GENERATION_FIELD_RE, line)
                self._start_battle(line, moment, generation)

        if _UI_READY_RE.search(line):
            # The ready marker belongs to the current turn-one search in the
            # real stream.  Ignore ready markers from later replans.
            turn = _field_int(_TURN_FIELD_RE, line)
            if turn in (None, 1):
                self._ready(moment)

        position = _UI_POSITION_RE.search(line)
        if position:
            self.set_overlay_position(
                (float(position.group("x")), float(position.group("y")))
            )

        enabled = _enabled_value(line)
        if enabled is not None:
            self._apply_enabled(enabled, moment)

        deploy = _FULL_AUTO_DEPLOY_RE.search(line)
        if deploy:
            self._apply_deploy(int(deploy.group("turn")), moment)

        return self._due(moment)

    # Friendly aliases for test/bridge callers that prefer event terminology.
    consume = on_line
    handle_line = on_line

    def _can_request(self, now: float) -> bool:
        if self._request_pending or self._click_in_flight:
            return False
        if now < self._next_retry_at:
            return False
        if (
            self._last_click_at is not None
            and now - self._last_click_at < self.click_debounce
        ):
            return False
        return True

    def _request(self, reason: str, now: float) -> bool:
        if not self._can_request(now):
            return False
        self._request_pending = True
        self._pending_reason = reason
        return True

    def _due(self, now: float) -> bool:
        if self._request_pending or self._click_in_flight:
            return False

        if self._forced_recovery and self._last_known_on is not True:
            return self._request("full_auto_disabled", now)

        battle = self._battle
        if battle is None or battle.ready_at is None or battle.deploy_turn_one:
            return False

        if now - battle.ready_at < self.battle_timeout:
            return False

        battle.timeout_reached = True
        # A current true line or a late deployment is evidence that a toggle
        # would be unsafe.  We still expose ``verification_warning`` for the
        # operator, but do not turn a known-on mode off.
        if battle.mode_enabled is True or battle.deploy_any:
            self._verification_warning = True
            return False
        return self._request("battle_turn1_deploy_timeout", now)

    def tick(self, now: float | None = None) -> bool:
        """Run timeout/retry checks when no new log line arrived."""
        moment = self._now(now)
        if self._click_in_flight and self._click_started_at is not None:
            if moment - self._click_started_at >= self.click_confirm_timeout:
                if self._click_confirmation_seen:
                    # The toggle itself was confirmed, so retrying would be a
                    # second toggle.  Leave a warning for a missing deploy.
                    self._click_in_flight = False
                    self._click_started_at = None
                    self._verification_warning = bool(
                        self._battle and not self._battle.deploy_turn_one
                    )
                else:
                    self._click_in_flight = False
                    self._click_started_at = None
                    self._next_retry_at = moment + self.retry_interval
        return self._due(moment)

    poll = tick

    def click_started(self, now: float | None = None) -> None:
        """Record that the caller is executing the requested OS click."""
        moment = self._now(now)
        if not self._request_pending:
            # This is intentionally permissive for a direct operator call,
            # while normal callers always consume a pending request first.
            if not self._can_request(moment):
                return
        self._request_pending = False
        self._pending_reason = None
        self._click_in_flight = True
        self._click_started_at = moment
        self._click_confirmation_seen = False
        self._last_click_at = moment

    mark_click_started = click_started
    record_click = click_started

    def click_failed(self, now: float | None = None) -> None:
        """Allow a retry after an OS/window failure."""
        moment = self._now(now)
        self._request_pending = False
        self._pending_reason = None
        self._click_in_flight = False
        self._click_started_at = None
        self._click_confirmation_seen = False
        self._next_retry_at = moment + self.retry_interval

    # ------------------------------------------------------------ source tailer


class LogTail:
    """Tail the newest log and report source changes to the keeper.

    The first source is positioned at EOF so starting the keeper does not
    replay an old completed run.  A later file (rotation/restart) is read from
    byte zero, and a truncation of the active file is treated the same way.
    The previous file is drained before the new file when possible, preventing
    a rotation boundary from dropping lines already written before the switch.
    ``source_change_index`` identifies the first line from the new source so
    callers can reset their state between the two streams.
    """

    def __init__(self, path: Path, *, start_at_end: bool = True) -> None:
        self.path = Path(path)
        self.start_at_end = start_at_end
        self.offsets: dict[str, int] = {}
        self._partial: dict[str, str] = {}
        self._active: str | None = None
        self._started = False
        self.source_changed = False
        self.source_change_index: int | None = None
        self.epoch = 0

    def _logs(self) -> list[Path]:
        try:
            return sorted(
                self.path.glob("*.log"),
                # ``st_mtime`` alone has coarse resolution on some Windows
                # filesystems.  Creation/change time and the name make a
                # just-created rotation deterministic in that case.
                key=lambda item: (
                    item.stat().st_mtime_ns,
                    item.stat().st_ctime_ns,
                    item.name,
                ),
            )
        except (OSError, ValueError):
            return []

    def _read_one(self, log: Path, *, start: int | None = None) -> list[str]:
        key = str(log)
        try:
            size = log.stat().st_size
            offset = self.offsets.get(key, 0) if start is None else start
            if size < offset:
                offset = 0
                self._partial.pop(key, None)
            if size == offset:
                return []
            with log.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(offset)
                text = self._partial.pop(key, "") + handle.read()
                self.offsets[key] = handle.tell()
        except (OSError, ValueError):
            return []

        # Preserve an unterminated line for the next poll.  The mod normally
        # writes newlines, but this avoids dropping a boundary event on a
        # process crash/rotation.
        if text and not text.endswith(("\n", "\r")):
            chunks = text.splitlines(keepends=True)
            self._partial[key] = chunks.pop() if chunks else text
        else:
            chunks = text.splitlines(keepends=True)

        return [
            chunk.rstrip("\r\n")
            for chunk in chunks
            if "CombatSolver" in chunk
        ]

    def poll(self) -> list[str]:
        self.source_changed = False
        self.source_change_index = None
        logs = self._logs()
        if not logs:
            return []
        newest = logs[-1]
        newest_key = str(newest)

        if not self._started:
            self._started = True
            self._active = newest_key
            try:
                initial_offset = newest.stat().st_size if self.start_at_end else 0
            except OSError:
                initial_offset = 0
            self.offsets[newest_key] = initial_offset
            if self.start_at_end:
                return []
            return self._read_one(newest)

        previous_key = self._active
        switched = previous_key != newest_key
        truncated = False
        if not switched and previous_key is not None:
            try:
                truncated = newest.stat().st_size < self.offsets.get(previous_key, 0)
            except OSError:
                truncated = False

        lines: list[str] = []
        if switched or truncated:
            self.source_changed = True
            self.epoch += 1
            change_index = 0
            if switched and previous_key is not None:
                # Drain the old file first when it still exists.  Its path is
                # intentionally kept as an opaque string; no broad glob is
                # used for a destructive operation.
                previous = Path(previous_key)
                if previous.exists():
                    lines.extend(self._read_one(previous))
                    change_index = len(lines)
            self._partial.pop(newest_key, None)
            self.offsets[newest_key] = 0
            self._active = newest_key
            self.source_change_index = change_index

        lines.extend(self._read_one(newest))
        return lines


def tail_lines(path: Path, offsets: dict[str, int]) -> Iterator[str]:
    """Yield new CombatSolver lines from the newest game log (legacy API).

    Existing callers pass a plain offsets dictionary.  Preserve its original
    first-sight-at-EOF behavior while handling truncation safely; the live
    ``main`` loop uses :class:`LogTail` so it can reset the state machine when
    the source file changes.
    """
    try:
        logs = sorted(path.glob("*.log"), key=lambda item: item.stat().st_mtime)
    except (OSError, ValueError):
        return
    if not logs:
        return
    newest = logs[-1]
    key = newest.name
    try:
        size = newest.stat().st_size
        if key not in offsets:
            offsets[key] = size
            return
        offset = offsets[key]
        if size < offset:
            offset = 0
        if size == offset:
            offsets[key] = offset
            return
        with newest.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            text = handle.read()
            offsets[key] = handle.tell()
    except (OSError, ValueError):
        return
    for line in text.splitlines():
        if "CombatSolver" in line:
            yield line


def _build_click_script(overlay_position: tuple[float, float]) -> str:
    """Build the guarded click script for the current panel origin.

    ``GetClientRect`` is in client coordinates.  The explicit
    ``ClientToScreen`` call below is therefore mandatory; passing the local
    point directly to ``SetCursorPos`` clicks at the wrong place whenever the
    game window is not at the top-left of the desktop.
    """
    position = _valid_overlay_position(overlay_position)
    if position is None:
        raise ValueError("overlay position is unavailable or invalid")
    # The original calibration used the default persisted origin.  A dragged
    # panel moves the button by this delta in the game's viewport.
    delta_x = position[0] - CALIBRATED_OVERLAY_POSITION[0]
    delta_y = position[1] - CALIBRATED_OVERLAY_POSITION[1]
    dx_literal = format(delta_x, ".6f")
    dy_literal = format(delta_y, ".6f")
    return f'''
Add-Type -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
[DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, uint d, UIntPtr e);
[DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern IntPtr FindWindowW(string cls, string title);
[DllImport("user32.dll")] public static extern bool GetClientRect(IntPtr h, out RECT r);
[DllImport("user32.dll")] public static extern bool ClientToScreen(IntPtr h, ref POINT p);
public struct RECT {{ public int Left; public int Top; public int Right; public int Bottom; }}
public struct POINT {{ public int X; public int Y; }}
'@ -Name U -Namespace W
# PowerShell binds $null to an empty string, not a NULL pointer; an
# empty class name makes FindWindowW return ERROR_INVALID_NAME forever.
$h = [W.U]::FindWindowW([NullString]::Value, "{GAME_WINDOW_TITLE}")
if ($h -eq [IntPtr]::Zero) {{ Write-Output "window-not-found"; exit 1 }}
$r = New-Object "W.U+RECT"
[W.U]::GetClientRect($h, [ref]$r) | Out-Null
$w = $r.Right - $r.Left
$hh = $r.Bottom - $r.Top
if ($w -le 0 -or $hh -le 0) {{ Write-Output "invalid-client-rect"; exit 1 }}
$localX = [int]([math]::Round($r.Left + ({BUTTON_RATIO[0]} * $w) + ({dx_literal})))
$localY = [int]([math]::Round($r.Top + ({BUTTON_RATIO[1]} * $hh) + ({dy_literal})))
if ($localX -lt $r.Left -or $localX -gt $r.Right -or $localY -lt $r.Top -or $localY -gt $r.Bottom) {{
    Write-Output "target-out-of-client"
    exit 1
}}
$point = New-Object "W.U+POINT"
$point.X = $localX
$point.Y = $localY
if (![W.U]::ClientToScreen($h, [ref]$point)) {{ Write-Output "client-to-screen-failed"; exit 1 }}
$x = $point.X
$y = $point.Y
if (![W.U]::SetCursorPos($x, $y)) {{ Write-Output "cursor-position-failed"; exit 1 }}
Start-Sleep -Milliseconds 120
[W.U]::mouse_event(2,0,0,0,[UIntPtr]::Zero)
Start-Sleep -Milliseconds 60
[W.U]::mouse_event(4,0,0,0,[UIntPtr]::Zero)
Write-Output "clicked screen $x,$y local $localX,$localY client $w x $hh overlay {position[0]},{position[1]}"
'''


_CLICK_SCRIPT = _build_click_script(CALIBRATED_OVERLAY_POSITION)


def _click_via_power_shell(
    overlay_position: tuple[float, float] | None = None,
) -> str:
    if overlay_position is None:
        # Never guess where a draggable/persisted overlay is.  The caller can
        # retry once the settings file or UI_POSITION_LOADED marker is seen.
        raise RuntimeError("overlay position unavailable; refusing full-auto click")
    try:
        script = _build_click_script(overlay_position)
    except ValueError as exc:
        raise RuntimeError(f"invalid overlay position: {exc}") from exc
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=15,
    )
    out = (result.stdout or "").strip()
    for marker, message in (
        ("window-not-found", "game window not found for full-auto click"),
        ("invalid-client-rect", "game client rect unavailable for full-auto click"),
        ("target-out-of-client", "full-auto target is outside the game client"),
        ("client-to-screen-failed", "could not convert full-auto target to screen coordinates"),
        ("cursor-position-failed", "could not move cursor to full-auto target"),
    ):
        if marker in out:
            raise RuntimeError(message)
    if "clicked" not in out:
        raise RuntimeError(f"click failed: {out} {result.stderr[:200]}")
    return out


def _offset_overlay_position(
    position: tuple[float, float], step: int
) -> tuple[float, float]:
    """Shift the overlay click target by the step-th vertical delta."""
    dy = CLICK_Y_OFFSET_STEPS[step % len(CLICK_Y_OFFSET_STEPS)]
    if dy == 0:
        return position
    return (position[0], position[1] + dy)


def _attempt_click(
    keeper: FullAutoKeeper,
    reason: str | None,
    line: str | None = None,
) -> None:
    detail = f": {line[:110]}" if line else ""
    print(
        f"[keeper] full-auto recovery ({reason or 'requested'}){detail}",
        flush=True,
    )
    keeper.click_started()
    try:
        # Calling without a position intentionally fails closed.  The
        # settings/log path normally supplies one before this point.
        if keeper.overlay_position is None:
            out = _click_via_power_shell()
        else:
            if keeper.click_confirmation_seen:
                keeper._click_offset_step = 0
            position = _offset_overlay_position(
                keeper.overlay_position, keeper._click_offset_step
            )
            keeper._click_offset_step += 1
            out = _click_via_power_shell(position)
        print(f"[keeper] {out}", flush=True)
    except RuntimeError as exc:
        keeper.click_failed()
        print(f"[keeper] {exc}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log-dir",
        default=None,
        help="game log dir (default: %%APPDATA%%\\SlayTheSpire2\\logs)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print decisions without clicking"
    )
    parser.add_argument(
        "--battle-timeout",
        type=float,
        default=BATTLE_CONFIRM_TIMEOUT,
        help="seconds after UI ready before a missing turn-one deploy is recovered",
    )
    parser.add_argument(
        "--poll", type=float, default=1.0, help="log poll interval in seconds"
    )
    args = parser.parse_args(argv)

    log_dir = (
        Path(args.log_dir)
        if args.log_dir
        else Path.home() / "AppData" / "Roaming" / "SlayTheSpire2" / "logs"
    )
    print(f"[keeper] watching {log_dir} — full-auto stays on", flush=True)
    tail = LogTail(log_dir)
    settings_position = read_overlay_position_from_settings()
    if settings_position is None:
        print(
            "[keeper] overlay position unavailable; clicks will fail closed until UI_POSITION_LOADED",
            flush=True,
        )
    else:
        print(f"[keeper] overlay position from settings: {settings_position}", flush=True)
    keeper = FullAutoKeeper(
        battle_timeout=args.battle_timeout,
        overlay_position=settings_position,
    )
    while True:
        lines = tail.poll()
        if tail.source_changed and tail.source_change_index in (None, 0):
            keeper.reset_for_log()
            print("[keeper] log source changed; battle/mode state reset", flush=True)
        boundary = tail.source_change_index
        for index, line in enumerate(lines):
            if tail.source_changed and boundary is not None and index == boundary:
                keeper.reset_for_log()
                print("[keeper] log source changed; battle/mode state reset", flush=True)
            if keeper.on_line(line):
                if args.dry_run:
                    print(
                        f"[keeper] would recover full-auto ({keeper.pending_reason}): {line[:110]}",
                        flush=True,
                    )
                    keeper.click_started()
                else:
                    _attempt_click(keeper, keeper.pending_reason, line)
        if keeper.tick():
            if args.dry_run:
                print(f"[keeper] would recover full-auto ({keeper.pending_reason})", flush=True)
                keeper.click_started()
            else:
                _attempt_click(keeper, keeper.pending_reason)
        time.sleep(max(0.05, args.poll))


if __name__ == "__main__":
    raise SystemExit(main())
