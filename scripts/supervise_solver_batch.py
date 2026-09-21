"""Coordinate one fresh Combat Solver automation batch.

This is intentionally a small process supervisor rather than another batch
runner.  The comparison runner remains the source of truth for battle
records; :mod:`bridge.autoplay` owns out-of-combat actions; and
:mod:`bridge.fullauto_keeper` owns the full-auto watchdog.  This module only
coordinates their lifetimes and records enough provenance to explain an
interrupted run.

The supervisor has a deliberately strict recovery policy: every invocation
gets a new ``ssb-*`` batch directory and no child is ever started with
``--resume``.  In particular, a directory or ID from the contaminated
``phase1-50`` run cannot be adopted accidentally.  A crashed supervisor can
therefore be retried with a new ID without relabelling old comparison
evidence.  A caller may explicitly point fixed mode at an existing seed
ledger to reconcile one active start reservation, but that is not comparison
batch resume and does not restore old battle records.

No game is started by this module.  Before starting any child it performs a
read-only GET health probe and waits for the configured bounded interval.  A
missing game produces an explicit ``game_wait_timeout`` result and no child
processes.

Examples::

    python scripts/supervise_solver_batch.py --mode observational \
        --allow-actions --max-battles 50
    python scripts/supervise_solver_batch.py --mode fixed --dry-run
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.error import URLError
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bridge.trace_controller import VersionLock  # noqa: E402

DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "runs" / "solver_supervisor"
DEFAULT_AUTOPLAY_LOCK = PROJECT_ROOT / "config" / "combat_solver.lock.json"
DEFAULT_GAME_LOG_DIR = Path.home() / "AppData" / "Roaming" / "SlayTheSpire2" / "logs"
DEFAULT_SEED_FILE = PROJECT_ROOT / "data" / "combat_solver" / "fixed_battle_seeds.json"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bridge.seed_allocation import (  # noqa: E402
    SeedAllocationError,
    SeedLedger,
    load_seed_allocation,
)

# Exit codes are stable so a shell wrapper can distinguish an unavailable game
# from a child failure without parsing human-readable output.
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_GAME_WAIT_TIMEOUT = 3
EXIT_GAME_LOST = 4
EXIT_CHILD_FAILED = 5
EXIT_ALREADY_RUNNING = 6
EXIT_BATCH_EXISTS = 7
EXIT_STOPPED = 130
EXIT_RUN_TIMEOUT = 8
EXIT_PARTIAL = 9
EXIT_PREFLIGHT_FAILED = 10
EXIT_CLIENT_WEDGED = 11

# Mirrors ``bridge.autoplay.EXIT_CLASSIFIED_STOP``: the autoplay child ended
# itself on purpose for a recorded reason (stale state, bridge unavailable).
# This is a deliberate stop, not a component crash, and the batch manifest
# records the child's own summary with the concrete reason.
AUTOPLAY_CLASSIFIED_STOP_EXIT = 3

_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,80}$")
_CONTAMINATED_ID_RE = re.compile(r"(?:^|[-_.])phase1(?:[-_.]50)?(?:$|[-_.])", re.IGNORECASE)


class SupervisorError(RuntimeError):
    """Base class for supervisor setup and lifecycle errors."""


class SupervisorAlreadyRunning(SupervisorError):
    """Another supervisor owns the active lock."""


class BatchAlreadyExists(SupervisorError):
    """A requested batch directory already exists and cannot be reused."""


class SupervisorConfigurationError(SupervisorError):
    """The requested supervisor configuration is unsafe or incomplete."""


class ChildStartError(SupervisorError):
    """A child could not be started."""


def _attest_mods() -> dict[str, Any]:
    """Measure the locked mod binaries; an unavailable measurement is recorded, never assumed clean.

    A batch that cannot state which mod bytes it ran against is not attested, so
    the error text becomes the evidence instead of a default to pass on.
    """
    try:
        from combat_solver.modpin import attest_lock_mods
    except Exception as exc:  # pragma: no cover - environment dependent
        return {"error": f"attestation module unavailable: {type(exc).__name__}: {exc}"}
    try:
        return attest_lock_mods(DEFAULT_AUTOPLAY_LOCK).to_json()
    except Exception as exc:
        return {"error": f"attestation failed: {type(exc).__name__}: {exc}"}


def _moved_mods(start: Mapping[str, Any] | None, end: Mapping[str, Any] | None) -> list[str]:
    """Mod ids whose bytes changed between the two in-batch measurements.

    An absent or errored measurement yields no verdict, not an empty one that
    would read as "nothing moved"; ``attested`` carries that distinction.
    """
    if not start or not end or "error" in start or "error" in end:
        return []
    before = {r.get("mod_id"): r.get("actual_sha256") for r in start.get("records", [])}
    after = {r.get("mod_id"): r.get("actual_sha256") for r in end.get("records", [])}
    return sorted(mod for mod in set(before) | set(after) if before.get(mod) != after.get(mod))


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write a JSON artifact without leaving a partly written manifest."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _windows_pid_is_alive(pid: int, kernel32: Any | None = None) -> bool:
    """Query a Windows PID without sending a signal to it.

    ``os.kill(pid, 0)`` is not a harmless existence check on Windows:
    CPython maps non-console signals to ``TerminateProcess``.  Use a limited
    query handle and ``GetExitCodeProcess`` instead.  A query/access failure
    is treated conservatively as alive, so an active lock is never reclaimed
    merely because it is protected by a stricter security policy.

    ``kernel32`` is injectable for unit tests; tests therefore never need to
    open or terminate a real process.
    """

    if pid <= 0:
        return False
    import ctypes

    process_query_limited_information = 0x1000
    error_invalid_parameter = 87
    error_not_found = 1168
    still_active = 259
    if kernel32 is None:
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            # This path is only expected on Windows; fail closed if a caller
            # explicitly asks for the Windows implementation elsewhere.
            return True
        try:
            kernel32 = loader("kernel32", use_last_error=True)
        except OSError:
            return True

    # A WinDLL function defaults to c_int.  HANDLE is pointer-sized on 64-bit
    # Windows, so leave no room for a truncated OpenProcess result.
    try:
        from ctypes import wintypes

        try:
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.OpenProcess.restype = wintypes.HANDLE
        except (AttributeError, TypeError):
            pass
        try:
            kernel32.GetExitCodeProcess.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.DWORD),
            ]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        except (AttributeError, TypeError):
            pass
        try:
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
        except (AttributeError, TypeError):
            pass
    except ImportError:  # pragma: no cover - ctypes.wintypes is standard
        return True

    try:
        handle = kernel32.OpenProcess(
            process_query_limited_information, False, int(pid)
        )
    except (OSError, AttributeError, TypeError):
        return True
    if not handle:
        error = int(ctypes.get_last_error())
        return error not in {error_invalid_parameter, error_not_found}

    try:
        exit_code = wintypes.DWORD()
        try:
            success = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        except (OSError, AttributeError, TypeError):
            return True
        if not success:
            return True
        return int(exit_code.value) == still_active
    finally:
        try:
            kernel32.CloseHandle(handle)
        except (OSError, AttributeError, TypeError):
            pass


def _pid_is_alive(pid: int) -> bool:
    """Return whether *pid* exists without a destructive Windows call."""

    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_is_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        # Windows can report ESRCH instead of ProcessLookupError.  EPERM means
        # the process exists but is not inspectable by this user.
        if exc.errno in (errno.ESRCH, errno.ENOENT):
            return False
        if exc.errno == errno.EPERM:
            return True
        return False
    return True


def validate_batch_id(batch_id: str) -> str:
    """Validate a new ID and reject known contaminated phase-1-50 names."""

    if not isinstance(batch_id, str) or not _BATCH_ID_RE.fullmatch(batch_id):
        raise SupervisorConfigurationError(
            "batch-id must contain 3-81 ASCII letters, digits, '.', '_' or '-'; "
            "it must start with a letter or digit"
        )
    if _CONTAMINATED_ID_RE.search(batch_id):
        raise SupervisorConfigurationError(
            f"refusing contaminated/resumable batch id {batch_id!r}; "
            "start a fresh independent batch"
        )
    return batch_id


def new_batch_id(now: datetime | None = None) -> str:
    """Create a collision-resistant supervisor ID with no resume semantics."""

    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return validate_batch_id(f"ssb-{stamp}-{secrets.token_hex(4)}")


@dataclass(frozen=True)
class SupervisorConfig:
    """Configuration for one fresh supervisor invocation."""

    batch_id: str
    mode: str
    seed_file: Path | None = None
    seed_ledger: Path | None = None
    output_root: Path = DEFAULT_OUTPUT_ROOT
    #: Dropping a file here asks a running batch to end itself at its next poll.
    #: A signal is not usable on Windows for a detached child, and killing the
    #: supervisor outright leaves its play child running behind it -- which is
    #: exactly what must not happen when the reason to stop is "the save is
    #: finally empty, start the next batch clean".
    stop_file: Path | None = None
    base_url: str = "http://127.0.0.1:15526"
    health_path: str = "/"
    game_log_dir: Path = DEFAULT_GAME_LOG_DIR
    max_battles: int = 50
    max_seconds: int | None = None
    max_runs: int = 30
    max_actions: int = 3000
    poll_seconds: float = 0.5
    game_wait_seconds: float = 60.0
    game_loss_grace_seconds: float = 5.0
    #: The client's own log is a diagnostic file, not a stream: a healthy session
    #: wrote 9.2 MB across a whole day of batches.  A rate this high means the
    #: game loop is pouring the same failing call every frame, which starves the
    #: bridge and fills the disk unattended (see ``_sample_client_log``).
    game_log_growth_limit_bytes_per_second: float = 5_000_000.0
    game_log_wedge_samples: int = 4
    graceful_timeout_seconds: float = 10.0
    run_timeout_seconds: float | None = None
    allow_actions: bool = False
    dry_run: bool = False
    comparison_batch_id: str | None = None
    #: ``comparison`` pins every mod because the experiment varies the solver.
    #: ``acceptance`` plays a real run and only needs to name the executor, so
    #: solver drift is recorded rather than fatal -- the contract README already
    #: promises. Both tracks still stop on a missing or unreadable mod.
    track: str = "comparison"
    autoplay_lock: Path = DEFAULT_AUTOPLAY_LOCK

    @property
    def mod_gate(self) -> str:
        return "attest" if self.track == "acceptance" else "strict"

    @property
    def resolved_stop_file(self) -> Path:
        """Where to touch to ask a running batch to stop itself."""
        return (
            Path(self.stop_file)
            if self.stop_file is not None
            else Path(self.output_root) / ".stop-requested"
        )

    def __post_init__(self) -> None:
        validate_batch_id(self.batch_id)
        if self.track not in {"comparison", "acceptance"}:
            raise SupervisorConfigurationError(
                f"unknown track {self.track!r}; expected comparison or acceptance"
            )
        if self.mode not in {"fixed", "observational"}:
            raise SupervisorConfigurationError(
                "mode must be explicitly 'fixed' or 'observational'"
            )
        if self.mode == "fixed":
            seed_file = (
                Path(self.seed_file).expanduser().resolve()
                if self.seed_file is not None
                else DEFAULT_SEED_FILE.resolve()
            )
            try:
                load_seed_allocation(seed_file)
            except SeedAllocationError as exc:
                raise SupervisorConfigurationError(str(exc)) from exc
            object.__setattr__(self, "seed_file", seed_file)
            ledger = (
                Path(self.seed_ledger).expanduser().resolve()
                if self.seed_ledger is not None
                else (
                    Path(self.output_root).expanduser().resolve()
                    / self.batch_id
                    / "seed_allocation.ledger.json"
                )
            )
            object.__setattr__(self, "seed_ledger", ledger)
        elif self.seed_file is not None or self.seed_ledger is not None:
            raise SupervisorConfigurationError(
                "seed allocation injection is disabled in observational mode"
            )
        if self.comparison_batch_id is not None:
            validate_batch_id(self.comparison_batch_id)
            if self.comparison_batch_id == self.batch_id:
                raise SupervisorConfigurationError(
                    "comparison batch must have a distinct independent ID"
                )
        # The derived comparison directory is also an ID.  Keep it within the
        # same bounds even when callers provide a long custom supervisor ID.
        validate_batch_id(self.comparison_batch_id or f"{self.batch_id}-comparison")
        for name in ("max_battles", "max_runs", "max_actions"):
            if int(getattr(self, name)) <= 0:
                raise SupervisorConfigurationError(f"{name} must be positive")
        for name in (
            "poll_seconds",
            "game_wait_seconds",
            "game_loss_grace_seconds",
            "graceful_timeout_seconds",
        ):
            if float(getattr(self, name)) < 0:
                raise SupervisorConfigurationError(f"{name} cannot be negative")
        if self.max_seconds is not None and self.max_seconds <= 0:
            raise SupervisorConfigurationError("max_seconds must be positive")
        if self.run_timeout_seconds is not None and self.run_timeout_seconds <= 0:
            raise SupervisorConfigurationError("run_timeout_seconds must be positive")
        if not self.base_url.strip():
            raise SupervisorConfigurationError("base_url cannot be empty")
        if not self.health_path.startswith("/"):
            raise SupervisorConfigurationError("health_path must start with '/'")
        if not self.dry_run and not self.allow_actions:
            raise SupervisorConfigurationError(
                "live supervision requires --allow-actions; use --dry-run for a plan"
            )

    @property
    def resolved_comparison_batch_id(self) -> str:
        return self.comparison_batch_id or f"{self.batch_id}-comparison"

    @property
    def resolved_seed_file(self) -> Path | None:
        return self.seed_file

    @property
    def resolved_seed_ledger(self) -> Path | None:
        return self.seed_ledger


def _health_url(base_url: str, health_path: str) -> str:
    return base_url.rstrip("/") + "/" + health_path.lstrip("/")


def probe_game(config: SupervisorConfig, timeout: float = 1.0) -> bool:
    """Read-only bridge health probe used before and during supervision."""

    request = Request(_health_url(config.base_url, config.health_path), method="GET")
    try:
        with urlopen(request, timeout=max(0.05, timeout)) as response:
            status = getattr(response, "status", 200)
            return int(status) == 200
    except (OSError, URLError, TimeoutError, ValueError):
        return False


def _display_command(command: Sequence[str]) -> list[str]:
    return [str(part) for part in command]


def assessor_artifact_paths(batch_dir: Path, comparison_dir: Path) -> dict[str, str]:
    """Return the complete path map consumed by the whole-run assessor.

    Keep this manifest independent of whether children have started yet: a
    supervisor status/manifest written during setup must still tell an
    assessor where the companion trace and durable comparison evidence will
    be found.  ``record_checkpoint`` is the public singular name while the
    runner's file is historically named ``records_checkpoint.json``.
    """

    comparison_dir = Path(comparison_dir)
    batch_dir = Path(batch_dir)
    checkpoint = comparison_dir / "records_checkpoint.json"
    trace = str(batch_dir / "autoplay_trace.jsonl")
    battles = str(comparison_dir / "battles.jsonl")
    comparison_manifest = str(comparison_dir / "manifest.json")
    comparison_summary = str(comparison_dir / "summary.json")
    return {
        "trace": trace,
        "autoplay_trace": trace,
        "comparison_dir": str(comparison_dir),
        "battles": battles,
        "comparison_battles": battles,
        "record_checkpoint": str(checkpoint),
        "records_checkpoint": str(checkpoint),
        "comparison_records_checkpoint": str(checkpoint),
        "comparison_manifest": comparison_manifest,
        "comparison_summary": comparison_summary,
        "manifest": comparison_manifest,
        "summary": comparison_summary,
    }


def build_component_commands(
    config: SupervisorConfig, output_dir: Path | None = None
) -> dict[str, list[str]]:
    """Build child commands without starting anything.

    The comparison ID is deliberately separate from the supervisor ID.  No
    command includes ``--resume`` and no command points at ``phase1-50``.
    """

    target_dir = output_dir or (config.output_root / config.batch_id)
    comparison_id = config.resolved_comparison_batch_id
    if _CONTAMINATED_ID_RE.search(comparison_id):
        raise SupervisorConfigurationError(
            "derived comparison batch id would adopt contaminated phase1-50 data"
        )
    python = str(Path(sys.executable).resolve())
    comparison = [
        python,
        str(PROJECT_ROOT / "scripts" / "run_solver_comparison.py"),
        "--batch-id",
        comparison_id,
        "--seed-mode",
        config.mode,
        "--base-url",
        config.base_url,
        "--max-battles",
        str(config.max_battles),
        # A deep solver search can block the mod's HTTP listener longer than
        # the default 30s runner grace; share the supervisor's loss grace.
        "--bridge-grace-seconds",
        str(config.game_loss_grace_seconds),
        "--automated",
        "--mod-gate",
        config.mod_gate,
    ]
    if config.max_seconds is not None:
        comparison.extend(["--max-seconds", str(config.max_seconds)])

    autoplay = [
        python,
        "-m",
        "bridge.autoplay",
        "--base-url",
        config.base_url,
        "--max-runs",
        str(config.max_runs),
        "--max-actions",
        str(config.max_actions),
        "--poll",
        str(config.poll_seconds),
        "--allow-actions",
        "--out-of-combat-only",
        "--lock-file",
        str(config.autoplay_lock),
        "--trace",
        str(target_dir / "autoplay_trace.jsonl"),
        "--log-dir",
        "",
    ]
    if config.mode == "fixed":
        # bridge.autoplay is the sole owner of menu POSTs.  Comparison only
        # observes the resulting run and deliberately receives no seed input.
        assert config.resolved_seed_file is not None
        assert config.resolved_seed_ledger is not None
        autoplay.extend(
            [
                "--seed-mode",
                "fixed",
                "--seed-file",
                str(config.resolved_seed_file),
                "--seed-ledger",
                str(config.resolved_seed_ledger),
            ]
        )
    keeper = [
        python,
        "-m",
        "bridge.fullauto_keeper",
        "--poll",
        str(config.poll_seconds),
        "--log-dir",
        str(config.game_log_dir),
    ]
    return {
        "comparison": _display_command(comparison),
        "autoplay": _display_command(autoplay),
        "fullauto_keeper": _display_command(keeper),
    }


def _load_json_object(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "missing"
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return None, f"invalid_json:{type(exc).__name__}"
    if not isinstance(payload, dict):
        return None, "json_not_object"
    return payload, None


def read_trace_session_end(trace_path: Path) -> dict[str, Any] | None:
    """Read the autoplay child's final summary event from its trace.

    ``bridge.autoplay`` appends a ``session_end`` event when the batch ends
    normally or as a classified stop.  The supervisor only reads this file
    (never rewrites a child artifact) so its manifest can carry the concrete
    stop reason; a trace without the event stays ``None`` — an abrupt child
    death must not be upgraded into a summary that never existed.

    The file is decoded line by line from binary content: a process killed
    mid-append can leave a torn final line with truncated UTF-8 bytes, and
    that must degrade to "corrupt tail skipped" instead of losing the last
    complete summary.  When any such line was skipped before the record was
    found, the returned dict carries ``trace_tail_corrupt: true`` so
    consumers can see the record is degraded while the original exit reason
    is preserved verbatim.
    """

    try:
        raw_bytes = Path(trace_path).read_bytes()
    except OSError:
        return None
    corrupt_tail = False
    for line in reversed(raw_bytes.splitlines()):
        try:
            row = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            # A torn append or an otherwise unparseable trailing line; keep
            # scanning earlier complete records instead of failing.
            corrupt_tail = True
            continue
        if isinstance(row, dict) and row.get("event_type") == "session_end":
            raw = row.get("raw")
            if not isinstance(raw, dict):
                return None
            if corrupt_tail:
                raw = dict(raw)
                raw["trace_tail_corrupt"] = True
            return raw
    return None


SOLVER_LOG_GLOB = "godot*.log"
SOLVER_LOG_MAX_HASH_BYTES = 64 * 1024 * 1024


def snapshot_solver_logs(
    log_dir: Path, *, max_hash_bytes: int = SOLVER_LOG_MAX_HASH_BYTES
) -> list[dict[str, Any]]:
    """Read-only content inventory of the saved game/solver logs.

    The manifest of every new batch records which ``godot*.log`` files existed
    at batch start (path, size, mtime, SHA-256) and, at finalization, which
    existed afterwards.  A later assessor can then bind a Combat Solver log to
    this batch by exact hash and window instead of guessing from timestamps.
    Recording the snapshot is binding *input* only; it never attributes a log
    to a run by itself.
    """

    inventory: list[dict[str, Any]] = []
    try:
        entries = sorted(Path(log_dir).glob(SOLVER_LOG_GLOB))
    except OSError:
        return inventory
    for entry in entries:
        if not entry.is_file():
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        row: dict[str, Any] = {
            "path": str(entry),
            "size_bytes": stat.st_size,
            "mtime_utc": datetime.fromtimestamp(
                stat.st_mtime, tz=UTC
            ).isoformat().replace("+00:00", "Z"),
        }
        if stat.st_size <= max_hash_bytes:
            digest = hashlib.sha256()
            try:
                with entry.open("rb") as handle:
                    for block in iter(
                        lambda: handle.read(1024 * 1024), b""
                    ):
                        digest.update(block)
            except OSError as exc:
                row["sha256"] = None
                row["hash_error"] = f"{type(exc).__name__}: {exc}"
            else:
                row["sha256"] = digest.hexdigest().upper()
        else:
            row["sha256"] = None
            row["hash_skipped"] = (
                f"file larger than {max_hash_bytes} bytes; hash refused"
            )
        inventory.append(row)
    return inventory


CLIENT_PROCESS_IMAGE = "SlayTheSpire2.exe"


def live_client_log(log_dir: Path) -> Path | None:
    """The log the running client is writing to, if the directory is readable.

    ``godot.log`` is the live file and the client renames it to a timestamped
    one on its next start, so the timestamped siblings are history.
    """
    live = Path(log_dir) / "godot.log"
    if live.is_file():
        return live
    try:
        entries = [p for p in Path(log_dir).glob(SOLVER_LOG_GLOB) if p.is_file()]
    except OSError:
        return None
    return max(entries, key=lambda p: p.name) if entries else None


def repeated_log_errors(path: Path, *, tail_bytes: int = 262_144) -> list[str]:
    """The ``ERROR:`` lines in the log's last chunk, most frequent first.

    Read from the end only: a wedged client's log runs to gigabytes, and the
    repeating frame is by definition in the tail.
    """
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - tail_bytes))
            chunk = handle.read().decode("utf-8", "replace")
    except OSError:
        return []
    counts = Counter(
        line.strip() for line in chunk.splitlines() if line.startswith("ERROR:")
    )
    return [f"{text} x{n}" for text, n in counts.most_common(3)]


def client_pids(image: str = CLIENT_PROCESS_IMAGE) -> list[int]:
    """Process ids for the client image, via ``tasklist`` (no psutil needed)."""
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    pids: list[int] = []
    for line in (proc.stdout or "").splitlines():
        fields = line.strip().split('","')
        if len(fields) < 2 or fields[0].strip('"').lower() != image.lower():
            continue
        try:
            pids.append(int(fields[1].strip('"')))
        except ValueError:
            continue
    return pids


def freeze_client(image: str = CLIENT_PROCESS_IMAGE) -> dict[str, Any]:
    """Suspend a wedged client instead of killing it.

    A kill can land inside the game's own save write, and the operator's
    boundary on this machine is that saves and profiles are never touched.  A
    suspend stops the runaway loop -- and the disk it is filling -- while
    leaving the process, its window and its files exactly where they were, and
    the user can close or resume it.  The outcome is reported, never swallowed.
    """
    pids = client_pids(image)
    if not pids:
        return {"image": image, "pids": [], "frozen": [], "error": "no such process"}
    try:
        import ctypes
    except ImportError as exc:  # pragma: no cover - non-Windows
        return {"image": image, "pids": pids, "frozen": [], "error": str(exc)}
    frozen: list[int] = []
    errors: dict[str, str] = {}
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    PROCESS_SUSPEND_RESUME = 0x0800
    for pid in pids:
        handle = kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
        if not handle:
            errors[str(pid)] = f"OpenProcess failed: {ctypes.get_last_error()}"
            continue
        try:
            status = ntdll.NtSuspendProcess(int(handle))
        finally:
            kernel32.CloseHandle(handle)
        if status == 0:
            frozen.append(pid)
        else:
            errors[str(pid)] = f"NtSuspendProcess status {status:#x}"
    result: dict[str, Any] = {"image": image, "pids": pids, "frozen": frozen}
    if errors:
        result["errors"] = errors
    return result


def verify_comparison_artifacts(
    comparison_dir: Path,
    expected_batch_id: str,
    target_battles: int,
    expected_mode: str,
) -> dict[str, Any]:
    """Validate the comparison runner's final state before declaring success.

    A zero process exit code only means that the runner shut down cleanly.  A
    ``max_seconds`` or signal stop also returns zero, so completion requires
    both final artifacts, a complete/max-battles state, the requested target,
    fresh non-resumed records, automated provenance, and valid JSONL records.
    The returned observation is retained verbatim in the supervisor manifest.
    """

    summary_path = comparison_dir / "summary.json"
    manifest_path = comparison_dir / "manifest.json"
    battles_path = comparison_dir / "battles.jsonl"
    summary, summary_error = _load_json_object(summary_path)
    manifest, manifest_error = _load_json_object(manifest_path)
    issues: list[str] = []
    if summary_error:
        issues.append(f"summary:{summary_error}")
    if manifest_error:
        issues.append(f"manifest:{manifest_error}")

    summary_batch = summary.get("batch") if isinstance(summary, dict) else None
    summary_verdict = summary.get("verdict") if isinstance(summary, dict) else None
    manifest_verdict = manifest.get("verdict") if isinstance(manifest, dict) else None
    if not isinstance(summary_batch, dict):
        issues.append("summary:missing_batch")
        summary_batch = {}
    if not isinstance(summary_verdict, dict):
        issues.append("summary:missing_verdict")
        summary_verdict = {}
    if not isinstance(manifest_verdict, dict):
        issues.append("manifest:missing_verdict")
        manifest_verdict = {}

    statuses = {
        "summary": summary_batch.get("status"),
        "manifest": manifest.get("status") if manifest else None,
    }
    stopped_reasons = {
        "summary": summary_batch.get("stopped_reason"),
        "summary_verdict": summary_verdict.get("stopped_reason"),
        "manifest_verdict": manifest_verdict.get("stopped_reason"),
    }
    observed_counts = {
        "summary_verdict": summary_verdict.get("n_battles"),
        "manifest_verdict": manifest_verdict.get("n_battles"),
        "new_battles": summary_batch.get("new_battles"),
        # The current runner stores existing_battles in verdict (the batch
        # envelope only carries new_battles), so accept that authoritative
        # location while still requiring it to be zero for a fresh run.
        "existing_battles": summary_batch.get(
            "existing_battles", summary_verdict.get("existing_battles")
        ),
        "manifest_existing_battles": manifest_verdict.get("existing_battles"),
    }

    if manifest and manifest.get("batch_id") != expected_batch_id:
        issues.append("manifest:batch_id_mismatch")
    if summary_batch.get("batch_id") not in (None, expected_batch_id):
        issues.append("summary:batch_id_mismatch")
    if statuses["summary"] != statuses["manifest"]:
        issues.append("status_mismatch")
    if statuses["summary"] != "complete":
        issues.append("status_not_complete")
    if any(reason != "max_battles" for reason in stopped_reasons.values()):
        issues.append("stopped_reason_not_max_battles")
    if any(
        observed_counts[key] != target_battles
        for key in ("summary_verdict", "manifest_verdict")
        if observed_counts[key] is not None
    ):
        issues.append("target_count_mismatch")
    if observed_counts["summary_verdict"] is None or observed_counts["manifest_verdict"] is None:
        issues.append("target_count_missing")
    if observed_counts["new_battles"] != target_battles:
        issues.append("new_battles_mismatch")
    if observed_counts["existing_battles"] != 0:
        issues.append("existing_battles_not_zero")
    if observed_counts["manifest_existing_battles"] != 0:
        issues.append("manifest_existing_battles_not_zero")

    automated_values = {
        "summary_verdict": summary_verdict.get("automated"),
        "manifest": manifest.get("automated") if manifest else None,
        "manifest_verdict": manifest_verdict.get("automated"),
    }
    if any(value is not True for value in automated_values.values()):
        issues.append("automated_provenance_missing")
    seed_modes = {
        "summary_batch": summary_batch.get("seed_mode"),
        "manifest": manifest.get("seed_mode") if manifest else None,
    }
    if any(value != expected_mode for value in seed_modes.values()):
        issues.append("seed_mode_mismatch")

    required_identity = {
        "game_mode": "standard",
        "character": "IRONCLAD",
        "ascension": 10,
        "singleplayer": True,
    }
    identity_payloads = {
        "summary": summary.get("run_identity") if isinstance(summary, dict) else None,
        "manifest": manifest.get("run_identity") if isinstance(manifest, dict) else None,
    }
    identity_verified_values = {
        "summary": summary_verdict.get("run_identity_verified"),
        "manifest": manifest_verdict.get("run_identity_verified"),
    }
    identity_ok: dict[str, bool] = {}
    for authority, payload in identity_payloads.items():
        valid = (
            isinstance(payload, dict)
            and payload.get("schema_version") == 1
            and payload.get("required") == required_identity
            and payload.get("status") == "verified"
            and payload.get("verified") is True
            and isinstance(payload.get("observations"), int)
            and not isinstance(payload.get("observations"), bool)
            and payload.get("observations", 0) > 0
            and payload.get("verified_observations") == payload.get("observations")
            and payload.get("missing_fields") == []
            and payload.get("conflicts") == []
            and payload.get("errors") == []
        )
        identity_ok[authority] = bool(valid)
        if not isinstance(payload, dict):
            issues.append(f"{authority}:run_identity_missing")
        elif not valid:
            issues.append(f"{authority}:run_identity_unverified")
    if any(value is not True for value in identity_verified_values.values()):
        issues.append("run_identity_verdict_missing_or_false")
    elif len(set(identity_verified_values.values())) != 1:
        issues.append("run_identity_verdict_conflict")
    if not all(identity_ok.values()):
        issues.append("run_identity_contract_failed")

    summary_resume = summary_batch.get("resume")
    manifest_resume = manifest.get("resume") if manifest else None
    manifest_resume_requested = (
        manifest_resume.get("requested") if isinstance(manifest_resume, dict) else manifest_resume
    )
    if summary_resume is not False or manifest_resume_requested is not False:
        issues.append("resume_requested")

    integrity_values = {
        "summary": (summary_verdict.get("record_seed_integrity") or {}).get("valid")
        if isinstance(summary_verdict.get("record_seed_integrity"), dict)
        else None,
        "manifest": (manifest_verdict.get("record_seed_integrity") or {}).get("valid")
        if isinstance(manifest_verdict.get("record_seed_integrity"), dict)
        else None,
    }
    if expected_mode == "fixed":
        # Fixed mode makes the registered partition a completion invariant.
        if any(value is not True for value in integrity_values.values()):
            issues.append("record_integrity_invalid")
    elif any(value is None for value in integrity_values.values()):
        # Observational mode may intentionally report false integrity because
        # a live bridge supplied an alphanumeric/non-partition seed.  Preserve
        # that evidence as an acceptance blocker, but do not reject collection
        # completion merely for lacking a fixed-seed claim.
        issues.append("record_integrity_missing")

    fixed_seed_claim_values = {
        "summary_batch": summary_batch.get("fixed_seed_claim"),
        "manifest": manifest.get("fixed_seed_claim") if manifest else None,
        "summary_verdict": summary_verdict.get("fixed_seed_verified"),
        "manifest_verdict": manifest_verdict.get("fixed_seed_verified"),
    }
    if expected_mode == "fixed":
        # These are runner-authored provenance fields.  Do not derive a fixed
        # claim locally from one integrity field: a malformed/partially copied
        # artifact must fail closed, and all authorities must agree.
        if any(value is not True for value in fixed_seed_claim_values.values()):
            issues.append("fixed_seed_claim_missing_or_false")
        elif len(set(fixed_seed_claim_values.values())) != 1:
            issues.append("fixed_seed_claim_conflict")
    elif any(value is True for value in fixed_seed_claim_values.values()):
        # Observational collection can be complete, but it can never carry a
        # fixed-seed or acceptance claim, even if a producer wrote one by
        # mistake.
        issues.append("observational_fixed_seed_claim")
    elif any(value is not None and not isinstance(value, bool) for value in fixed_seed_claim_values.values()):
        issues.append("fixed_seed_claim_invalid")

    row_count = 0
    row_ids: set[str] = set()
    try:
        lines = battles_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
        issues.append("battles:missing")
    except (OSError, UnicodeDecodeError) as exc:
        lines = []
        issues.append(f"battles:unreadable:{type(exc).__name__}")
    for line_number, line in enumerate(lines, start=1):
        try:
            row = json.loads(line)
        except ValueError:
            issues.append(f"battles:invalid_json_line_{line_number}")
            continue
        if not isinstance(row, dict) or not isinstance(row.get("battle_id"), str):
            issues.append(f"battles:invalid_row_{line_number}")
            continue
        row_count += 1
        row_ids.add(row["battle_id"])
    if row_count != target_battles:
        issues.append("battles:target_count_mismatch")
    if len(row_ids) != row_count:
        issues.append("battles:duplicate_ids")

    accepted_values = {
        "summary": summary_verdict.get("accepted"),
        "manifest": manifest_verdict.get("accepted"),
    }
    fixed_seed_claim = expected_mode == "fixed" and all(
        value is True for value in fixed_seed_claim_values.values()
    )
    acceptance_blockers: list[str] = []
    if expected_mode == "observational":
        acceptance_blockers.append("observational_mode")
        if any(value is True for value in fixed_seed_claim_values.values()):
            acceptance_blockers.append("observational_fixed_seed_claim")
    elif not fixed_seed_claim:
        acceptance_blockers.append("fixed_seed_claim_unverified")
    if any(value is not True for value in integrity_values.values()):
        acceptance_blockers.append("record_integrity_invalid")
    if not all(identity_ok.values()) or any(
        value is not True for value in identity_verified_values.values()
    ):
        acceptance_blockers.append("run_identity_unverified")
    if not all(value is True for value in accepted_values.values()):
        acceptance_blockers.append("runner_acceptance_false")

    # A runner status of partial/running is a valid, auditable interruption;
    # malformed or contradictory artifacts are a hard failure.  Neither may
    # be promoted to complete on the strength of process exit code 0.
    is_partial = any(
        status in {"partial", "running", "dry_run"}
        for status in statuses.values()
    ) or any(
        reason not in {None, "max_battles"} for reason in stopped_reasons.values()
    )
    classification = "complete" if not issues else "partial" if is_partial else "failed"
    return {
        "classification": classification,
        "expected_batch_id": expected_batch_id,
        "comparison_dir": str(comparison_dir),
        "artifacts": {
            "summary": str(summary_path),
            "manifest": str(manifest_path),
            "battles": str(battles_path),
            "records_checkpoint": str(comparison_dir / "records_checkpoint.json"),
        },
        "status": statuses,
        "stopped_reason": stopped_reasons,
        "counts": {**observed_counts, "battle_rows": row_count},
        "target_battles": target_battles,
        "automated": automated_values,
        "seed_mode": seed_modes,
        "resume": {
            "summary": summary_resume,
            "manifest_requested": manifest_resume_requested,
        },
        "record_integrity": integrity_values,
        "run_identity": {
            "summary": identity_payloads["summary"],
            "manifest": identity_payloads["manifest"],
            "verdict": identity_verified_values,
            "valid": all(identity_ok.values())
            and all(value is True for value in identity_verified_values.values()),
        },
        "fixed_seed_claim_values": fixed_seed_claim_values,
        "fixed_seed_claim": fixed_seed_claim,
        "acceptance_claim": expected_mode == "fixed"
        and fixed_seed_claim
        and all(value is True for value in accepted_values.values()),
        "acceptance_blockers": acceptance_blockers,
        "issues": issues,
    }


class BatchLock:
    """Atomic root lock with stale-owner reclamation but no data resumption."""

    def __init__(self, output_root: Path, batch_id: str) -> None:
        self.output_root = output_root
        self.batch_id = batch_id
        self.path = output_root / ".active.lock"
        self.owner_token = uuid.uuid4().hex
        self.held = False

    def _read_existing(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError, UnicodeDecodeError):
            return {}

    def acquire(self, batch_dir: Path) -> None:
        self.output_root.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "pid": os.getpid(),
            "batch_id": self.batch_id,
            "batch_dir": str(batch_dir),
            "owner_token": self.owner_token,
            "started_at_utc": _utc_now(),
            "policy": (
                "fresh_batch_only; never resume comparison records; "
                "explicit seed-ledger reconciliation only"
            ),
        }
        for attempt in range(2):
            try:
                descriptor = os.open(
                    str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
            except FileExistsError:
                existing = self._read_existing()
                pid = existing.get("pid")
                if attempt == 0 and isinstance(pid, int) and not _pid_is_alive(pid):
                    # A dead supervisor cannot protect a running batch.  The
                    # lock is the exact file we just inspected; reclaiming it
                    # never opens or modifies the old batch directory.
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        raise SupervisorAlreadyRunning(
                            f"stale active lock cannot be reclaimed: {self.path}: {exc}"
                        ) from exc
                    continue
                owner = f" pid={pid}" if pid is not None else ""
                old_batch = existing.get("batch_id")
                raise SupervisorAlreadyRunning(
                    f"another solver supervisor is active{owner} batch={old_batch!r}; "
                    f"lock={self.path}"
                )
            else:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                    json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                self.held = True
                return
        raise SupervisorAlreadyRunning(f"could not acquire active lock {self.path}")

    def release(self) -> None:
        if not self.held:
            return
        try:
            existing = self._read_existing()
            if existing.get("owner_token") == self.owner_token:
                self.path.unlink(missing_ok=True)
        finally:
            self.held = False


@dataclass
class ManagedProcess:
    name: str
    command: list[str]
    log_path: Path
    process: Any
    log_handle: Any = None
    pid: int | None = None
    exit_code: int | None = None
    stop_requested: bool = False
    stop_signal: str | None = None


def _process_group_kwargs() -> dict[str, Any]:
    """Create independent process groups so a stop does not leak children."""

    if os.name == "nt":
        flag = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": flag} if flag else {}
    return {"start_new_session": True}


class BatchSupervisor:
    """Run and supervise the three live components for one fresh batch."""

    def __init__(
        self,
        config: SupervisorConfig,
        *,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        game_probe: Callable[..., bool] = probe_game,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        comparison_output_dir: Path | None = None,
    ) -> None:
        self.config = config
        self.popen_factory = popen_factory
        self.game_probe = game_probe
        self.clock = clock
        self.sleep = sleep
        self.batch_dir = config.output_root / config.batch_id
        self.log_dir = self.batch_dir / "logs"
        self.comparison_output_dir = comparison_output_dir or (
            PROJECT_ROOT / "runs" / "combat_solver_compare" / config.resolved_comparison_batch_id
        )
        self.lock = BatchLock(config.output_root, config.batch_id)
        self.children: dict[str, ManagedProcess] = {}
        self.commands: dict[str, list[str]] = {}
        self.exit_codes: dict[str, int | None] = {}
        self.status = "created"
        self.stop_reason: str | None = None
        self.result_code: int | None = None
        self.started_at_utc = _utc_now()
        self.completed_at_utc: str | None = None
        self._prepared = False
        self._supervisor_log: Any = None
        self._stop_requested = False
        self._stop_signal_reason: str | None = None
        self._old_signal_handlers: dict[int, Any] = {}
        self.comparison_result: dict[str, Any] | None = None
        self.autoplay_summary: dict[str, Any] | None = None
        self.solver_logs_at_start: list[dict[str, Any]] | None = None
        self.solver_logs_at_end: list[dict[str, Any]] | None = None
        # Read-only measurement of the mod binaries the batch actually ran
        # against, at both ends of the window (see combat_solver/modpin.py).
        self.mods_at_start: dict[str, Any] | None = None
        self.mods_at_end: dict[str, Any] | None = None
        #: What ``--dry-run`` concluded about running a batch on this machine.
        self.preflight_gates: dict[str, Any] | None = None
        #: (path, size, mono) of the previous client-log sample.
        self._client_log_sample: tuple[Path, int, float] | None = None
        self._client_log_fast_samples = 0
        #: Set when the client was found wedged; names what was done about it.
        self.client_watchdog: dict[str, Any] | None = None
        #: Things that degrade this batch's self-attestation without vetoing it.
        self.attestation_gaps: list[str] = []
        self.seed_allocation = (
            load_seed_allocation(config.resolved_seed_file)
            if config.mode == "fixed" and config.resolved_seed_file is not None
            else None
        )

    @property
    def manifest_path(self) -> Path:
        return self.batch_dir / "manifest.json"

    @property
    def status_path(self) -> Path:
        return self.batch_dir / "status.json"

    @property
    def supervisor_log_path(self) -> Path:
        return self.batch_dir / "supervisor.log"

    def _prepare(self) -> None:
        if self._prepared:
            return
        validate_batch_id(self.config.batch_id)
        self.lock.acquire(self.batch_dir)
        try:
            if self.batch_dir.exists():
                raise BatchAlreadyExists(
                    f"batch directory already exists; choose a new batch-id: {self.batch_dir}"
                )
            self.batch_dir.mkdir(parents=True, exist_ok=False)
            self.log_dir.mkdir(parents=True, exist_ok=False)
            self._supervisor_log = self.supervisor_log_path.open(
                "a", encoding="utf-8", buffering=1, newline="\n"
            )
            self.commands = build_component_commands(self.config, self.batch_dir)
            # Record which Combat Solver/game logs exist before any child
            # starts, so post-batch log bindings have an exact baseline.
            self.solver_logs_at_start = snapshot_solver_logs(self.config.game_log_dir)
            self.mods_at_start = _attest_mods()
            self._prepared = True
            self._log(
                "batch_created",
                batch_id=self.config.batch_id,
                mode=self.config.mode,
                comparison_batch_id=self.config.resolved_comparison_batch_id,
                resume=False,
                recovery_policy=(
                    "new_batch_only; never resume comparison records; "
                    "explicit seed-ledger reconciliation only"
                ),
            )
            self._persist("created")
        except BaseException:
            if self._supervisor_log is not None:
                self._supervisor_log.close()
                self._supervisor_log = None
            if self.lock.held:
                self.lock.release()
            raise

    def _child_snapshot(self) -> dict[str, dict[str, Any]]:
        return {
            name: {
                "pid": child.pid,
                "command": child.command,
                "log": str(child.log_path),
                "exit_code": child.exit_code,
                "stop_requested": child.stop_requested,
                "stop_signal": child.stop_signal,
            }
            for name, child in self.children.items()
        }

    def _seed_allocation_snapshot(self) -> dict[str, Any] | None:
        """Expose allocation/hash/consumption without inventing progress."""

        if self.seed_allocation is None:
            return None
        assert self.config.resolved_seed_ledger is not None
        result: dict[str, Any] = {
            "allocation": self.seed_allocation.describe(),
            "ledger_path": str(self.config.resolved_seed_ledger),
            "status": "not_started",
            "next_index": 0,
            "active": None,
            "consumed": [],
            "exhausted": False,
        }
        if not self.config.resolved_seed_ledger.exists():
            return result
        try:
            ledger = SeedLedger(
                self.seed_allocation,
                self.config.resolved_seed_ledger,
                create=False,
            )
            snapshot = ledger.snapshot()
        except SeedAllocationError as exc:
            # Keep the manifest readable while making a malformed/mismatched
            # ledger explicit.  The autoplay child will fail closed before a
            # random/fresh run can be started.
            result["status"] = "invalid"
            result["error"] = str(exc)
            return result
        result.update(
            {
                "status": "active" if snapshot.get("active") is not None else "ready",
                "next_index": snapshot["next_index"],
                "active": snapshot["active"],
                "consumed": snapshot["consumed"],
                "exhausted": snapshot["exhausted"],
            }
        )
        return result

    def _persist(self, status: str | None = None) -> None:
        if status is not None:
            self.status = status
        assessor_paths = assessor_artifact_paths(
            self.batch_dir, self.comparison_output_dir
        )
        payload = {
            "schema_version": 1,
            "batch_id": self.config.batch_id,
            "comparison_batch_id": self.config.resolved_comparison_batch_id,
            "mode": self.config.mode,
            "track": self.config.track,
            "mod_gate": self.config.mod_gate,
            "seed_allocation": self._seed_allocation_snapshot(),
            "status": self.status,
            "started_at_utc": self.started_at_utc,
            "completed_at_utc": self.completed_at_utc,
            "updated_at_utc": _utc_now(),
            "output_dir": str(self.batch_dir),
            # ``resume`` is retained as a requested flag for compatibility;
            # this supervisor has no comparison-resume implementation.
            "resume": False,
            "resume_supported": False,
            "comparison_resume_supported": False,
            "recovery_policy": (
                "new_batch_only; never resume comparison records; "
                "explicit seed-ledger reconciliation only"
            ),
            "game": {
                "base_url": self.config.base_url,
                "health_path": self.config.health_path,
                "wait_seconds": self.config.game_wait_seconds,
                "loss_grace_seconds": self.config.game_loss_grace_seconds,
            },
            #: Touch this path to ask a running batch to end at its next poll.
            "stop_file": str(self.config.resolved_stop_file),
            "limits": {
                "max_battles": self.config.max_battles,
                "max_seconds": self.config.max_seconds,
                "max_runs": self.config.max_runs,
                "max_actions": self.config.max_actions,
                "poll_seconds": self.config.poll_seconds,
                "run_timeout_seconds": self.config.run_timeout_seconds,
            },
            "commands": self.commands,
            "children": self._child_snapshot(),
            "exit_codes": self.exit_codes,
            "comparison_result": self.comparison_result,
            #: Why this batch's self-attestation is weaker than it looks.  The
            #: acceptance track records an observer that died instead of letting
            #: it veto the run the batch exists to produce.
            "attestation_gaps": list(self.attestation_gaps),
            "client_watchdog": self.client_watchdog,
            # Read-only Combat Solver/game log inventory around the batch
            # window.  Hash-observation semantics only: the fields say which
            # hashes were observed at start / end.  A hash missing from
            # ``at_start`` never implies the file was created in the window
            # (an appended file changes its hash; a start snapshot can be
            # missing or refuse to hash), and no observation attributes a
            # log to a run — that requires content-range and run-identity
            # association inside the log.
            "combat_solver_logs": {
                "log_dir": str(self.config.game_log_dir),
                "glob": SOLVER_LOG_GLOB,
                "at_start": self.solver_logs_at_start,
                "at_end": self.solver_logs_at_end,
                "purpose": (
                    "hash-observation inventories only; hash absence at "
                    "start never infers creation time, and timestamp "
                    "proximity is not evidence"
                ),
            },
            # The autoplay child's own final summary (stop reason, runs,
            # seed-allocation state) when it ended audibly.  Read-only copy:
            # the trace itself stays the authoritative artifact.
            # What ``--dry-run`` concluded, so the refusal an operator sees is the
            # same measurement recorded in the batch.
            "preflight_gates": self.preflight_gates,
            # Read-only measurement of the mod binaries behind this batch, at
            # both ends of the window.  Steam updates Workshop mods on its own,
            # so "we were locked to 0.31.0" is only meaningful if the batch
            # states what was actually loaded.  ``invalidated_by_mod_update``
            # means the bytes moved during the window, which makes every
            # decision in it unverifiable against a single grammar/build.
            "mod_attestation": {
                "lock_file": str(DEFAULT_AUTOPLAY_LOCK),
                "at_start": self.mods_at_start,
                "at_end": self.mods_at_end,
                "drifted_from_lock_at_start": (self.mods_at_start or {}).get(
                    "drifted_from_lock", []
                ),
                "moved_during_batch": _moved_mods(self.mods_at_start, self.mods_at_end),
                "attested": bool(self.mods_at_start)
                and "error" not in self.mods_at_start
                and bool(self.mods_at_end)
                and "error" not in self.mods_at_end,
            },
            "autoplay_summary": self.autoplay_summary,
            # Whole-run assessment is a separate read-only step.  Publish all
            # of its required inputs up front, including both spellings of
            # the checkpoint key used by older consumers.
            "assessor_paths": assessor_paths,
            "assessor_artifacts": dict(assessor_paths),
            "evidence_paths": dict(assessor_paths),
            "assessor": {
                "trace": assessor_paths["trace"],
                "comparison_evidence": {
                    "directory": assessor_paths["comparison_dir"],
                    "battles": assessor_paths["battles"],
                    "record_checkpoint": assessor_paths["record_checkpoint"],
                    "records_checkpoint": assessor_paths["records_checkpoint"],
                    "manifest": assessor_paths["comparison_manifest"],
                    "summary": assessor_paths["comparison_summary"],
                },
            },
            # Once comparison artifacts are checked, expose the identity
            # verdict at supervisor level as well as inside the copied
            # comparison result.  This keeps the supervisor manifest a
            # self-contained audit anchor for the target run.
            "run_identity": (
                self.comparison_result.get("run_identity")
                if isinstance(self.comparison_result, dict)
                else None
            ),
            "stop_reason": self.stop_reason,
            "result_code": self.result_code,
            "dry_run": self.config.dry_run,
            "allow_actions": self.config.allow_actions,
        }
        if self._prepared:
            _atomic_write_json(self.status_path, payload)
            _atomic_write_json(self.manifest_path, payload)

    def _log(self, event: str, **fields: Any) -> None:
        if self._supervisor_log is None:
            return
        row = {"at_utc": _utc_now(), "event": event, **fields}
        self._supervisor_log.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        self._supervisor_log.flush()
        try:
            os.fsync(self._supervisor_log.fileno())
        except OSError:
            pass

    def _probe(self) -> bool:
        try:
            # Accept the documented config argument and a no-argument test
            # probe for convenient deterministic unit tests.
            try:
                return bool(self.game_probe(self.config))
            except TypeError as config_error:
                try:
                    return bool(self.game_probe())
                except TypeError:
                    raise config_error
        except Exception as exc:  # a probe failure is an unavailable game
            self._log("game_probe_error", error=f"{type(exc).__name__}: {exc}")
            return False

    def _wait_for_game(self) -> bool:
        self._persist("waiting_for_game")
        started = self.clock()
        deadline = started + self.config.game_wait_seconds
        attempt = 0
        while True:
            attempt += 1
            if self._probe():
                self._log("game_ready", attempt=attempt)
                self._persist("game_ready")
                return True
            elapsed = max(0.0, self.clock() - started)
            remaining = max(0.0, deadline - self.clock())
            self._log(
                "game_waiting",
                attempt=attempt,
                elapsed_seconds=round(elapsed, 3),
                remaining_seconds=round(remaining, 3),
            )
            if remaining <= 0:
                self.stop_reason = "game_wait_timeout"
                self._log("game_wait_timeout", elapsed_seconds=round(elapsed, 3))
                self._persist("failed")
                return False
            self.sleep(min(max(0.01, self.config.poll_seconds), remaining))

    def _spawn(self, name: str) -> ManagedProcess:
        command = self.commands[name]
        log_path = self.log_dir / f"{name}.log"
        log_handle = log_path.open("a", encoding="utf-8", buffering=1, newline="\n")
        kwargs: dict[str, Any] = {
            "cwd": str(PROJECT_ROOT),
            "stdout": log_handle,
            "stderr": subprocess.STDOUT,
            "text": True,
            "encoding": "utf-8",
        }
        kwargs.update(_process_group_kwargs())
        try:
            process = self.popen_factory(command, **kwargs)
        except (OSError, ValueError) as exc:
            log_handle.close()
            self._log("child_start_failed", component=name, error=f"{type(exc).__name__}: {exc}")
            raise ChildStartError(f"could not start {name}: {exc}") from exc
        pid = getattr(process, "pid", None)
        managed = ManagedProcess(
            name=name,
            command=list(command),
            log_path=log_path,
            process=process,
            log_handle=log_handle,
            pid=int(pid) if isinstance(pid, int) else None,
        )
        self.children[name] = managed
        self.exit_codes[name] = None
        self._log("child_started", component=name, pid=managed.pid, command=command, log=str(log_path))
        self._persist("running")
        return managed

    def _start_children(self) -> None:
        # Keeper starts first so the turn-one watchdog is already observing
        # the first battle.  Comparison is started before autoplay so its
        # journal boundary exists before any action can close a battle.
        for name in ("fullauto_keeper", "comparison", "autoplay"):
            self._spawn(name)

    def _poll_child(self, child: ManagedProcess) -> int | None:
        if child.exit_code is not None:
            return child.exit_code
        try:
            code = child.process.poll()
        except Exception as exc:
            self._log("child_poll_failed", component=child.name, error=f"{type(exc).__name__}: {exc}")
            code = EXIT_CHILD_FAILED
        if code is not None:
            self._record_exit(child, int(code))
        return child.exit_code

    def _record_exit(self, child: ManagedProcess, code: int) -> None:
        if child.exit_code is not None:
            return
        child.exit_code = code
        self.exit_codes[child.name] = code
        self._log(
            "child_exited",
            component=child.name,
            pid=child.pid,
            exit_code=code,
            stop_requested=child.stop_requested,
        )
        self._persist()
        if child.log_handle is not None:
            child.log_handle.close()
            child.log_handle = None

    def _request_stop(self, child: ManagedProcess, reason: str) -> None:
        if child.exit_code is not None or child.stop_requested:
            return
        child.stop_requested = True
        child.stop_signal = reason
        process = child.process
        try:
            if os.name == "nt" and hasattr(signal, "CTRL_BREAK_EVENT"):
                try:
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                except (AttributeError, OSError, ValueError):
                    process.terminate()
            else:
                process.terminate()
            self._log("child_stop_requested", component=child.name, pid=child.pid, reason=reason)
        except (OSError, ValueError) as exc:
            self._log(
                "child_stop_request_failed",
                component=child.name,
                pid=child.pid,
                reason=reason,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _force_kill(self, child: ManagedProcess, reason: str) -> None:
        if child.exit_code is not None:
            return
        try:
            child.process.kill()
            self._log("child_kill", component=child.name, pid=child.pid, reason=reason)
        except (OSError, ValueError) as exc:
            self._log(
                "child_kill_failed",
                component=child.name,
                pid=child.pid,
                reason=reason,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _stop_children(self, reason: str) -> None:
        live = [child for child in self.children.values() if self._poll_child(child) is None]
        if not live:
            return
        self._persist("stopping")
        for child in reversed(live):
            self._request_stop(child, reason)
        deadline = self.clock() + self.config.graceful_timeout_seconds
        while True:
            remaining = [child for child in live if self._poll_child(child) is None]
            if not remaining:
                return
            remaining_seconds = deadline - self.clock()
            if remaining_seconds <= 0:
                for child in remaining:
                    self._force_kill(child, reason)
                # Poll once more so the forced exit code is visible where the
                # fake process/test or OS reports it synchronously.
                for child in remaining:
                    try:
                        code = child.process.poll()
                    except Exception:
                        code = None
                    if code is None:
                        try:
                            code = child.process.wait(timeout=1.0)
                        except Exception:
                            code = None
                    if code is not None:
                        self._record_exit(child, int(code))
                return
            self.sleep(min(0.05, max(0.01, remaining_seconds)))

    def request_stop(self, reason: str = "operator_request") -> None:
        """Request a cooperative stop for callers embedding the supervisor."""

        self._stop_requested = True
        self._stop_signal_reason = reason
        self.stop_reason = reason
        self._log("stop_requested", reason=reason)
        self._stop_children(reason)
        if self._prepared:
            self._persist("stopped")

    def stop(self, reason: str = "operator_request") -> None:
        """Public alias for :meth:`request_stop` used by service wrappers."""

        self.request_stop(reason)

    def _install_signal_handlers(self) -> None:
        if threading_is_main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                try:
                    self._old_signal_handlers[signum] = signal.getsignal(signum)
                    signal.signal(signum, self._handle_signal)
                except (OSError, ValueError):
                    pass

    def _restore_signal_handlers(self) -> None:
        for signum, handler in self._old_signal_handlers.items():
            try:
                signal.signal(signum, handler)
            except (OSError, ValueError):
                pass
        self._old_signal_handlers.clear()

    def _handle_signal(self, signum: int, _frame: Any) -> None:
        self._stop_requested = True
        self._stop_signal_reason = "interrupt" if signum == signal.SIGINT else "signal"
        self._log("stop_requested", reason=self._stop_signal_reason, signal=signum)

    def _verify_comparison(self) -> dict[str, Any]:
        result = verify_comparison_artifacts(
            self.comparison_output_dir,
            self.config.resolved_comparison_batch_id,
            self.config.max_battles,
            self.config.mode,
        )
        self.comparison_result = result
        self._log(
            "comparison_artifacts_checked",
            classification=result["classification"],
            status=result["status"],
            stopped_reason=result["stopped_reason"],
            counts=result["counts"],
            issues=result["issues"],
        )
        self._persist()
        return result

    def _finalize_stop(self) -> dict[str, Any]:
        """Reconcile recorded state after children stopped.

        A stopped comparison child may have been unable to publish its final
        summary (force kill, crash mid-write).  Its on-disk artifacts are
        never rewritten here; instead the supervisor verifies them read-only
        and records the resulting classification — including a residual
        ``running`` status — in the batch manifest, so the layers converge on
        an honest terminal state instead of pretending completion.
        """

        self.autoplay_summary = read_trace_session_end(
            self.batch_dir / "autoplay_trace.jsonl"
        )
        self.solver_logs_at_end = snapshot_solver_logs(self.config.game_log_dir)
        self.mods_at_end = _attest_mods()
        return self._verify_comparison()

    def _monitor(self) -> int:
        started = self.clock()
        game_missing_since: float | None = None
        self._persist("running")
        while True:
            if self._stop_requested:
                self.stop_reason = self._stop_signal_reason or "stop_requested"
                self._stop_children(self.stop_reason)
                self._finalize_stop()
                return EXIT_STOPPED

            if self._stop_file_requested():
                self.stop_reason = "operator_stop_file"
                self._log(
                    "operator_stop_file",
                    stop_file=str(self.config.resolved_stop_file),
                )
                self._stop_children("operator_stop_file")
                self._finalize_stop()
                return EXIT_STOPPED

            states = {
                name: self._poll_child(child) for name, child in self.children.items()
            }
            comparison_code = states.get("comparison")
            if comparison_code is not None:
                if comparison_code == 0:
                    comparison_result = self._verify_comparison()
                    classification = comparison_result["classification"]
                    if classification == "complete":
                        self.stop_reason = "comparison_complete"
                        self._log("comparison_complete", exit_code=comparison_code)
                        self._stop_children("comparison_complete")
                        self._finalize_stop()
                        return EXIT_OK
                    if classification == "partial":
                        self.stop_reason = "comparison_partial"
                        self._log(
                            "comparison_partial",
                            exit_code=comparison_code,
                            observed_stopped_reason=comparison_result["stopped_reason"],
                            issues=comparison_result["issues"],
                        )
                        self._stop_children("comparison_partial")
                        self._finalize_stop()
                        return EXIT_PARTIAL
                    self.stop_reason = "comparison_artifacts_invalid"
                    self._log(
                        "comparison_artifacts_invalid",
                        exit_code=comparison_code,
                        issues=comparison_result["issues"],
                    )
                    self._stop_children("comparison_artifacts_invalid")
                    self._finalize_stop()
                    return EXIT_CHILD_FAILED
                if self.config.track == "acceptance":
                    # The acceptance batch exists to produce the run; this child
                    # only witnesses what the auto-updating solver did while the
                    # run was playing.  Its death costs the batch evidence, and
                    # evidence lost is not a reason to kill the thing being
                    # attested -- the same split the solver-drift gate uses.  If
                    # the client itself went away, ``game_lost`` below says so
                    # under its own name.
                    self.attestation_gaps.append(
                        f"comparison_observer_exited:{comparison_code}"
                    )
                    self.children.pop("comparison", None)
                    self._log(
                        "comparison_observer_lost",
                        exit_code=comparison_code,
                        remaining_children=sorted(self.children),
                    )
                    self._persist("running")
                    continue
                self.stop_reason = "comparison_failed"
                self._log("comparison_failed", exit_code=comparison_code)
                self._stop_children("comparison_failed")
                self._finalize_stop()
                return EXIT_CHILD_FAILED

            unexpected = [
                (name, code)
                for name, code in states.items()
                if name != "comparison" and code is not None
            ]
            if unexpected:
                name, code = unexpected[0]
                if name == "autoplay" and code == 0:
                    # The driver finished its quota cleanly (max runs/actions
                    # or seed exhaustion).  The batch is over; wrap up instead
                    # of labelling a normal completion a child failure.
                    self.stop_reason = "autoplay_completed"
                    self._log("autoplay_completed", exit_code=code)
                elif name == "autoplay" and code == AUTOPLAY_CLASSIFIED_STOP_EXIT:
                    # The driver ended itself for a recorded reason (stale
                    # state, bridge unavailable).  Preserve that reason rather
                    # than reporting an unexpected crash.
                    self.stop_reason = "autoplay_classified_stop"
                    self._log("autoplay_classified_stop", exit_code=code)
                elif code == 0:
                    self.stop_reason = f"{name}_finished"
                    self._log("child_finished", component=name, exit_code=code)
                else:
                    self.stop_reason = f"{name}_exited"
                    self._log("child_unexpected_exit", component=name, exit_code=code)
                self._stop_children(self.stop_reason)
                comparison_result = self._finalize_stop()
                if self.stop_reason == "autoplay_completed":
                    return (
                        EXIT_OK
                        if comparison_result["classification"] == "complete"
                        else EXIT_PARTIAL
                    )
                return EXIT_CHILD_FAILED

            if not self._probe():
                now = self.clock()
                if game_missing_since is None:
                    game_missing_since = now
                    self._log("game_lost", grace_seconds=self.config.game_loss_grace_seconds)
                if now - game_missing_since >= self.config.game_loss_grace_seconds:
                    self.stop_reason = "game_lost_timeout"
                    self._log(
                        "game_lost_timeout",
                        missing_seconds=round(now - game_missing_since, 3),
                    )
                    self._stop_children("game_lost_timeout")
                    self._finalize_stop()
                    return EXIT_GAME_LOST
            else:
                if game_missing_since is not None:
                    self._log("game_restored", missing_seconds=round(self.clock() - game_missing_since, 3))
                game_missing_since = None

            wedge = self._sample_client_log()
            if wedge is not None:
                self.client_watchdog = wedge
                self.stop_reason = "client_wedged"
                self._log("client_wedged", **wedge)
                self._stop_children("client_wedged")
                self._finalize_stop()
                return EXIT_CLIENT_WEDGED

            elapsed = self.clock() - started
            if self.config.run_timeout_seconds is not None and elapsed >= self.config.run_timeout_seconds:
                self.stop_reason = "supervisor_timeout"
                self._log("supervisor_timeout", elapsed_seconds=round(elapsed, 3))
                self._stop_children("supervisor_timeout")
                self._finalize_stop()
                return EXIT_RUN_TIMEOUT
            self.sleep(max(0.01, self.config.poll_seconds))

    def _stop_file_requested(self) -> bool:
        """True once, on a stop request written since the last poll.

        The file is consumed rather than left in place: it asks *this* batch to
        stop, and a stale marker would stop every batch after it forever.
        """
        path = self.config.resolved_stop_file
        if not path.exists():
            return False
        try:
            path.unlink()
        except OSError as exc:
            self._log("stop_file_unlink_failed", stop_file=str(path), error=str(exc))
        return True

    def _sample_client_log(self) -> dict[str, Any] | None:
        """Report a wedged client, and freeze it, from its own log's growth.

        Observed on the real client 2026-09-21: travelling to an event node put
        the game loop into a per-frame failed VFX instantiation
        (``PunchOff.PunchEachOther`` -> ``NHitSparkVfx.Create`` ->
        ``Parameter "particles" is null``).  The client wrote 2.3 GB to
        ``godot.log`` in about two minutes, stopped serving the bridge inside ten
        seconds, and would have gone on filling the disk unattended.  A batch
        cannot continue through that, so the run is named and the process is
        suspended -- not killed, which is the difference between stopping the
        runaway and touching the save file.
        """
        limit = self.config.game_log_growth_limit_bytes_per_second
        path = live_client_log(self.config.game_log_dir)
        now = self.clock()
        previous = self._client_log_sample
        try:
            size = path.stat().st_size if path is not None else None
        except OSError:
            size = None
        self._client_log_sample = (path, size, now) if path and size is not None else None
        if (
            path is None
            or size is None
            or previous is None
            or previous[0] != path
            or now <= previous[2]
        ):
            self._client_log_fast_samples = 0
            return None
        rate = (size - previous[1]) / (now - previous[2])
        if rate <= limit:
            self._client_log_fast_samples = 0
            return None
        self._client_log_fast_samples += 1
        if self._client_log_fast_samples < self.config.game_log_wedge_samples:
            return None
        report = {
            "log_path": str(path),
            "log_bytes": size,
            "bytes_per_second": round(rate, 1),
            "samples_over_limit": self._client_log_fast_samples,
            "limit_bytes_per_second": limit,
            "repeated_errors": repeated_log_errors(path),
            "client": freeze_client(),
            "note": (
                "the client was suspended mid-screen, not killed; nothing was "
                "written to its save files, and closing the window from the "
                "desktop is the operator's call"
            ),
        }
        return report

    def _preflight_gates(self) -> dict[str, Any]:
        """Answer "can a batch be played here" from the files the run trusts.

        Until now ``--dry-run`` checked configuration only, while the entry point
        pointed at it as the gate for the build and the mod bytes.  The installed
        game has to match the lock or the acceptance target is void, so that one
        fails closed.  Mod *bytes* are measured and reported but do not gate:
        the operator owns the in-game solver's auto-update, and refusing a batch
        over a solver version bump would override that standing decision.
        """
        gates: dict[str, Any] = {"lock_file": str(DEFAULT_AUTOPLAY_LOCK)}
        failures: list[str] = []
        try:
            lock = VersionLock.load(DEFAULT_AUTOPLAY_LOCK)
            gates["installed_game"] = lock.verify_installed_game()
        except Exception as exc:
            gates["installed_game_error"] = f"{type(exc).__name__}: {exc}"
            failures.append(f"installed game does not match the lock: {exc}")
        attestation = self.mods_at_start or {}
        gates["mod_attestation"] = {
            key: attestation.get(key)
            for key in ("all_match_lock", "drifted_from_lock", "unreadable", "error")
        }
        if attestation.get("error"):
            failures.append(f"mod bytes could not be measured: {attestation['error']}")
        elif attestation.get("unreadable"):
            failures.append(
                "locked mod files unreadable or missing: "
                + ", ".join(str(mod) for mod in attestation["unreadable"])
            )
        gates["failures"] = failures
        gates["ok"] = not failures
        return gates

    def run(self) -> int:
        """Execute the lifecycle and always release the active lock."""

        try:
            self._prepare()
            self._install_signal_handlers()
            if self.config.dry_run:
                gates = self._preflight_gates()
                self.preflight_gates = gates
                self.stop_reason = "dry_run" if gates["ok"] else "preflight_failed"
                self._log("dry_run", commands=self.commands, preflight_gates=gates)
                for failure in gates["failures"]:
                    print(f"[preflight] {failure}", file=sys.stderr, flush=True)
                self.result_code = EXIT_OK if gates["ok"] else EXIT_PREFLIGHT_FAILED
                self.completed_at_utc = _utc_now()
                self._persist(self.stop_reason)
                return self.result_code
            if not self._wait_for_game():
                self.result_code = EXIT_GAME_WAIT_TIMEOUT
                self.completed_at_utc = _utc_now()
                self._persist("failed")
                return self.result_code
            self._start_children()
            self.result_code = self._monitor()
            self.completed_at_utc = _utc_now()
            final_status = (
                "complete"
                if self.result_code == EXIT_OK
                else "stopped"
                if self.result_code == EXIT_STOPPED
                else "partial"
                if self.result_code == EXIT_PARTIAL
                else "failed"
            )
            self._persist(final_status)
            return self.result_code
        except SupervisorAlreadyRunning:
            # This happens before artifacts are created; preserve the clear
            # exception for library callers and make CLI map it to code 6.
            raise
        except BatchAlreadyExists:
            raise
        except (SupervisorConfigurationError, ChildStartError) as exc:
            self.stop_reason = "setup_failed"
            self._log("setup_failed", error=f"{type(exc).__name__}: {exc}")
            if isinstance(exc, ChildStartError):
                self._stop_children("setup_failed")
            self.result_code = EXIT_USAGE if isinstance(exc, SupervisorConfigurationError) else EXIT_CHILD_FAILED
            if self._prepared:
                self.completed_at_utc = _utc_now()
                self._persist("failed")
            return self.result_code
        except KeyboardInterrupt:
            self.stop_reason = "interrupt"
            self._stop_children("interrupt")
            self.result_code = EXIT_STOPPED
            if self._prepared:
                self.completed_at_utc = _utc_now()
                try:
                    self._finalize_stop()
                except OSError:
                    pass
                self._persist("stopped")
            return self.result_code
        except BaseException as exc:
            self.stop_reason = "supervisor_exception"
            self._log("supervisor_exception", error=f"{type(exc).__name__}: {exc}")
            self._stop_children("supervisor_exception")
            self.result_code = EXIT_CHILD_FAILED
            if self._prepared:
                self.completed_at_utc = _utc_now()
                try:
                    self._finalize_stop()
                except OSError:
                    pass
                self._persist("failed")
            return self.result_code
        finally:
            if self._prepared and self.result_code is not None and self.completed_at_utc is None:
                # Safety net for an early return added in a future lifecycle
                # branch; final artifacts must always expose a completion time.
                self.completed_at_utc = _utc_now()
                final_status = (
                    "complete"
                    if self.result_code == EXIT_OK
                    else "stopped"
                    if self.result_code == EXIT_STOPPED
                    else "partial"
                    if self.result_code == EXIT_PARTIAL
                    else "failed"
                )
                try:
                    self._persist(final_status)
                except OSError:
                    pass
            self._restore_signal_handlers()
            if self._supervisor_log is not None:
                self._supervisor_log.close()
                self._supervisor_log = None
            self.lock.release()


def threading_is_main_thread() -> bool:
    """Small lazy helper; importing ``threading`` is unnecessary for workers."""

    # Signal handlers are process-global and only legal on the main thread.
    # Avoid a module-level dependency for the short-lived dry-run command.
    import threading

    return threading.current_thread() is threading.main_thread()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("fixed", "observational"),
        required=True,
        help="seed evidence mode; must be explicit for every new batch",
    )
    parser.add_argument(
        "--track",
        choices=("comparison", "acceptance"),
        default="comparison",
        help=(
            "comparison pins every required mod (the solver is the variable "
            "under test); acceptance plays a real run and records solver drift "
            "as a blocker instead of aborting. Both still refuse a mod that is "
            "missing or unreadable."
        ),
    )
    parser.add_argument("--batch-id", default=None)
    parser.add_argument(
        "--seed-file",
        type=Path,
        default=None,
        help="ordered fixed-seed allocation (fixed mode uses the checked-in allocation by default)",
    )
    parser.add_argument(
        "--seed-ledger",
        type=Path,
        default=None,
        help=(
            "batch-local crash-safe seed ledger (defaults to the current batch "
            "directory); pass an existing path only for explicit active-run "
            "reconciliation, not comparison resume"
        ),
    )
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--base-url", default="http://127.0.0.1:15526")
    parser.add_argument("--health-path", default="/")
    parser.add_argument("--game-log-dir", type=Path, default=DEFAULT_GAME_LOG_DIR)
    parser.add_argument("--max-battles", type=int, default=50)
    parser.add_argument("--max-seconds", type=int, default=None)
    parser.add_argument("--max-runs", type=int, default=30)
    parser.add_argument("--max-actions", type=int, default=3000)
    parser.add_argument("--poll", type=float, default=0.5)
    parser.add_argument("--game-wait-seconds", type=float, default=60.0)
    parser.add_argument("--game-loss-grace-seconds", type=float, default=5.0)
    parser.add_argument("--graceful-timeout-seconds", type=float, default=10.0)
    parser.add_argument("--run-timeout-seconds", type=float, default=None)
    parser.add_argument(
        "--allow-actions",
        action="store_true",
        help="explicitly allow autoplay POST actions for a live run",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="create a manifest and print child commands without probing or starting them",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    batch_id = args.batch_id or new_batch_id()
    try:
        config = SupervisorConfig(
            batch_id=batch_id,
            mode=args.mode,
            track=args.track,
            seed_file=args.seed_file,
            seed_ledger=args.seed_ledger,
            output_root=args.output_root,
            base_url=args.base_url,
            health_path=args.health_path,
            game_log_dir=args.game_log_dir,
            max_battles=args.max_battles,
            max_seconds=args.max_seconds,
            max_runs=args.max_runs,
            max_actions=args.max_actions,
            poll_seconds=args.poll,
            game_wait_seconds=args.game_wait_seconds,
            game_loss_grace_seconds=args.game_loss_grace_seconds,
            graceful_timeout_seconds=args.graceful_timeout_seconds,
            run_timeout_seconds=args.run_timeout_seconds,
            allow_actions=args.allow_actions,
            dry_run=args.dry_run,
        )
        supervisor = BatchSupervisor(config)
        result = supervisor.run()
        print(
            json.dumps(
                {
                    "batch_id": batch_id,
                    "mode": args.mode,
                    "result_code": result,
                    "status_path": str(supervisor.status_path),
                    "manifest_path": str(supervisor.manifest_path),
                },
                ensure_ascii=False,
            )
        )
        return result
    except SupervisorAlreadyRunning as exc:
        print(f"supervisor already running: {exc}", file=sys.stderr)
        return EXIT_ALREADY_RUNNING
    except BatchAlreadyExists as exc:
        print(f"batch already exists: {exc}", file=sys.stderr)
        return EXIT_BATCH_EXISTS
    except SupervisorConfigurationError as exc:
        parser.error(str(exc))
        return EXIT_USAGE  # pragma: no cover - parser.error raises


if __name__ == "__main__":
    raise SystemExit(main())
