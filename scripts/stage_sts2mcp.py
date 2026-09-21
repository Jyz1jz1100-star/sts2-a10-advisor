"""Fail-closed staging, installation, and rollback for a candidate STS2MCP.

This tool is deliberately separate from the live bridge and comparison
runner.  It can inspect a candidate DLL/manifest and the installed game, but
it never changes the game directory unless ``--apply`` is explicitly passed.
Before an apply it requires the game process and the loopback bridge port to
be demonstrably idle, verifies the version lock and both candidate hashes,
and creates a hash-verified backup of both installed bridge files.  Replacing
the files is done by copying to same-directory temporary files followed by
``os.replace``; the original candidate files are never consumed.

The tool never edits ``live_version.lock.json`` and never treats a successful
file replacement as a healthy bridge.  It emits a lock-update suggestion that
requires a later real health/state/compendium smoke before a human updates
the lock.

Examples (PowerShell):

    python scripts/stage_sts2mcp.py `
      --candidate-dll artifacts/sts2mcp-seeded/STS2_MCP.dll `
      --candidate-manifest artifacts/sts2mcp-seeded/STS2_MCP.json `
      --candidate-dll-sha256 <sha256> `
      --candidate-manifest-sha256 <sha256>

    python scripts/stage_sts2mcp.py ... --apply
    python scripts/stage_sts2mcp.py --rollback <backup-manifest.json>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bridge.trace_controller import VersionLock, VersionLockError  # noqa: E402


SHA256_RE = re.compile(r"^[0-9A-Fa-f]{64}$")
BACKUP_MANIFEST = "backup_manifest.json"
SUGGESTION_FILE = "lock_update_suggestion.json"
# FILE_ATTRIBUTE_REPARSE_POINT is exposed by ``stat`` on some Python/Windows
# versions, but not all of the versions supported by this project.  Keep the
# value here as a compatibility fallback.  A reparse point is not safe to use
# as part of a staging path: junctions and name-surrogate reparse points can
# redirect an otherwise apparently-contained path outside its root.
REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)


class StagingError(RuntimeError):
    """A preflight, install, or rollback safety check failed."""


@dataclass(frozen=True)
class StagingPlan:
    lock_path: Path
    target_dll: Path
    target_manifest: Path
    backup_root: Path
    candidate_dll: Path | None
    candidate_manifest: Path | None
    expected_target_dll_sha256: str
    expected_target_manifest_sha256: str
    expected_candidate_dll_sha256: str | None
    expected_candidate_manifest_sha256: str | None
    bridge_base_url: str
    bridge_version: str
    lock_raw: dict[str, Any]
    installed_game: dict[str, Any] | None


def _path_key(path: Path) -> str:
    """Compare lexical absolute paths without following links.

    ``Path.resolve`` is intentionally not used here.  Resolving a symlink
    before checking containment would turn a path that escaped through a link
    into the link target and make the original unsafe input impossible to
    detect.  Link/reparse components are rejected separately by
    ``_reject_reparse_components``.
    """
    return os.path.normcase(os.path.abspath(os.fspath(path))).casefold().rstrip("\\/") or os.path.sep


def _is_within(path: Path, root: Path) -> bool:
    path_key = _path_key(path)
    root_key = _path_key(root)
    if path_key == root_key:
        return True
    # ``/`` is the only root whose key is one character on POSIX.  For a
    # Windows drive root, ``abspath``/the trim above leaves ``c:``; adding the
    # separator gives the same boundary-safe prefix check.
    if root_key in {os.path.sep, os.path.altsep}:
        return path_key.startswith(root_key)
    return path_key.startswith(root_key + os.path.sep)


def _reparse_kind(path: Path) -> str | None:
    """Return ``symlink``/``reparse point`` for a link-like path component."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StagingError(f"cannot inspect path {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    if getattr(info, "st_file_attributes", 0) & REPARSE_POINT_ATTRIBUTE:
        return "reparse point"
    return None


def _reject_reparse_components(path: Path, field: str) -> None:
    """Reject symlink/reparse components without resolving the supplied path.

    The scan stops at the first missing component because descendants cannot
    exist until that directory is created.  Callers that need an existing
    object still validate it with ``_regular_file`` or ``_directory``.  This
    catches links in a parent directory as well as a link at the leaf, which
    is important for Windows junctions (``Path.is_symlink`` alone does not
    identify every reparse point).
    """
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    parts = absolute.parts
    for component in parts[1:] if absolute.anchor else parts:
        current = current / component
        try:
            kind = _reparse_kind(current)
        except StagingError as exc:
            raise StagingError(f"cannot inspect {field}: {exc}") from exc
        if kind is None:
            # A missing leaf or missing parent is handled by the object-level
            # validator.  No later component can be inspected usefully.
            try:
                os.lstat(current)
            except FileNotFoundError:
                break
            except OSError as exc:
                raise StagingError(f"cannot inspect {field} {current}: {exc}") from exc
            continue
        raise StagingError(f"{field} must not contain a {kind}: {current}")


def _directory(path: Path, field: str, *, allow_missing: bool = False) -> None:
    """Require a real directory and reject links/reparse points."""
    _reject_reparse_components(path, field)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if allow_missing:
            return
        raise StagingError(f"{field} does not exist: {path}") from None
    except OSError as exc:
        raise StagingError(f"cannot inspect {field} {path}: {exc}") from exc
    kind = _reparse_kind(path)
    if kind is not None:
        raise StagingError(f"{field} must not be a {kind}: {path}")
    if not stat.S_ISDIR(info.st_mode):
        raise StagingError(f"{field} is not a directory: {path}")


def _absolute_path(value: str | Path, field: str) -> Path:
    if not isinstance(value, (str, Path)):
        raise StagingError(f"{field} must be a path: {value!r}")
    try:
        path = Path(value).expanduser()
    except (OSError, RuntimeError, TypeError) as exc:
        raise StagingError(f"{field} is not a valid path: {value!r}") from exc
    if not path.is_absolute():
        raise StagingError(f"{field} must be an absolute path: {value!r}")
    # ``abspath`` normalizes ``.``/``..`` but, unlike ``resolve``, preserves
    # the lexical link/reparse component so it can be rejected below.
    path = Path(os.path.abspath(os.fspath(path)))
    _reject_reparse_components(path, field)
    return path


def _regular_file(path: Path, field: str) -> None:
    _reject_reparse_components(path, field)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise StagingError(f"{field} is not a regular file: {path}") from None
    except OSError as exc:
        raise StagingError(f"cannot inspect {field} {path}: {exc}") from exc
    kind = _reparse_kind(path)
    if kind is not None:
        raise StagingError(f"{field} must not be a {kind}: {path}")
    if not stat.S_ISREG(info.st_mode):
        raise StagingError(f"{field} is not a regular file: {path}")


def sha256_file(path: Path) -> str:
    _regular_file(path, "file")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise StagingError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest().upper()


def _hash_value(value: Any, field: str, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value.strip()):
        raise StagingError(f"{field} must be a 64-character SHA-256 hex string")
    return value.strip().upper()


def _json_object(path: Path, field: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StagingError(f"cannot read {field} JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise StagingError(f"{field} JSON must be an object: {path}")
    return payload


def _validate_loopback_url(value: Any) -> tuple[str, int]:
    if not isinstance(value, str) or not value.strip():
        raise StagingError("lock bridge.base_url is missing")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise StagingError(f"bridge.base_url must use HTTP(S): {value!r}")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise StagingError("bridge.base_url must be loopback for staging")
    try:
        port = parsed.port
    except ValueError as exc:
        raise StagingError(f"bridge.base_url has an invalid port: {value!r}") from exc
    if port is None or not 1 <= port <= 65535:
        raise StagingError(f"bridge.base_url has no valid port: {value!r}")
    return parsed.hostname, port


def _verify_installed_game(lock: VersionLock) -> dict[str, Any]:
    """Indirection kept injectable for deterministic preflight tests."""
    try:
        return lock.verify_installed_game()
    except VersionLockError:
        raise
    except Exception as exc:
        raise StagingError(f"installed game failed version-lock check: {exc}") from exc


def _load_lock(lock_path: Path, *, verify_game: bool = True) -> tuple[dict[str, Any], dict[str, Any] | None]:
    try:
        lock = VersionLock.load(lock_path)
    except (VersionLockError, OSError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise StagingError(f"cannot load version lock {lock_path}: {exc}") from exc
    raw = lock.raw
    game = raw.get("game")
    bridge = raw.get("bridge")
    if not isinstance(game, dict) or not isinstance(bridge, dict):
        raise StagingError("version lock must contain game and bridge objects")
    for field in ("version", "commit", "steam_build_id", "branch"):
        if game.get(field) in (None, ""):
            raise StagingError(f"version lock game.{field} is missing")
    for field in ("dll_path", "manifest_path"):
        value = bridge.get(field)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise StagingError(f"version lock bridge.{field} must be absolute")
    for field in ("dll_sha256", "manifest_sha256"):
        _hash_value(bridge.get(field), f"version lock bridge.{field}")
    _validate_loopback_url(bridge.get("base_url"))
    observed = _verify_installed_game(lock) if verify_game else None
    return raw, observed


def _validate_candidate_manifest(path: Path, bridge_version: str) -> dict[str, Any]:
    _regular_file(path, "candidate manifest")
    payload = _json_object(path, "candidate manifest")
    if payload.get("id") != "STS2_MCP":
        raise StagingError(
            f"candidate manifest id must be STS2_MCP, observed {payload.get('id')!r}"
        )
    if payload.get("has_dll") is not True:
        raise StagingError("candidate manifest must declare has_dll=true")
    if str(payload.get("version") or "") != str(bridge_version):
        raise StagingError(
            "candidate manifest version disagrees with the game lock: "
            f"{payload.get('version')!r} != {bridge_version!r}"
        )
    return payload


def _ensure_separate_paths(plan: StagingPlan) -> None:
    target_parent = plan.target_dll.parent
    if plan.target_manifest.parent != target_parent:
        raise StagingError("target DLL and manifest must be in the same mods directory")
    # The backup root may be created by ``apply`` later, but every existing
    # component must already be an ordinary directory.  In particular, do
    # not let ``mkdir(parents=True)`` walk through a pre-existing junction.
    _directory(plan.backup_root, "backup root", allow_missing=True)
    for source, field in (
        (plan.candidate_dll, "candidate DLL"),
        (plan.candidate_manifest, "candidate manifest"),
    ):
        if source is None:
            continue
        if _path_key(source) in {_path_key(plan.target_dll), _path_key(plan.target_manifest)}:
            raise StagingError(f"{field} must not be the installed target")
        if _is_within(source, target_parent):
            raise StagingError(
                f"{field} must be outside the installed mods directory: {source}"
            )
    backup_root = plan.backup_root
    if _is_within(backup_root, target_parent) or _is_within(target_parent, backup_root):
        raise StagingError(
            "backup directory must be separate from (and not contain) the installed mods directory"
        )
    if _path_key(backup_root) in {_path_key(plan.target_dll), _path_key(plan.target_manifest)}:
        raise StagingError("backup directory cannot be a target file")
    if backup_root == backup_root.parent:
        raise StagingError("backup directory cannot be a filesystem root")


def game_process_running(process_name: str = "SlayTheSpire2.exe") -> bool:
    """Return whether the game process is present; command errors fail closed."""
    if os.name == "nt":
        command = ["tasklist", "/FI", f"IMAGENAME eq {process_name}", "/FO", "CSV", "/NH"]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise StagingError(f"cannot inspect game processes: {exc}") from exc
        if result.returncode != 0:
            raise StagingError(
                f"tasklist failed with exit code {result.returncode}: {result.stderr.strip()}"
            )
        return any(
            line.strip() and not line.lstrip().upper().startswith("INFO:")
            and process_name.casefold() in line.casefold()
            for line in result.stdout.splitlines()
        )

    stem = Path(process_name).stem
    try:
        result = subprocess.run(
            ["pgrep", "-x", stem],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise StagingError(f"cannot inspect game processes: {exc}") from exc
    if result.returncode not in (0, 1):
        raise StagingError(f"pgrep failed with exit code {result.returncode}")
    return result.returncode == 0


def bridge_port_occupied(base_url: str, timeout: float = 0.35) -> bool:
    """Return whether the bridge port has a local TCP listener.

    Windows' ``connect()`` probe is not a reliable availability test: a
    filtered or otherwise unreachable local endpoint can time out even when
    nothing is listening.  Query the kernel's listener table instead and fail
    closed on command, timeout, or parse errors.  POSIX keeps the small socket
    probe because its connection-refused result is deterministic for this
    local-only bridge.
    """
    host, port = _validate_loopback_url(base_url)
    if os.name == "nt":
        command = [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                # Windows PowerShell inherits the active console code page in
                # some hosts.  Set both encodings explicitly even though the
                # command emits only ASCII sentinels; stderr may still carry
                # localized text and is decoded strictly below.
                "$utf8=New-Object System.Text.UTF8Encoding($false); "
                "[Console]::OutputEncoding=$utf8; $OutputEncoding=$utf8; "
                "$ErrorActionPreference='Stop'; "
                # Get-NetTCPConnection treats an explicit -LocalPort query
                # with no match as CmdletizationQuery_NotFound.  Enumerate
                # the successful Listen view first, then filter in PowerShell
                # so an empty result is the valid FREE sentinel.
                f"$listeners=@(Get-NetTCPConnection -State Listen -ErrorAction Stop "
                f"| Where-Object {{ $_.LocalPort -eq {port} }}); "
                "if ($listeners.Count -gt 0) { 'LISTEN' } else { 'FREE' }"
            ),
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                # Capture bytes so Python's subprocess reader cannot decode
                # with the machine's GBK/ACP before this function can apply
                # the explicit UTF-8 policy.
                text=False,
                check=False,
                # Process startup can exceed the socket probe's 350ms, while
                # still remaining bounded.  A query that cannot complete is
                # unknown and therefore blocks staging.  The floor has to clear
                # the real cost of starting Windows PowerShell at all -- one
                # second is not that, and the 1s floor made an idle machine read
                # as "cannot enumerate", which blocked a legitimate install.
                # Raising it changes only the budget of the query, never the
                # rule: the answer must still be FREE.
                timeout=max(25.0, float(timeout)),
            )
        except Exception as exc:
            raise StagingError(
                f"cannot enumerate Windows TCP listeners for port {port}: {exc}"
            ) from exc

        def _decode_output(value: Any, stream: str) -> str:
            if value is None:
                return ""
            if isinstance(value, bytes):
                try:
                    return value.decode("utf-8", errors="strict")
                except UnicodeDecodeError as exc:
                    raise StagingError(
                        f"cannot decode Windows listener {stream} as UTF-8: {exc}"
                    ) from exc
            if isinstance(value, str):
                return value
            raise StagingError(
                f"Windows listener {stream} output has unexpected type "
                f"{type(value).__name__}"
            )

        try:
            stdout = _decode_output(result.stdout, "stdout")
            stderr = _decode_output(result.stderr, "stderr")
            returncode = result.returncode
        except StagingError:
            raise
        except Exception as exc:
            raise StagingError(
                f"cannot inspect Windows TCP listener query result: {exc}"
            ) from exc
        if not isinstance(returncode, int):
            raise StagingError(
                "Windows listener query returned an invalid exit code: "
                f"{returncode!r}"
            )
        if returncode != 0:
            raise StagingError(
                "Get-NetTCPConnection failed with exit code "
                f"{returncode}: {stderr.strip()}"
            )
        observed = stdout.strip()
        if observed == "LISTEN":
            return True
        if observed == "FREE":
            return False
        raise StagingError(
            "cannot parse Get-NetTCPConnection listener result for "
            f"port {port}: {observed!r}"
        )
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except ConnectionRefusedError:
        return False
    except OSError as exc:
        # Windows and POSIX expose connection-refused as different errno
        # values.  A timeout or any other error is unknown, not "free".
        if getattr(exc, "errno", None) in {111, 10061}:
            return False
        raise StagingError(f"cannot prove bridge port {host}:{port} is free: {exc}") from exc


def build_plan(
    *,
    lock_path: str | Path,
    candidate_dll: str | Path | None,
    candidate_manifest: str | Path | None,
    backup_root: str | Path,
    target_dll: str | Path | None = None,
    target_manifest: str | Path | None = None,
    candidate_dll_sha256: str | None = None,
    candidate_manifest_sha256: str | None = None,
    verify_game: bool = True,
) -> StagingPlan:
    lock_path_resolved = _absolute_path(lock_path, "lock_file")
    _regular_file(lock_path_resolved, "lock_file")
    raw, installed_game = _load_lock(lock_path_resolved, verify_game=verify_game)
    bridge = raw["bridge"]
    locked_dll = _absolute_path(bridge["dll_path"], "lock bridge.dll_path")
    locked_manifest = _absolute_path(bridge["manifest_path"], "lock bridge.manifest_path")
    actual_target_dll = _absolute_path(target_dll or locked_dll, "target_dll")
    actual_target_manifest = _absolute_path(
        target_manifest or locked_manifest, "target_manifest"
    )
    if _path_key(actual_target_dll) != _path_key(locked_dll):
        raise StagingError("target_dll does not exactly match version-lock bridge.dll_path")
    if _path_key(actual_target_manifest) != _path_key(locked_manifest):
        raise StagingError(
            "target_manifest does not exactly match version-lock bridge.manifest_path"
        )
    candidate_dll_path = (
        _absolute_path(candidate_dll, "candidate_dll") if candidate_dll is not None else None
    )
    candidate_manifest_path = (
        _absolute_path(candidate_manifest, "candidate_manifest")
        if candidate_manifest is not None
        else None
    )
    if (candidate_dll_path is None) != (candidate_manifest_path is None):
        raise StagingError("candidate_dll and candidate_manifest must be supplied together")
    if candidate_dll_path is not None:
        _regular_file(candidate_dll_path, "candidate DLL")
        _validate_candidate_manifest(candidate_manifest_path, str(bridge["version"]))
    plan = StagingPlan(
        lock_path=lock_path_resolved,
        target_dll=actual_target_dll,
        target_manifest=actual_target_manifest,
        backup_root=_absolute_path(backup_root, "backup_root"),
        candidate_dll=candidate_dll_path,
        candidate_manifest=candidate_manifest_path,
        expected_target_dll_sha256=_hash_value(
            bridge.get("dll_sha256"), "lock target DLL hash"
        ),
        expected_target_manifest_sha256=_hash_value(
            bridge.get("manifest_sha256"), "lock target manifest hash"
        ),
        expected_candidate_dll_sha256=_hash_value(
            candidate_dll_sha256, "candidate DLL expected hash", required=False
        ),
        expected_candidate_manifest_sha256=_hash_value(
            candidate_manifest_sha256, "candidate manifest expected hash", required=False
        ),
        bridge_base_url=str(bridge["base_url"]),
        bridge_version=str(bridge["version"]),
        lock_raw=raw,
        installed_game=installed_game,
    )
    _ensure_separate_paths(plan)
    return plan


def preflight_candidate(
    plan: StagingPlan,
    *,
    process_checker: Callable[[], bool] = game_process_running,
    bridge_checker: Callable[[str], bool] = bridge_port_occupied,
    require_candidate_hashes: bool = True,
) -> dict[str, Any]:
    """Perform every read-only check required before an apply."""
    _regular_file(plan.target_dll, "installed target DLL")
    _regular_file(plan.target_manifest, "installed target manifest")
    target_dll_hash = sha256_file(plan.target_dll)
    target_manifest_hash = sha256_file(plan.target_manifest)
    if target_dll_hash != plan.expected_target_dll_sha256:
        raise StagingError(
            "installed target DLL hash differs from live_version.lock.json: "
            f"{target_dll_hash} != {plan.expected_target_dll_sha256}"
        )
    if target_manifest_hash != plan.expected_target_manifest_sha256:
        raise StagingError(
            "installed target manifest hash differs from live_version.lock.json: "
            f"{target_manifest_hash} != {plan.expected_target_manifest_sha256}"
        )
    candidate_hashes: dict[str, str] = {}
    if plan.candidate_dll is not None:
        candidate_hashes["dll"] = sha256_file(plan.candidate_dll)
        candidate_hashes["manifest"] = sha256_file(plan.candidate_manifest)
        if require_candidate_hashes and (
            plan.expected_candidate_dll_sha256 is None
            or plan.expected_candidate_manifest_sha256 is None
        ):
            raise StagingError(
                "candidate DLL and manifest hashes must be explicitly pinned before apply"
            )
        if (
            plan.expected_candidate_dll_sha256 is not None
            and candidate_hashes["dll"] != plan.expected_candidate_dll_sha256
        ):
            raise StagingError(
                "candidate DLL hash mismatch: "
                f"{candidate_hashes['dll']} != {plan.expected_candidate_dll_sha256}"
            )
        if (
            plan.expected_candidate_manifest_sha256 is not None
            and candidate_hashes["manifest"] != plan.expected_candidate_manifest_sha256
        ):
            raise StagingError(
                "candidate manifest hash mismatch: "
                f"{candidate_hashes['manifest']} != {plan.expected_candidate_manifest_sha256}"
            )
    if process_checker():
        raise StagingError("SlayTheSpire2.exe is running; refusing bridge file mutation")
    if bridge_checker(plan.bridge_base_url):
        raise StagingError("STS2MCP loopback bridge is occupied; refusing bridge file mutation")
    suggestion = lock_update_suggestion(plan, candidate_hashes)
    return {
        "status": "ready",
        "mode": "candidate",
        "lock_file": str(plan.lock_path),
        "installed_game": plan.installed_game,
        "target": {
            "dll": str(plan.target_dll),
            "manifest": str(plan.target_manifest),
            "dll_sha256": target_dll_hash,
            "manifest_sha256": target_manifest_hash,
        },
        "candidate": {
            "dll": str(plan.candidate_dll) if plan.candidate_dll else None,
            "manifest": str(plan.candidate_manifest) if plan.candidate_manifest else None,
            "dll_sha256": candidate_hashes.get("dll"),
            "manifest_sha256": candidate_hashes.get("manifest"),
        },
        "guards": {
            "game_process_running": False,
            "bridge_port_occupied": False,
            "target_hashes_match_lock": True,
            "candidate_hashes_pinned": (
                plan.expected_candidate_dll_sha256 is not None
                and plan.expected_candidate_manifest_sha256 is not None
            ),
        },
        "lock_update_suggestion": suggestion,
        "mutated": False,
    }


def lock_update_suggestion(
    plan: StagingPlan, candidate_hashes: dict[str, str]
) -> dict[str, Any]:
    """Describe a future lock edit without claiming any live health check."""
    return {
        "lock_file": str(plan.lock_path),
        "current_locked_bridge": {
            "version": plan.bridge_version,
            "dll_sha256": plan.expected_target_dll_sha256,
            "manifest_sha256": plan.expected_target_manifest_sha256,
        },
        "candidate_observed": {
            "dll_sha256": candidate_hashes.get("dll"),
            "manifest_sha256": candidate_hashes.get("manifest"),
        },
        "proposed_bridge_hashes": {
            "dll_sha256": candidate_hashes.get("dll"),
            "manifest_sha256": candidate_hashes.get("manifest"),
        },
        "health_verification": {
            "performed": False,
            "required": [
                "start current locked game with candidate enabled",
                "GET / returns expected bridge version",
                "GET /api/v1/singleplayer?format=json succeeds",
                "GET /api/v1/compendium succeeds and identity is verified",
                "run read-only real-machine smoke before updating the lock",
            ],
        },
        "written_to_lock": False,
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _directory(path.parent, "JSON destination directory", allow_missing=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    _directory(path.parent, "JSON destination directory")
    # Replacing a leaf symlink is not an acceptable fallback.  The check also
    # rejects a reparse-point leaf on Windows before the replace operation.
    _reject_reparse_components(path, "JSON destination")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
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


def _copy_atomic(source: Path, destination: Path) -> None:
    _regular_file(source, "source")
    _directory(destination.parent, "copy destination directory", allow_missing=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _directory(destination.parent, "copy destination directory")
    # Keep the destination lexical path visible to the safety checks.  If it
    # is a symlink/junction, os.replace could otherwise redirect the write or
    # silently replace a path that was not part of the checked transaction.
    _reject_reparse_components(destination, "copy destination")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".staging", dir=str(destination.parent)
    )
    try:
        with os.fdopen(descriptor, "wb") as target:
            with source.open("rb") as origin:
                shutil.copyfileobj(origin, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        try:
            shutil.copystat(source, temporary_name)
        except OSError:
            # File mode/timestamps are ancillary; content hash is the safety
            # contract and is checked after the replace.
            pass
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _new_backup_dir(root: Path) -> Path:
    _directory(root, "backup root", allow_missing=True)
    root.mkdir(parents=True, exist_ok=True)
    _directory(root, "backup root")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    for _ in range(8):
        candidate = root / f"sts2mcp-{stamp}-{uuid.uuid4().hex[:8]}"
        # exists() is false for a dangling link.  lexists() keeps that case
        # from being mistaken for a free transaction directory name.
        if os.path.lexists(candidate):
            continue
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        _directory(candidate, "new backup directory")
        return candidate
    raise StagingError(f"could not allocate a unique backup directory under {root}")


def apply_candidate(
    plan: StagingPlan,
    preflight: dict[str, Any],
    *,
    process_checker: Callable[[], bool] = game_process_running,
    bridge_checker: Callable[[str], bool] = bridge_port_occupied,
) -> dict[str, Any]:
    """Backup both targets, atomically replace both, and verify content hashes."""
    if preflight.get("status") != "ready" or preflight.get("mutated"):
        raise StagingError("apply requires a fresh successful preflight")
    if plan.candidate_dll is None or plan.candidate_manifest is None:
        raise StagingError("apply requires candidate DLL and manifest")
    if plan.expected_candidate_dll_sha256 is None or plan.expected_candidate_manifest_sha256 is None:
        raise StagingError("apply requires explicitly pinned candidate hashes")
    backup_dir = _new_backup_dir(plan.backup_root)
    backup_files = {
        "dll": backup_dir / plan.target_dll.name,
        "manifest": backup_dir / plan.target_manifest.name,
    }
    original_hashes = {
        "dll": sha256_file(plan.target_dll),
        "manifest": sha256_file(plan.target_manifest),
    }
    if original_hashes["dll"] != plan.expected_target_dll_sha256:
        raise StagingError("installed target DLL changed after preflight")
    if original_hashes["manifest"] != plan.expected_target_manifest_sha256:
        raise StagingError("installed target manifest changed after preflight")
    if process_checker():
        raise StagingError("SlayTheSpire2.exe started after preflight; refusing bridge mutation")
    if bridge_checker(plan.bridge_base_url):
        raise StagingError("STS2MCP bridge became occupied after preflight; refusing bridge mutation")
    if sha256_file(plan.candidate_dll) != plan.expected_candidate_dll_sha256:
        raise StagingError("candidate DLL changed after preflight")
    if sha256_file(plan.candidate_manifest) != plan.expected_candidate_manifest_sha256:
        raise StagingError("candidate manifest changed after preflight")
    for key, source in (("dll", plan.target_dll), ("manifest", plan.target_manifest)):
        _copy_atomic(source, backup_files[key])
        if sha256_file(backup_files[key]) != original_hashes[key]:
            raise StagingError(f"backup hash verification failed for {key}")
    backup_manifest = {
        "schema_version": 1,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "backup_root": str(plan.backup_root),
        "lock_file": str(plan.lock_path),
        "target": {
            "dll": str(plan.target_dll),
            "manifest": str(plan.target_manifest),
        },
        "files": {
            key: {
                "original_path": str(source),
                "backup_path": str(backup_files[key]),
                "sha256_original": original_hashes[key],
                "sha256_backup": sha256_file(backup_files[key]),
            }
            for key, source in (("dll", plan.target_dll), ("manifest", plan.target_manifest))
        },
        "candidate": preflight["candidate"],
        "lock_update_suggestion": preflight["lock_update_suggestion"],
        "rollback": {"available": True, "performed": False},
    }
    backup_manifest_path = backup_dir / BACKUP_MANIFEST
    _atomic_write_json(backup_manifest_path, backup_manifest)
    replaced: list[str] = []
    try:
        _copy_atomic(plan.candidate_dll, plan.target_dll)
        replaced.append("dll")
        _copy_atomic(plan.candidate_manifest, plan.target_manifest)
        replaced.append("manifest")
        if sha256_file(plan.target_dll) != plan.expected_candidate_dll_sha256:
            raise StagingError("post-install candidate DLL hash verification failed")
        if sha256_file(plan.target_manifest) != plan.expected_candidate_manifest_sha256:
            raise StagingError("post-install candidate manifest hash verification failed")
    except BaseException as exc:
        restore_errors: list[str] = []
        for key in reversed(replaced):
            try:
                _copy_atomic(backup_files[key], plan.target_dll if key == "dll" else plan.target_manifest)
            except Exception as restore_exc:
                restore_errors.append(f"{key}: {restore_exc}")
        if restore_errors:
            raise StagingError(
                f"candidate install failed and automatic restore was incomplete: {restore_errors}"
            ) from exc
        raise StagingError(f"candidate install failed; original files restored: {exc}") from exc

    suggestion_path = backup_dir / SUGGESTION_FILE
    _atomic_write_json(suggestion_path, preflight["lock_update_suggestion"])
    backup_manifest["rollback"]["backup_manifest"] = str(backup_manifest_path)
    _atomic_write_json(backup_manifest_path, backup_manifest)
    return {
        "status": "applied",
        "mutated": True,
        "backup_dir": str(backup_dir),
        "backup_manifest": str(backup_manifest_path),
        "lock_update_suggestion": str(suggestion_path),
        "target": {
            "dll": str(plan.target_dll),
            "manifest": str(plan.target_manifest),
            "dll_sha256": sha256_file(plan.target_dll),
            "manifest_sha256": sha256_file(plan.target_manifest),
        },
        "health_verification": {"performed": False, "lock_updated": False},
    }


def _load_backup_manifest(path: Path, plan: StagingPlan) -> tuple[dict[str, Any], dict[str, Path]]:
    _directory(plan.backup_root, "backup root")
    if path.name.casefold() != BACKUP_MANIFEST.casefold():
        raise StagingError(
            f"rollback manifest must be named {BACKUP_MANIFEST}: {path}"
        )
    if not _is_within(path, plan.backup_root):
        raise StagingError(
            "rollback manifest must be located inside the configured backup root"
        )
    # The manifest's parent is the transaction directory.  It must itself be
    # a real directory under the root; this prevents a manifest path such as
    # ``backup_root\\tx\\..\\outside\\backup_manifest.json`` or a junction
    # from redirecting the rollback files elsewhere.
    _directory(path.parent, "backup transaction directory")
    if not _is_within(path.parent, plan.backup_root):
        raise StagingError("backup transaction directory escapes backup root")
    _regular_file(path, "backup manifest")
    payload = _json_object(path, "backup manifest")
    if payload.get("schema_version") != 1:
        raise StagingError("unsupported backup manifest schema")
    declared_root = payload.get("backup_root")
    if not isinstance(declared_root, str):
        raise StagingError("backup manifest backup_root is missing")
    if _path_key(_absolute_path(declared_root, "backup manifest backup_root")) != _path_key(
        plan.backup_root
    ):
        raise StagingError("backup manifest backup_root does not match configured backup root")
    declared_lock = payload.get("lock_file")
    if not isinstance(declared_lock, str):
        raise StagingError("backup manifest lock_file is missing")
    if _path_key(_absolute_path(declared_lock, "backup manifest lock_file")) != _path_key(
        plan.lock_path
    ):
        raise StagingError("backup manifest lock_file does not match version lock")
    target = payload.get("target")
    if not isinstance(target, dict):
        raise StagingError("backup manifest target is missing")
    if _path_key(_absolute_path(target.get("dll"), "backup target DLL")) != _path_key(plan.target_dll):
        raise StagingError("backup manifest target DLL does not match version lock")
    if _path_key(_absolute_path(target.get("manifest"), "backup target manifest")) != _path_key(plan.target_manifest):
        raise StagingError("backup manifest target manifest does not match version lock")
    files = payload.get("files")
    if not isinstance(files, dict):
        raise StagingError("backup manifest files are missing")
    resolved: dict[str, Path] = {}
    for key, target_path, name in (
        ("dll", plan.target_dll, "DLL"),
        ("manifest", plan.target_manifest, "manifest"),
    ):
        entry = files.get(key)
        if not isinstance(entry, dict):
            raise StagingError(f"backup manifest {key} entry is missing")
        backup_path = _absolute_path(entry.get("backup_path"), f"backup {key} path")
        if (
            not _is_within(backup_path, plan.backup_root)
            or _path_key(backup_path.parent) != _path_key(path.parent)
            or backup_path.name.casefold() != target_path.name.casefold()
        ):
            raise StagingError(f"backup {key} path escapes its backup directory")
        original_hash = _hash_value(entry.get("sha256_original"), f"backup {key} original hash")
        backup_hash = _hash_value(entry.get("sha256_backup"), f"backup {key} backup hash")
        _regular_file(backup_path, f"backup {name}")
        if sha256_file(backup_path) != backup_hash or backup_hash != original_hash:
            raise StagingError(f"backup {name} hash verification failed")
        resolved[key] = backup_path
    return payload, resolved


def rollback_backup(
    plan: StagingPlan,
    backup_manifest_path: str | Path,
    *,
    process_checker: Callable[[], bool] = game_process_running,
    bridge_checker: Callable[[str], bool] = bridge_port_occupied,
) -> dict[str, Any]:
    """Restore the two hash-verified files from one backup transaction."""
    manifest_path = _absolute_path(backup_manifest_path, "backup_manifest")
    payload, backups = _load_backup_manifest(manifest_path, plan)
    if process_checker():
        raise StagingError("SlayTheSpire2.exe is running; refusing rollback")
    if bridge_checker(plan.bridge_base_url):
        raise StagingError("STS2MCP loopback bridge is occupied; refusing rollback")
    for target in (plan.target_dll, plan.target_manifest):
        if target.exists():
            _regular_file(target, "rollback target")
    current_snapshots: dict[str, Path] = {}
    snapshot_root = manifest_path.parent
    try:
        for key, target in (("dll", plan.target_dll), ("manifest", plan.target_manifest)):
            snapshot = snapshot_root / f".rollback-current-{key}-{uuid.uuid4().hex}.tmp"
            _copy_atomic(target, snapshot)
            current_snapshots[key] = snapshot
        _copy_atomic(backups["dll"], plan.target_dll)
        _copy_atomic(backups["manifest"], plan.target_manifest)
        for key, target in (("dll", plan.target_dll), ("manifest", plan.target_manifest)):
            expected = payload["files"][key]["sha256_original"]
            if sha256_file(target) != expected:
                raise StagingError(f"rollback {key} hash verification failed")
    except BaseException as exc:
        restore_errors: list[str] = []
        for key, target in (("dll", plan.target_dll), ("manifest", plan.target_manifest)):
            snapshot = current_snapshots.get(key)
            if snapshot is None:
                continue
            try:
                _copy_atomic(snapshot, target)
            except Exception as restore_exc:
                restore_errors.append(f"{key}: {restore_exc}")
        if restore_errors:
            raise StagingError(
                f"rollback failed and current files could not be restored: {restore_errors}"
            ) from exc
        raise StagingError(f"rollback failed; current files restored: {exc}") from exc
    finally:
        for snapshot in current_snapshots.values():
            try:
                snapshot.unlink(missing_ok=True)
            except OSError:
                pass
    result = {
        "status": "rolled_back",
        "mutated": True,
        "backup_manifest": str(manifest_path),
        "target": {
            "dll": str(plan.target_dll),
            "manifest": str(plan.target_manifest),
            "dll_sha256": sha256_file(plan.target_dll),
            "manifest_sha256": sha256_file(plan.target_manifest),
        },
        "health_verification": {"performed": False, "lock_updated": False},
    }
    payload["rollback"] = {
        "available": True,
        "performed": True,
        "performed_at_utc": datetime.now(UTC).isoformat(),
        "result": result,
    }
    _atomic_write_json(manifest_path, payload)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="explicitly replace installed bridge files")
    mode.add_argument("--rollback", type=Path, help="restore from an exact backup_manifest.json")
    parser.add_argument("--lock-file", type=Path, default=PROJECT_ROOT / "config" / "live_version.lock.json")
    parser.add_argument("--candidate-dll", type=Path)
    parser.add_argument("--candidate-manifest", type=Path)
    parser.add_argument("--candidate-dll-sha256")
    parser.add_argument("--candidate-manifest-sha256")
    parser.add_argument("--target-dll", type=Path)
    parser.add_argument("--target-manifest", type=Path)
    parser.add_argument(
        "--backup-root",
        type=Path,
        default=PROJECT_ROOT / "runs" / "sts2mcp_staging" / "backups",
    )
    parser.add_argument(
        "--no-installed-game-check",
        action="store_true",
        help="test-only escape hatch; never use for a real apply",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.no_installed_game_check and (args.apply or args.rollback is not None):
            raise StagingError(
                "--no-installed-game-check is test-only and cannot be combined with mutation"
            )
        if args.rollback is not None:
            plan = build_plan(
                lock_path=args.lock_file,
                candidate_dll=None,
                candidate_manifest=None,
                backup_root=args.backup_root,
                target_dll=args.target_dll,
                target_manifest=args.target_manifest,
                verify_game=not args.no_installed_game_check,
            )
            result = rollback_backup(plan, args.rollback)
        else:
            if args.candidate_dll is None or args.candidate_manifest is None:
                raise StagingError(
                    "candidate DLL and manifest are required for dry-run/apply"
                )
            plan = build_plan(
                lock_path=args.lock_file,
                candidate_dll=args.candidate_dll,
                candidate_manifest=args.candidate_manifest,
                backup_root=args.backup_root,
                target_dll=args.target_dll,
                target_manifest=args.target_manifest,
                candidate_dll_sha256=args.candidate_dll_sha256,
                candidate_manifest_sha256=args.candidate_manifest_sha256,
                verify_game=not args.no_installed_game_check,
            )
            result = preflight_candidate(plan)
            if args.apply:
                result = apply_candidate(plan, result)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (StagingError, VersionLockError) as exc:
        print(
            json.dumps(
                {"status": "blocked", "mutated": False, "error": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
