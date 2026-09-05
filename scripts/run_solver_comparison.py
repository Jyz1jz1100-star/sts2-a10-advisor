"""Run the Combat Solver comparison batch (Phase B of docs/COMBAT_SOLVER.md).

Read-only by construction: the game is polled over STS2MCP GET endpoints and
solver answers are consumed from configured on-disk sources. Nothing is ever
POSTed to the game and no game input is simulated.

Outputs under runs/combat_solver_compare/<batch-id>/:
* trace.jsonl      raw polled states (same shape as live traces);
* battles.jsonl    one closed-battle comparison record per line;
* summary.json     aggregated metrics, Wilson intervals, gate results;
* manifest.json    locks, hashes, versions, provenance.

Example:
    python scripts/run_solver_comparison.py --batch-id csb-001
Add --dry-run to verify locks/reader/seeds without touching the game.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import sys
import tempfile
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from bridge.trace_controller import (  # noqa: E402
    TraceRecorder,
    VersionLock,
    VersionLockError,
    decision_id,
)
from combat_solver.compare import aggregate_battles, evaluate_gates  # noqa: E402
from combat_solver.compare import BattleRecord, DeviationInfo, TurnOutcome  # noqa: E402
from combat_solver.executed import ExecutedTurn  # noqa: E402
from combat_solver.reader import (  # noqa: E402
    DirectorySource,
    JsonlSource,
    parse_normalized_json_bytes,
)
from combat_solver.session import BattleTracker  # noqa: E402
from combat_solver.snapshot import (  # noqa: E402
    RouteAction,
    SnapshotError,
    SolverFailure,
    SolverSnapshot,
    failure_from_json,
    parse_route_step,
    snapshot_from_json,
)


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest().upper()
    except OSError:
        return None


def _git_head() -> str | None:
    try:
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def load_config(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def resolve_gate_config(
    config: dict[str, Any] | None, automated: bool
) -> tuple[dict[str, float], dict[str, Any]]:
    """Resolve the gates that actually apply to this batch.

    The base ``[gates]`` section applies to every run.  The stricter
    provenance-dependent ``[automated_gates]`` section is intentionally
    merged only for ``--automated`` batches.  Keeping both the applied set and
    its source in the artifacts makes a manual diagnostic unable to inherit
    automated-only assumptions silently.
    """
    source = config or {}
    base = source.get("gates") or {}
    automated_config = source.get("automated_gates") or {}
    if not isinstance(base, dict) or not isinstance(automated_config, dict):
        raise BatchIntegrityError("gates and automated_gates must be TOML tables")

    merged = dict(base)
    if automated:
        merged.update(automated_config)
    gate_values = {
        key: float(value) for key, value in merged.items() if value is not None
    }
    gate_sources = {
        key: (
            "automated_gates"
            if automated and key in automated_config and automated_config[key] is not None
            else "gates"
        )
        for key in gate_values
    }
    ignored_automated = (
        {}
        if automated
        else {
            key: value
            for key, value in automated_config.items()
            if value is not None
        }
    )
    provenance = {
        "mode": "automated" if automated else "manual",
        "base_section": "gates",
        "automated_section": "automated_gates",
        "automated_section_applied": automated,
        "applied_gate_set": sorted(gate_values),
        "applied_gate_config": gate_values,
        "applied_gate_sources": gate_sources,
        "ignored_automated_gate_config": ignored_automated,
    }
    return gate_values, provenance


def verify_solver_inventory(solver_lock: VersionLock) -> dict:
    """Fail-closed mod inventory: every required DLL present and hash-matched."""
    environment = solver_lock.raw.get("evaluation_environment") or {}
    inventory = environment.get("mod_dll_inventory") or []
    results = []
    for entry in inventory:
        if not entry.get("required"):
            continue
        path_value = entry.get("path")
        expected = entry.get("sha256")
        observed = _sha256(Path(path_value)) if path_value else None
        ok = bool(path_value) and observed is not None and (
            expected is None or observed == str(expected).upper()
        )
        results.append(
            {
                "mod_id": entry.get("mod_id"),
                "path": path_value,
                "sha256_observed": observed,
                "ok": ok,
            }
        )
    failed = [r["mod_id"] for r in results if not r["ok"]]
    if failed:
        raise VersionLockError(
            "Combat Solver track mod inventory incomplete/failing (fill "
            "config/combat_solver.lock.json at installation time): "
            + ", ".join(str(m) for m in failed)
        )
    observed_mods = solver_lock.verify_live_mods()
    return {"verified": True, "mods": observed_mods, "inventory": results}


def cross_check_game_identity(live_lock: VersionLock, solver_lock: VersionLock) -> None:
    live_game = live_lock.game
    solver_game = solver_lock.raw.get("game") or {}
    for key in ("app_id", "steam_build_id", "version", "branch"):
        if str(solver_game.get(key)) != str(live_game.get(key)):
            raise VersionLockError(
                f"combat_solver.lock.json game.{key}="
                f"{solver_game.get(key)!r} disagrees with live lock "
                f"{live_game.get(key)!r}"
            )


def build_reader(config: dict, project_root: Path, mode_override: str | None = None):
    reader_cfg = config.get("reader") or {}
    mode = mode_override or reader_cfg.get("mode", "jsonl")
    if mode == "jsonl":
        path = project_root / reader_cfg.get("jsonl_path", "runtime/combat_solver/snapshots.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        return JsonlSource(path)
    if mode == "logtail":
        from combat_solver.logformat import LogTailSource

        live = reader_cfg.get("logtail") or {}
        raw_dir = str(live.get("game_log_dir", "runtime/combat_solver/logs"))
        log_dir = Path(os.path.expandvars(raw_dir))
        if not log_dir.is_absolute():
            log_dir = project_root / log_dir
        settings_raw = live.get("settings_path")
        settings_path = (
            Path(os.path.expandvars(str(settings_raw))) if settings_raw else None
        )
        return LogTailSource(
            log_dir,
            settings_path=settings_path,
            mod_version=live.get("mod_version") or None,
        )
    if mode == "directory":
        directory = project_root / reader_cfg.get("directory", "runtime/combat_solver/inbox")
        directory.mkdir(parents=True, exist_ok=True)
        return DirectorySource(
            directory,
            parse_fn=lambda payload, path: parse_normalized_json_bytes(
                payload, source=path.name
            ),
            pattern=reader_cfg.get("pattern", "*"),
        )
    raise ValueError(f"unknown reader mode {mode!r}")


def write_json(path: Path, payload: dict | list) -> None:
    """Atomically publish a JSON artifact for restart-safe batches."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
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


# ---------------------------------------------------------------------------
# Batch lifecycle / seed contract (kept in this runner so compare.py remains
# a pure metrics module).

SEED_MODES = ("fixed", "observational")
CHECKPOINT_FILE = "records_checkpoint.json"


class BatchIntegrityError(RuntimeError):
    """The persisted batch cannot support an auditable comparison claim."""


class SeedContractError(BatchIntegrityError):
    """A fixed-seed batch observed no verifiable fixed seed."""


class _StopBatch(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BatchIntegrityError(f"cannot read JSON artifact {path}: {exc}") from exc


def validate_seed_payload(payload: Any) -> tuple[dict[str, Any], set[int]]:
    """Validate the pre-registered run-seed allocation.

    ``count=12`` is the number of allocated *run seeds*, not a twelve-battle
    cap.  A seed can yield multiple combats; the file's expected battle count
    is consequently an estimate (12 x 9 = 108 by default).
    """
    if not isinstance(payload, dict):
        raise BatchIntegrityError("seed file must contain an object")
    partition = payload.get("partition")
    seeds = payload.get("seeds")
    if not isinstance(partition, dict) or not isinstance(seeds, list):
        raise BatchIntegrityError("seed file requires partition and seeds")
    name = partition.get("name")
    start = partition.get("start")
    count = partition.get("count")
    if (
        not isinstance(name, str)
        or not name
        or not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count <= 0
    ):
        raise BatchIntegrityError("seed partition requires name, positive start/count")
    if len(seeds) != count or any(
        not isinstance(seed, int) or isinstance(seed, bool) for seed in seeds
    ):
        raise BatchIntegrityError(
            f"seed partition {name!r} declares count={count} but carries "
            f"{len(seeds)} valid integer seeds"
        )
    expected = list(range(start, start + count))
    if seeds != expected:
        raise BatchIntegrityError(
            f"seed partition {name!r} must be the contiguous pre-registered range "
            f"{start}..{start + count - 1}"
        )
    expected_battles = payload.get("expected_battles")
    if expected_battles is not None and (
        not isinstance(expected_battles, int) or isinstance(expected_battles, bool)
    ):
        raise BatchIntegrityError("seed file expected_battles must be an integer")
    return partition, set(seeds)


def _active_run(state: dict[str, Any]) -> bool:
    run = state.get("run")
    if not isinstance(run, dict) or not run:
        return False
    if state.get("state_type") == "monster":
        return True
    return run.get("floor") is not None or run.get("act") is not None


def _partition_seed_key(raw_seed: Any) -> int | None:
    """Return the numeric allocation key without fabricating alphanumeric seeds.

    STS2's authoritative save can encode a seed as a decimal string or as an
    alphanumeric string (for example ``2450ZAR9EF``).  Decimal strings are
    canonically comparable with this project's numeric pre-registration and
    are normalized only for partition lookup.  The raw value remains in the
    trace/state; non-decimal seeds stay unavailable to the fixed contract.
    """
    if isinstance(raw_seed, bool):
        return None
    if isinstance(raw_seed, int):
        return raw_seed
    if isinstance(raw_seed, str):
        value = raw_seed.strip()
        if value and value.isascii() and value.isdecimal():
            try:
                return int(value)
            except ValueError:
                return None
    return None


class SeedAudit:
    """Observe real bridge seeds; never turn a missing value into a claim."""

    def __init__(self, mode: str, partition: dict[str, Any] | None, allowed: set[int]):
        if mode not in SEED_MODES:
            raise ValueError(f"unknown seed mode {mode!r}")
        self.mode = mode
        self.partition = partition
        self.allowed = set(allowed)
        self.observed: set[int] = set()
        self.missing_observations = 0
        self.outside_partition: set[int] = set()
        self.observations = 0

    def observe(self, state: dict[str, Any]) -> int | None:
        if not _active_run(state):
            return None
        self.observations += 1
        raw_seed = (state.get("run") or {}).get("seed")
        seed = _partition_seed_key(raw_seed)
        if seed is None:
            self.missing_observations += 1
            if self.mode == "fixed":
                name = (self.partition or {}).get("name", "unknown")
                raise SeedContractError(
                    "fixed-seed batch is blocked: live state has no integer-"
                    f"compatible run.seed (observed {raw_seed!r}) for partition "
                    f"{name!r}. The exact save value is preserved, but this "
                    "numeric allocation cannot verify it; rerun explicitly "
                    "with --seed-mode observational until the allocation and "
                    "bridge seed format agree, and do not treat this batch as "
                    "fixed-seed."
                )
            return None
        self.observed.add(seed)
        if seed not in self.allowed:
            self.outside_partition.add(seed)
            if self.mode == "fixed":
                name = (self.partition or {}).get("name", "unknown")
                raise SeedContractError(
                    f"observed run seed {seed} is outside pre-registered "
                    f"partition {name!r}"
                )
        return seed

    @property
    def fixed_seed_verified(self) -> bool:
        return (
            self.mode == "fixed"
            and self.observations > 0
            and self.missing_observations == 0
            and not self.outside_partition
            and bool(self.observed)
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "fixed_seed_verified": self.fixed_seed_verified,
            "observations": self.observations,
            "missing_observations": self.missing_observations,
            "observed_seeds": sorted(self.observed),
            "outside_partition": sorted(self.outside_partition),
            "partition": self.partition,
        }


RUN_IDENTITY_REQUIRED: dict[str, Any] = {
    "game_mode": "standard",
    "character": "IRONCLAD",
    "ascension": 10,
    "singleplayer": True,
}


def _first_nonempty(*values: Any) -> Any:
    """Return the first value that is present (zero is a valid value)."""
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _canonical_identity_character(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    token = value.strip().upper()
    if not token:
        return None
    token = token.rsplit(".", 1)[-1].replace(" ", "_")
    if token in {"IRONCLAD", "THE_IRONCLAD", "铁甲战士"}:
        return "IRONCLAD"
    return token


def _identity_ascension(value: Any) -> int | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _identity_equal(field: str, left: Any, right: Any) -> bool:
    if field == "seed":
        left_key = _partition_seed_key(left)
        right_key = _partition_seed_key(right)
        if left_key is not None and right_key is not None:
            return left_key == right_key
        if isinstance(left, str) and isinstance(right, str):
            return left.strip().upper() == right.strip().upper()
    if field == "run_id":
        return str(left).strip() == str(right).strip()
    return left == right


def _state_run_identity(state: dict[str, Any]) -> dict[str, Any]:
    run = state.get("run") if isinstance(state.get("run"), dict) else {}
    player = state.get("player") if isinstance(state.get("player"), dict) else {}
    players = state.get("players")
    if not player and isinstance(players, list) and len(players) == 1 and isinstance(players[0], dict):
        player = players[0]
    character_id = _first_nonempty(
        player.get("character_id"),
        player.get("characterId"),
        run.get("character_id"),
        state.get("character_id"),
    )
    character_label = _first_nonempty(
        player.get("character"),
        player.get("character_name"),
        run.get("character"),
        state.get("character"),
    )
    return {
        "character_id": character_id,
        "character": _canonical_identity_character(
            _first_nonempty(character_id, character_label)
        ),
        "character_label": character_label,
        "ascension": _identity_ascension(
            _first_nonempty(
                run.get("ascension"),
                state.get("ascension"),
                player.get("ascension"),
            )
        ),
        "game_mode": _first_nonempty(
            run.get("game_mode"),
            run.get("mode"),
            state.get("game_mode"),
            state.get("mode"),
        ),
        "run_id": _first_nonempty(run.get("run_id"), state.get("run_id")),
        "seed": _first_nonempty(run.get("seed"), state.get("seed")),
        "is_multiplayer": _first_nonempty(
            state.get("is_multiplayer"), run.get("is_multiplayer")
        ),
        "player_count": len(players) if isinstance(players, list) else None,
    }


def _compendium_run_identity(compendium: dict[str, Any]) -> dict[str, Any]:
    current = compendium.get("current_run") if isinstance(compendium, dict) else None
    if not isinstance(current, dict):
        return {}
    return {
        "is_in_progress": current.get("is_in_progress"),
        "game_mode": _first_nonempty(current.get("game_mode"), current.get("mode")),
        "ascension": _identity_ascension(current.get("ascension")),
        "character_id": _first_nonempty(
            current.get("character_id"), current.get("characterId")
        ),
        "character": _canonical_identity_character(
            _first_nonempty(
                current.get("character_id"),
                current.get("character"),
                current.get("character_name"),
            )
        ),
        "run_id": current.get("run_id"),
        "seed": current.get("seed"),
        "save_scope": current.get("save_scope"),
        "is_multiplayer": current.get("is_multiplayer"),
    }


RUN_IDENTITY_READ_TIMEOUT_SECONDS = 15.0
RUN_IDENTITY_READ_RETRY_SECONDS = 0.5


def _compendium_identity_ready(compendium: Any) -> bool:
    """Return true only for a stable, machine-complete current-run record."""
    if not isinstance(compendium, dict):
        return False
    current = compendium.get("current_run")
    if not isinstance(current, dict):
        return False
    # STS2MCP exposes a short-lived ``limitation`` marker while the save is
    # being created/read.  Never cache that partial object as identity
    # evidence: retry it within a bounded window instead.
    limitation = current.get("limitation") or compendium.get("limitation")
    if limitation not in (None, "", False):
        return False
    identity = _compendium_run_identity(compendium)
    run_id = identity.get("run_id")
    seed = identity.get("seed")
    return (
        identity.get("is_in_progress") is True
        and isinstance(identity.get("game_mode"), str)
        and bool(identity.get("game_mode").strip())
        and isinstance(identity.get("ascension"), int)
        and not isinstance(identity.get("ascension"), bool)
        and isinstance(run_id, str)
        and bool(run_id.strip())
        and (
            (isinstance(seed, str) and bool(seed.strip()))
            or (isinstance(seed, int) and not isinstance(seed, bool))
        )
    )


def read_verified_compendium(
    reader: Any,
    *,
    timeout_seconds: float = RUN_IDENTITY_READ_TIMEOUT_SECONDS,
    retry_seconds: float = RUN_IDENTITY_READ_RETRY_SECONDS,
    monotonic: Any = time.monotonic,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    """Read current-run identity, retrying known startup-partial responses.

    A healthy bridge does not guarantee that ``current_run.save`` has become
    visible yet.  This helper waits only a small, explicit amount of time and
    then raises; callers must never proceed with a cached partial identity.
    """
    timeout = max(0.0, float(timeout_seconds))
    retry = max(0.01, float(retry_seconds))
    started = monotonic()
    attempts = 0
    last_detail = "no response"
    while True:
        attempts += 1
        try:
            candidate = reader()
            if _compendium_identity_ready(candidate):
                return candidate
            current = candidate.get("current_run") if isinstance(candidate, dict) else None
            limitation = (
                (current.get("limitation") or candidate.get("limitation"))
                if isinstance(current, dict)
                else candidate.get("limitation")
                if isinstance(candidate, dict)
                else None
            )
            last_detail = (
                f"incomplete current_run identity (limitation={limitation!r})"
            )
        except Exception as exc:
            # The caller has already passed the health gate.  A short-lived
            # GET failure is treated like an incomplete read, but still fails
            # closed once the bounded retry window expires.
            last_detail = f"read error {type(exc).__name__}: {exc}"
        elapsed = monotonic() - started
        if elapsed >= timeout:
            raise BatchIntegrityError(
                "run identity compendium did not become complete within "
                f"{timeout:.1f}s after {attempts} attempts: {last_detail}"
            )
        sleep(min(retry, max(0.0, timeout - elapsed)))


class RunIdentityAudit:
    """Durable evidence that every observed active state is the target run.

    The comparison reader is deliberately read-only, so it cannot rely on a
    menu label or on the autoplay trace alone.  Each active bridge state is
    checked against the singleplayer endpoint and the authoritative
    ``current_run`` compendium record; the compact first/last/unique evidence
    is then written to both comparison artifacts.
    """

    schema_version = 1

    def __init__(self) -> None:
        self.status = "pending"
        self.observations = 0
        self.verified_observations = 0
        self.missing_fields: set[str] = set()
        self.conflicts: list[str] = []
        self.errors: list[str] = []
        self.first: dict[str, Any] | None = None
        self.last: dict[str, Any] | None = None
        self._identities: dict[str, dict[str, Any]] = {}
        self._run_ids: set[str] = set()

    @property
    def verified(self) -> bool:
        return (
            self.status == "verified"
            and self.observations > 0
            and self.verified_observations == self.observations
            and not self.missing_fields
            and not self.conflicts
            and not self.errors
        )

    def _fail(self, problems: list[str]) -> None:
        self.status = "failed"
        self.errors.extend(problem for problem in problems if problem not in self.errors)
        raise BatchIntegrityError(
            "run identity contract failed: "
            + "; ".join(self.errors[-10:])
        )

    def observe(
        self, state: dict[str, Any], compendium: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Validate one active state; unknown identity fails closed."""
        if not _active_run(state) or state.get("state_type") in {"menu", "game_over"}:
            return None
        if self.status == "failed":
            raise BatchIntegrityError("run identity audit is already failed")
        self.observations += 1
        live = _state_run_identity(state)
        saved = _compendium_run_identity(compendium)
        problems: list[str] = []

        character = live.get("character")
        if character is None:
            self.missing_fields.add("character")
        elif character != "IRONCLAD":
            problems.append(f"character={character!r}")
        saved_character = saved.get("character")
        if saved_character is not None and saved_character != "IRONCLAD":
            problems.append(f"compendium.character={saved_character!r}")
        if character is not None and saved_character is not None and character != saved_character:
            problems.append(
                f"character_conflict:{character!r}!={saved_character!r}"
            )

        ascension = live.get("ascension")
        if ascension is None:
            self.missing_fields.add("ascension")
        elif ascension != 10:
            problems.append(f"ascension={ascension!r}")
        saved_ascension = saved.get("ascension")
        if saved_ascension is None:
            self.missing_fields.add("compendium.ascension")
        elif saved_ascension != 10:
            problems.append(f"compendium.ascension={saved_ascension!r}")
        if (
            ascension is not None
            and saved_ascension is not None
            and ascension != saved_ascension
        ):
            problems.append(
                f"ascension_conflict:{ascension!r}!={saved_ascension!r}"
            )

        if saved.get("run_id") in (None, ""):
            self.missing_fields.add("compendium.run_id")
        if saved.get("seed") in (None, ""):
            self.missing_fields.add("compendium.seed")

        if saved.get("is_in_progress") is not True:
            if saved.get("is_in_progress") is None:
                self.missing_fields.add("compendium.is_in_progress")
            else:
                problems.append(
                    f"compendium.is_in_progress={saved.get('is_in_progress')!r}"
                )
        mode = saved.get("game_mode")
        if not isinstance(mode, str) or not mode.strip():
            self.missing_fields.add("game_mode")
        elif mode.strip().upper() != "STANDARD":
            problems.append(f"game_mode={mode!r}")
        live_mode = live.get("game_mode")
        if live_mode is not None and str(live_mode).strip().upper() != "STANDARD":
            problems.append(f"state.game_mode={live_mode!r}")
        if (
            live_mode is not None
            and isinstance(mode, str)
            and str(live_mode).strip().upper() != mode.strip().upper()
        ):
            problems.append(f"game_mode_conflict:{live_mode!r}!={mode!r}")

        if saved.get("is_multiplayer") is True or live.get("is_multiplayer") is True:
            problems.append("singleplayer=false")
        players = state.get("players")
        if isinstance(players, list) and len(players) != 1:
            problems.append(f"player_count={len(players)}")

        saved_run_id = saved.get("run_id")
        live_run_id = live.get("run_id")
        if saved_run_id not in (None, "") and live_run_id not in (None, ""):
            if not _identity_equal("run_id", saved_run_id, live_run_id):
                problems.append(f"run_id_conflict:{live_run_id!r}!={saved_run_id!r}")
        saved_seed = saved.get("seed")
        live_seed = live.get("seed")
        if saved_seed not in (None, "") and live_seed not in (None, ""):
            if not _identity_equal("seed", saved_seed, live_seed):
                problems.append(f"seed_conflict:{live_seed!r}!={saved_seed!r}")

        if self.missing_fields or problems:
            self.conflicts.extend(problem for problem in problems if problem not in self.conflicts)
            self._fail(sorted(self.missing_fields) + problems)

        identity = {
            "character_id": live.get("character_id") or "IRONCLAD",
            "character": "IRONCLAD",
            "character_label": live.get("character_label"),
            "ascension": 10,
            "game_mode": "standard",
            "singleplayer_verified": True,
            "run_id": live_run_id or saved_run_id,
            "seed": live_seed if live_seed not in (None, "") else saved_seed,
            "source": {
                "state": "GET /api/v1/singleplayer",
                "compendium": "GET /api/v1/compendium current_run",
            },
        }
        self.verified_observations += 1
        self.status = "verified"
        if self.first is None:
            self.first = dict(identity)
        self.last = dict(identity)
        key = json.dumps(identity, ensure_ascii=False, sort_keys=True)
        self._identities.setdefault(key, identity)
        run_id = identity.get("run_id")
        if run_id not in (None, ""):
            self._run_ids.add(str(run_id))
        return identity

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "verified": self.verified,
            "required": dict(RUN_IDENTITY_REQUIRED),
            "observations": self.observations,
            "verified_observations": self.verified_observations,
            "missing_fields": sorted(self.missing_fields),
            "conflicts": list(self.conflicts),
            "errors": list(self.errors),
            "first": self.first,
            "last": self.last,
            "unique_identities": list(self._identities.values()),
            "run_ids": sorted(self._run_ids),
            "singleplayer_source": "GET /api/v1/singleplayer",
            "compendium_source": "GET /api/v1/compendium current_run",
        }

    @classmethod
    def from_json(cls, payload: Any) -> "RunIdentityAudit":
        if not isinstance(payload, dict):
            raise BatchIntegrityError("run_identity artifact must be an object")
        if payload.get("schema_version") != cls.schema_version:
            raise BatchIntegrityError("unsupported run_identity artifact schema")
        required = payload.get("required")
        if required != RUN_IDENTITY_REQUIRED:
            raise BatchIntegrityError("run_identity required contract changed")
        audit = cls()
        audit.status = str(payload.get("status") or "pending")
        if audit.status not in {"pending", "verified", "failed"}:
            raise BatchIntegrityError("invalid run_identity status")
        for name in ("observations", "verified_observations"):
            value = payload.get(name, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise BatchIntegrityError(f"invalid run_identity {name}")
            setattr(audit, name, value)
        if audit.verified_observations > audit.observations:
            raise BatchIntegrityError("run_identity verified count exceeds observations")
        audit.missing_fields = set(
            value for value in payload.get("missing_fields", []) if isinstance(value, str)
        )
        audit.conflicts = [value for value in payload.get("conflicts", []) if isinstance(value, str)]
        audit.errors = [value for value in payload.get("errors", []) if isinstance(value, str)]
        audit.first = payload.get("first") if isinstance(payload.get("first"), dict) else None
        audit.last = payload.get("last") if isinstance(payload.get("last"), dict) else None
        for identity in payload.get("unique_identities", []):
            if isinstance(identity, dict):
                key = json.dumps(identity, ensure_ascii=False, sort_keys=True)
                audit._identities.setdefault(key, identity)
        audit._run_ids = set(value for value in payload.get("run_ids", []) if isinstance(value, str))
        if audit.status == "verified" and not audit.verified:
            raise BatchIntegrityError("verified run_identity artifact is not internally consistent")
        return audit


def invalid_record_seeds(
    records: list[BattleRecord], allowed: set[int]
) -> list[dict[str, Any]]:
    """Return every persisted record whose seed is not in the allocation."""
    invalid: list[dict[str, Any]] = []
    for record in records:
        seed = record.seed
        if not isinstance(seed, int) or isinstance(seed, bool) or seed not in allowed:
            invalid.append({"battle_id": record.battle_id, "seed": seed})
    return invalid


def validate_record_seeds(records: list[BattleRecord], allowed: set[int]) -> None:
    """Fail closed when restored/current records disagree with the partition."""
    invalid = invalid_record_seeds(records, allowed)
    if invalid:
        raise SeedContractError(
            "fixed-seed records contain missing, non-integer, or "
            "out-of-partition seeds: "
            + json.dumps(invalid[:10], ensure_ascii=False, sort_keys=True)
        )


def _sample_key_for_record(record: BattleRecord) -> str | None:
    """Build a stable identity for duplicate-sample protection on resume."""
    snapshots = [
        turn.snapshot
        for turn in record.turns
        if turn.snapshot is not None and turn.snapshot.state_hash
    ]
    if record.battle_snapshot is not None and record.battle_snapshot.state_hash:
        snapshots.insert(0, record.battle_snapshot)
    if snapshots:
        return f"state-hash:{snapshots[0].state_hash}"
    if record.seed is None:
        return None
    identity = {
        "seed": record.seed,
        "act": record.act,
        "floor": record.floor,
        "enemies": list(record.enemies),
        "hp_start": record.hp_start,
    }
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return f"run-fallback:{digest}"


def _sample_key_from_row(row: dict[str, Any]) -> str | None:
    explicit = row.get("sample_key")
    if isinstance(explicit, str) and explicit:
        return explicit
    snapshot = row.get("battle_snapshot")
    if isinstance(snapshot, dict):
        state_hash = snapshot.get("state_hash")
        if isinstance(state_hash, str) and state_hash:
            return f"state-hash:{state_hash}"
    return None


def load_battle_rows(path: Path) -> tuple[list[dict[str, Any]], set[str], set[str], int]:
    """Load the append-only battle journal and reject duplicate IDs/samples."""
    if not path.exists():
        return [], set(), set(), 0
    rows: list[dict[str, Any]] = []
    ids: set[str] = set()
    sample_keys: set[str] = set()
    unkeyed = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BatchIntegrityError(f"cannot read battle journal {path}: {exc}") from exc
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BatchIntegrityError(
                f"battle journal {path}:{line_number} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(row, dict) or not isinstance(row.get("battle_id"), str):
            raise BatchIntegrityError(
                f"battle journal {path}:{line_number} lacks a string battle_id"
            )
        battle_id = row["battle_id"]
        if battle_id in ids:
            raise BatchIntegrityError(f"duplicate battle_id {battle_id!r} in {path}")
        ids.add(battle_id)
        sample_key = _sample_key_from_row(row)
        if sample_key is not None:
            if sample_key in sample_keys:
                raise BatchIntegrityError(
                    f"duplicate sample_key {sample_key!r} in {path}"
                )
            sample_keys.add(sample_key)
        else:
            unkeyed += 1
        rows.append(row)
    return rows, ids, sample_keys, unkeyed


def _next_battle_sequence(ids: set[str], prefix: str) -> int:
    pattern = re.compile(rf"^{re.escape(prefix)}-(\d+)(?:-|$)")
    return max(
        (int(match.group(1)) for battle_id in ids if (match := pattern.match(battle_id))),
        default=0,
    )


def _serialize_record(record: BattleRecord) -> dict[str, Any]:
    def snapshot_payload(snapshot: SolverSnapshot | None) -> dict[str, Any] | None:
        return snapshot.to_json() if snapshot is not None else None

    turns = []
    for turn in record.turns:
        turns.append(
            {
                "turn": turn.turn,
                "decision_id": turn.decision_id,
                "snapshot": snapshot_payload(turn.snapshot),
                "failure": turn.failure.to_json() if turn.failure else None,
                "executed": turn.executed.to_json() if turn.executed else None,
                "route_step": turn.route_step.to_json() if turn.route_step else None,
                "actual_hp_end": turn.actual_hp_end,
                "deviation": (
                    {
                        "deviated": turn.deviation.deviated,
                        "first_divergence_index": turn.deviation.first_divergence_index,
                        "executed_len": turn.deviation.executed_len,
                        "route_len": turn.deviation.route_len,
                    }
                    if turn.deviation
                    else None
                ),
            }
        )
    return {
        "battle_id": record.battle_id,
        "run_id": record.run_id,
        "seed": record.seed,
        "act": record.act,
        "floor": record.floor,
        "enemies": list(record.enemies),
        "hp_start": record.hp_start,
        "hp_end": record.hp_end,
        "outcome": record.outcome,
        "turns": turns,
        "battle_snapshot": snapshot_payload(record.battle_snapshot),
        "failures": [failure.to_json() for failure in record.failures],
        "prediction_complete": record.prediction_complete,
        "heal_adjustment": record.heal_adjustment,
    }


def _restore_record(payload: Any) -> BattleRecord:
    if not isinstance(payload, dict):
        raise BatchIntegrityError("records checkpoint contains a non-object record")
    try:
        turns: list[TurnOutcome] = []
        for item in payload.get("turns", []):
            if not isinstance(item, dict):
                raise ValueError("turn must be an object")
            executed_payload = item.get("executed")
            executed = None
            if executed_payload is not None:
                executed = ExecutedTurn(
                    turn=int(executed_payload["turn"]),
                    actions=tuple(
                        RouteAction(**action)
                        for action in executed_payload.get("actions", [])
                    ),
                    ambiguous=bool(executed_payload["ambiguous"]),
                    notes=tuple(executed_payload.get("notes", [])),
                    source=str(executed_payload.get("source", "inferred")),
                )
            route_payload = item.get("route_step")
            route_step = (
                parse_route_step(route_payload, "checkpoint.route_step")
                if route_payload
                else None
            )
            snapshot_payload = item.get("snapshot")
            snapshot = snapshot_from_json(snapshot_payload) if snapshot_payload else None
            failure_payload = item.get("failure")
            failure = failure_from_json(failure_payload) if failure_payload else None
            outcome = TurnOutcome(
                turn=int(item["turn"]),
                snapshot=snapshot,
                failure=failure,
                executed=executed,
                route_step=route_step,
                actual_hp_end=item.get("actual_hp_end"),
                decision_id=(
                    str(item["decision_id"])
                    if item.get("decision_id") not in (None, "")
                    else None
                ),
            )
            deviation_payload = item.get("deviation")
            if deviation_payload is not None:
                outcome.deviation = DeviationInfo(
                    deviated=bool(deviation_payload["deviated"]),
                    first_divergence_index=deviation_payload.get("first_divergence_index"),
                    executed_len=int(deviation_payload["executed_len"]),
                    route_len=int(deviation_payload["route_len"]),
                )
            turns.append(outcome)
        battle_snapshot_payload = payload.get("battle_snapshot")
        failures = [failure_from_json(item) for item in payload.get("failures", [])]
        return BattleRecord(
            battle_id=str(payload["battle_id"]),
            run_id=(
                str(payload["run_id"])
                if payload.get("run_id") not in (None, "")
                else None
            ),
            seed=payload.get("seed"),
            act=payload.get("act"),
            floor=payload.get("floor"),
            enemies=tuple(payload.get("enemies", [])),
            hp_start=int(payload["hp_start"]),
            hp_end=int(payload["hp_end"]),
            outcome=str(payload["outcome"]),
            turns=turns,
            battle_snapshot=(
                snapshot_from_json(battle_snapshot_payload)
                if battle_snapshot_payload
                else None
            ),
            failures=failures,
            prediction_complete=bool(payload.get("prediction_complete", False)),
            heal_adjustment=payload.get("heal_adjustment"),
        )
    except (KeyError, TypeError, ValueError, SnapshotError) as exc:
        raise BatchIntegrityError(f"invalid records checkpoint entry: {exc}") from exc


def load_records_checkpoint(path: Path) -> list[BattleRecord]:
    if not path.exists():
        return []
    payload = _load_json(path)
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise BatchIntegrityError(f"unsupported records checkpoint schema in {path}")
    raw_records = payload.get("records")
    if not isinstance(raw_records, list):
        raise BatchIntegrityError(f"records checkpoint {path} lacks records[]")
    records = [_restore_record(item) for item in raw_records]
    ids = [record.battle_id for record in records]
    if len(ids) != len(set(ids)):
        raise BatchIntegrityError(f"duplicate battle_id in records checkpoint {path}")
    return records


def _row_stub(row: dict[str, Any]) -> BattleRecord:
    """Conservative fallback for pre-v2 rows without a full record payload."""
    return BattleRecord(
        battle_id=str(row["battle_id"]),
        run_id=(str(row["run_id"]) if row.get("run_id") not in (None, "") else None),
        seed=row.get("seed"),
        act=row.get("act"),
        floor=row.get("floor"),
        enemies=tuple(row.get("enemies") or ()),
        hp_start=int(row.get("hp_start", 0)),
        hp_end=int(row.get("hp_end", row.get("hp_start", 0))),
        outcome=str(row.get("outcome", "unknown")),
    )


def recover_records_from_journal(
    rows: list[dict[str, Any]], checkpoint_records: list[BattleRecord]
) -> tuple[list[BattleRecord], bool]:
    """Reconcile a journal with a possibly stale aggregate checkpoint.

    The journal append is fsynced before the checkpoint replace.  If a process
    dies in that window, rows can legitimately outnumber checkpoint records;
    embedded ``record_checkpoint`` payloads restore the exact missing records.
    The boolean reports whether any legacy row required a conservative stub.
    """
    journal_ids = {str(row["battle_id"]) for row in rows}
    checkpoint_ids = {record.battle_id for record in checkpoint_records}
    if checkpoint_ids - journal_ids:
        raise BatchIntegrityError(
            "records checkpoint contains battle IDs absent from battles.jsonl"
        )
    records = list(checkpoint_records)
    legacy = False
    for row in rows:
        battle_id = str(row["battle_id"])
        if battle_id in checkpoint_ids:
            continue
        embedded = row.get("record_checkpoint")
        if isinstance(embedded, dict):
            if embedded.get("battle_id") != battle_id:
                raise BatchIntegrityError(
                    f"record_checkpoint battle_id disagrees with journal row {battle_id!r}"
                )
            records.append(_restore_record(embedded))
        else:
            legacy = True
            records.append(_row_stub(row))
    if len({record.battle_id for record in records}) != len(records):
        raise BatchIntegrityError("duplicate battle_id after journal/checkpoint recovery")
    by_id = {record.battle_id: record for record in records}
    for row in rows:
        record = by_id[str(row["battle_id"])]
        # The top-level journal row is the audit-facing projection while the
        # embedded payload is the exact aggregation record.  A disagreement
        # indicates artifact tampering or a partial write and must not be
        # silently resolved by choosing one side.
        if "seed" in row and row.get("seed") != record.seed:
            raise BatchIntegrityError(
                f"journal/checkpoint seed disagrees for battle_id {record.battle_id!r}"
            )
    return records, legacy


def write_records_checkpoint(path: Path, records: list[BattleRecord]) -> None:
    write_json(
        path,
        {"schema_version": 1, "records": [_serialize_record(record) for record in records]},
    )


def _open_battle_payload(tracker: BattleTracker | None) -> dict[str, Any] | None:
    battle = getattr(tracker, "_open", None) if tracker is not None else None
    if battle is None:
        return None
    return {
        "battle_id": battle.battle_id,
        "run_id": battle.run_id,
        "seed": battle.seed,
        "act": battle.act,
        "floor": battle.floor,
        "enemies": list(battle.enemies),
        "hp_start": battle.hp_start,
        "anchor_turn": battle.anchor.turn,
        "turns_closed": len(battle.turns),
        "opened_at_utc": battle.opened_at_utc,
    }


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def main(argv: list[str] | None = None) -> int:
    """Run or explicitly resume one comparison batch.

    Lifecycle, seed-contract, and journal policy live here so the comparison
    engine remains a pure metrics module and persisted artifacts stay the
    source of truth across interruptions.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-id", default=None)
    parser.add_argument("--config", default="config/combat_solver.toml")
    parser.add_argument("--live-lock", default="config/live_version.lock.json")
    parser.add_argument("--solver-lock", default="config/combat_solver.lock.json")
    parser.add_argument("--seeds-file", default="data/combat_solver/fixed_battle_seeds.json")
    parser.add_argument("--max-battles", type=int, default=None)
    parser.add_argument("--max-seconds", type=int, default=None)
    parser.add_argument("--bridge-grace-seconds", type=float, default=30.0)
    parser.add_argument("--base-url", default=None)
    parser.add_argument(
        "--reader-mode",
        default=None,
        choices=["jsonl", "logtail", "directory"],
        help="override config [reader].mode",
    )
    parser.add_argument(
        "--seed-mode",
        choices=SEED_MODES,
        default="fixed",
        help="fixed verifies live run.seed against the allocation; observational "
        "is an explicit non-acceptance downgrade for bridges without seed support",
    )
    parser.add_argument(
        "--observational",
        dest="seed_mode",
        action="store_const",
        const="observational",
        help="alias for --seed-mode observational",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="continue an existing batch directory; refuse duplicate IDs/samples",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--automated",
        action="store_true",
        help="battles are played by the solver's full-auto/execute mode; "
        "executed actions come from its DEPLOY log, and the batch is "
        "permanently tagged automated=true (never counts as no-SL play)",
    )
    args = parser.parse_args(argv)

    if args.max_battles is not None and args.max_battles <= 0:
        parser.error("--max-battles must be positive")
    if args.max_seconds is not None and args.max_seconds <= 0:
        parser.error("--max-seconds must be positive")
    if args.bridge_grace_seconds < 0:
        parser.error("--bridge-grace-seconds cannot be negative")

    batch_id = args.batch_id or f"csb-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    out_dir = PROJECT_ROOT / "runs" / "combat_solver_compare" / batch_id
    if out_dir.exists() and any(out_dir.iterdir()) and not args.resume:
        raise SystemExit(
            f"batch directory already contains data: {out_dir}; use --resume "
            "to continue it explicitly"
        )
    out_dir.mkdir(parents=True, exist_ok=True)

    started_at = _utc_now()
    config: dict[str, Any] | None = None
    live_lock: VersionLock | None = None
    solver_lock: VersionLock | None = None
    seeds_path = PROJECT_ROOT / args.seeds_file
    partition: dict[str, Any] | None = None
    partition_seeds: set[int] = set()
    game_observed: dict[str, Any] | None = None
    inventory: dict[str, Any] | None = None
    reader: Any = None
    tracker: BattleTracker | None = None
    recorder: TraceRecorder | None = None
    comparison_cfg: dict[str, Any] = {}
    existing_rows: list[dict[str, Any]] = []
    existing_ids: set[str] = set()
    existing_sample_keys: set[str] = set()
    unkeyed_existing = 0
    records: list[BattleRecord] = []
    seed_audit = SeedAudit(args.seed_mode, None, set())
    identity_audit = RunIdentityAudit()
    stopped_reason = "running"
    error_message: str | None = None
    resume_legacy = False
    dry_run = bool(args.dry_run)
    checkpoint_path = out_dir / CHECKPOINT_FILE
    battles_path = out_dir / "battles.jsonl"
    old_manifest: dict[str, Any] = {}
    initial_record_count = 0
    max_battles = 100

    def _status_for(stopped: str, error: str | None, final: bool) -> str:
        if not final:
            return "running"
        if dry_run:
            return "dry_run"
        if error:
            return "failed" if not records else "partial"
        if stopped == "max_battles":
            return "complete"
        return "partial"

    def _persist(final: bool) -> None:
        current_status = _status_for(stopped_reason, error_message, final)
        summary = aggregate_battles(records)
        gate_values, gate_provenance = resolve_gate_config(config, args.automated)
        gate_results = [g.to_json() for g in evaluate_gates(summary, gate_values)]
        min_battles = int(comparison_cfg.get("min_battles", 50))
        decidable = len(records) >= min_battles
        record_seed_errors = invalid_record_seeds(records, partition_seeds)
        # Never inherit a fixed claim from an old audit unless every restored
        # record independently satisfies the current partition contract.
        seed_verified = seed_audit.fixed_seed_verified and not record_seed_errors
        run_identity = identity_audit.to_json()
        identity_verified = identity_audit.verified
        blockers: list[str] = []
        if not seed_verified:
            blockers.append(
                "fixed_seed_unverified"
                if args.seed_mode == "fixed"
                else "observational_mode"
            )
        if unkeyed_existing:
            blockers.append("legacy_samples_without_stable_sample_key")
        if resume_legacy:
            blockers.append("legacy_records_without_full_checkpoint")
        if not identity_verified:
            blockers.append("run_identity_unverified")
        verdict = {
            "accepted": (
                current_status == "complete"
                and decidable
                and seed_verified
                and not unkeyed_existing
                and not resume_legacy
                and identity_verified
                and all(gate["passed"] for gate in gate_results)
            ),
            "decidable": decidable,
            "n_battles": len(records),
            "existing_battles": initial_record_count,
            "min_battles": min_battles,
            "stopped_reason": stopped_reason,
            "automated": args.automated,
            "seed_mode": args.seed_mode,
            "run_identity_verified": identity_verified,
                "fixed_seed_verified": seed_verified,
                "record_seed_integrity": {
                    "valid": not record_seed_errors,
                    "invalid": record_seed_errors,
                },
                "acceptance_blockers": blockers,
            "tracker": tracker.stats() if tracker is not None else None,
        }
        summary_payload = {
            **summary,
            "batch": {
                "batch_id": batch_id,
                "status": current_status,
                "resume": bool(args.resume),
                "new_battles": max(0, len(records) - initial_record_count),
                "seed_mode": args.seed_mode,
                "fixed_seed_claim": seed_verified,
                "stopped_reason": stopped_reason,
                "error": error_message,
            },
            "gates": gate_results,
            "gate_provenance": gate_provenance,
            "run_identity": run_identity,
            "verdict": verdict,
        }
        manifest = {
            "schema_version": 2,
            "batch_id": batch_id,
            "track": "combat_solver_comparison",
            "status": current_status,
            "automated": args.automated,
            "started_at_utc": started_at,
            "completed_at_utc": _utc_now() if final else None,
            "error": error_message,
            "game_observed": game_observed,
            "mod_inventory": inventory,
            "seeds_file": str(seeds_path),
            "seeds_file_sha256": _sha256(seeds_path),
            # This is a plan only.  ``seeds_partition`` is populated only when
            # actual run.seed values have been observed and verified.
            "planned_seed_partition": partition,
            "seeds_partition": partition if seed_verified else None,
            "seed_mode": args.seed_mode,
            "fixed_seed_claim": seed_verified,
            "seed_audit": seed_audit.to_json(),
            "run_identity": run_identity,
            "record_seed_integrity": {
                "valid": not record_seed_errors,
                "invalid": record_seed_errors,
            },
            "gate_provenance": gate_provenance,
            "config": config,
            "git_head": _git_head(),
            "reader": reader.name if reader is not None else None,
            "resume": {
                "requested": bool(args.resume),
                "existing_battles": initial_record_count,
                "existing_manifest": bool(old_manifest),
                "legacy_checkpoint": resume_legacy,
                "next_battle_sequence": _next_battle_sequence(
                    existing_ids,
                    str(comparison_cfg.get("battle_id_prefix", "csb")),
                ),
            },
            "journal": {
                "battles_path": str(battles_path),
                "records_checkpoint": str(checkpoint_path),
                "unkeyed_existing": unkeyed_existing,
                "open_battle": _open_battle_payload(tracker),
            },
            "verdict": verdict,
        }
        write_json(out_dir / "summary.json", summary_payload)
        write_json(out_dir / "manifest.json", manifest)

    try:
        existing_rows, existing_ids, existing_sample_keys, unkeyed_existing = load_battle_rows(
            battles_path
        )
        old_manifest_path = out_dir / "manifest.json"
        if old_manifest_path.exists():
            old_manifest = _load_json(old_manifest_path)
            if not isinstance(old_manifest, dict):
                raise BatchIntegrityError("existing manifest must be an object")
            if old_manifest.get("batch_id") not in (None, batch_id):
                raise BatchIntegrityError("existing manifest belongs to a different batch_id")
            if args.resume and isinstance(old_manifest.get("started_at_utc"), str):
                # A resumed batch keeps its original start boundary; the new
                # invocation is represented by the resume fields instead.
                started_at = old_manifest["started_at_utc"]
            if args.resume and old_manifest.get("run_identity") is not None:
                # Restore only a schema-validated identity audit.  A resumed
                # batch must not silently lose the evidence for its earlier
                # active states, nor may it inherit arbitrary fields.
                identity_audit = RunIdentityAudit.from_json(
                    old_manifest.get("run_identity")
                )

        config = load_config(PROJECT_ROOT / args.config)
        live_lock = VersionLock.load(PROJECT_ROOT / args.live_lock)
        solver_lock = VersionLock.load(PROJECT_ROOT / args.solver_lock)
        seeds_payload = _load_json(seeds_path)
        partition, partition_seeds = validate_seed_payload(seeds_payload)
        seed_audit = SeedAudit(args.seed_mode, partition, partition_seeds)
        if old_manifest:
            old_mode = old_manifest.get("seed_mode")
            if old_mode is not None and old_mode != args.seed_mode:
                raise BatchIntegrityError(
                    f"cannot resume batch with seed mode {args.seed_mode!r}; "
                    f"existing manifest is {old_mode!r}"
                )
            old_hash = old_manifest.get("seeds_file_sha256")
            current_hash = _sha256(seeds_path)
            if old_hash and current_hash and old_hash != current_hash:
                raise BatchIntegrityError(
                    "seed file hash changed; choose a new batch-id instead of mixing allocations"
                )
            old_audit = old_manifest.get("seed_audit")
            if isinstance(old_audit, dict):
                # Preserve verified evidence when a completed batch is opened
                # only to inspect/resume; new observations are added below.
                seed_audit.observed.update(
                    value for value in old_audit.get("observed_seeds", []) if isinstance(value, int)
                )
                seed_audit.outside_partition.update(
                    value for value in old_audit.get("outside_partition", []) if isinstance(value, int)
                )
                seed_audit.missing_observations = int(old_audit.get("missing_observations", 0) or 0)
                seed_audit.observations = int(old_audit.get("observations", 0) or 0)

        checkpoint_records = load_records_checkpoint(checkpoint_path)
        records, resume_legacy = recover_records_from_journal(
            existing_rows, checkpoint_records
        )
        if args.seed_mode == "fixed":
            # Do this before any bridge connection so a poisoned checkpoint
            # cannot inherit an old fixed-seed audit and be relabeled clean.
            validate_record_seeds(records, partition_seeds)
        initial_record_count = len(records)
        if existing_rows and len(records) == len(existing_ids) and (
            not checkpoint_records or len(records) != len(checkpoint_records)
        ):
            # Materialize any journal recovery before touching the bridge so a
            # second interruption starts from a complete checkpoint as well.
            write_records_checkpoint(checkpoint_path, records)

        # Refuse a mode change even when the old manifest predates the seed
        # contract fields: an explicit --resume is never a way to relabel old
        # observational evidence as fixed-seed evidence.
        cross_check_game_identity(live_lock, solver_lock)
        game_observed = live_lock.verify_installed_game()
        inventory = verify_solver_inventory(solver_lock)
        reader = build_reader(config, PROJECT_ROOT, args.reader_mode)
        comparison_cfg = config.get("comparison") or {}
        max_battles = args.max_battles
        if max_battles is None:
            max_battles = int(comparison_cfg.get("max_battles", 100))
        min_battles = int(comparison_cfg.get("min_battles", 50))
        if max_battles <= 0 or min_battles <= 0:
            raise BatchIntegrityError("comparison max_battles/min_battles must be positive")
        poll_seconds = float((config.get("poll") or {}).get("interval_seconds", 0.25))
        prefix = str(comparison_cfg.get("battle_id_prefix", "csb"))

        print(
            json.dumps(
                {
                    "batch_id": batch_id,
                    "game": game_observed,
                    "mods": inventory["mods"],
                    "planned_seed_partition": partition,
                    "seed_mode": args.seed_mode,
                    "max_battles": max_battles,
                    "reader": reader.name,
                    "automated": args.automated,
                    "resume": args.resume,
                    "dry_run": args.dry_run,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        _persist(final=False)
        if args.dry_run:
            stopped_reason = "dry_run"
            _persist(final=True)
            print("dry-run: environment verified, no game connection attempted")
            return 0
        if len(existing_ids) >= max_battles:
            stopped_reason = "max_battles"
            _persist(final=True)
            print("resume: batch already reached max_battles; no samples replayed")
            return 0

        # Imported late so --dry-run never touches the live bridge module.
        from bridge.client import BridgeClient, BridgeError

        base_url = args.base_url or str(live_lock.bridge["base_url"])
        client = BridgeClient(
            base_url=base_url,
            state_path=str(live_lock.bridge["singleplayer_path"]),
            health_path=str(live_lock.bridge["health_path"]),
            compendium_path=str(
                live_lock.bridge.get("compendium_path", "/api/v1/compendium")
            ),
        )
        compendium_cache: dict[str, Any] | None = None
        compendium_cache_key: tuple[Any, Any] | None = None
        # Publish a running manifest before waiting for the game.  A bridge or
        # game exit during startup is therefore resumable and visible.
        deadline = time.monotonic() + 180
        while True:
            try:
                health = client._get(str(live_lock.bridge["health_path"]))
                live_lock.verify_bridge_health(health)
                break
            except (BridgeError, VersionLockError) as exc:
                if time.monotonic() > deadline:
                    stopped_reason = "bridge_start_timeout"
                    raise BatchIntegrityError(f"bridge did not become healthy: {exc}") from exc
                print(f"waiting for bridge: {exc}", flush=True)
                time.sleep(2.0)

        recorder = TraceRecorder(
            out_dir / "trace.jsonl",
            metadata={
                "batch_id": batch_id,
                "track": "combat_solver_comparison",
                "automated": args.automated,
                "resume": args.resume,
                "seed_mode": args.seed_mode,
                "game": game_observed,
                "mods": inventory["mods"],
            },
        )
        tracker = BattleTracker(
            source=reader,
            battle_id_prefix=prefix,
            battle_id_start=_next_battle_sequence(existing_ids, prefix),
        )
        started = time.monotonic()
        bridge_lost_since: float | None = None
        bridge_last_error: str | None = None
        old_sigterm = signal.getsignal(signal.SIGTERM)

        def _handle_stop(signum: int, _frame: Any) -> None:
            raise _StopBatch("signal" if signum == signal.SIGTERM else "interrupt")

        signal.signal(signal.SIGTERM, _handle_stop)
        try:
            while len(existing_ids) < max_battles:
                if args.max_seconds and time.monotonic() - started > args.max_seconds:
                    stopped_reason = "max_seconds"
                    break
                try:
                    state = client.get_state()
                    bridge_lost_since = None
                    bridge_last_error = None
                except BridgeError as exc:
                    now = time.monotonic()
                    bridge_lost_since = bridge_lost_since or now
                    bridge_last_error = str(exc)
                    print(f"bridge unavailable ({exc}); retrying", flush=True)
                    if now - bridge_lost_since >= args.bridge_grace_seconds:
                        stopped_reason = "bridge_disconnected"
                        error_message = (
                            f"bridge unavailable for {now - bridge_lost_since:.1f}s: "
                            f"{bridge_last_error}"
                        )
                        break
                    time.sleep(1.0)
                    continue
                recorder.write("state", state, decision_id=decision_id(state))
                seed_audit.observe(state)
                if _active_run(state) and state.get("state_type") not in {"menu", "game_over"}:
                    run = state.get("run") if isinstance(state.get("run"), dict) else {}
                    cache_key = (
                        _first_nonempty(run.get("run_id"), state.get("run_id")),
                        _first_nonempty(run.get("seed"), state.get("seed")),
                    )
                    if compendium_cache is None or cache_key != compendium_cache_key:
                        compendium_cache = read_verified_compendium(
                            client.get_compendium,
                        )
                        compendium_cache_key = cache_key
                    identity = identity_audit.observe(state, compendium_cache)
                    if identity is not None:
                        recorder.write(
                            "run_identity", identity,
                            decision_id=decision_id(state),
                        )
                closed = tracker.feed(state)
                if closed is not None:
                    if args.seed_mode == "fixed":
                        validate_record_seeds([closed], partition_seeds)
                    sample_key = _sample_key_for_record(closed)
                    if closed.battle_id in existing_ids:
                        raise BatchIntegrityError(
                            f"refusing duplicate battle_id {closed.battle_id!r} on resume"
                        )
                    if sample_key is not None and sample_key in existing_sample_keys:
                        raise BatchIntegrityError(
                            f"refusing duplicate sample {sample_key!r} on resume"
                        )
                    row = closed.to_json()
                    row["sample_key"] = sample_key
                    # Keep a complete, self-contained record in the durable
                    # journal line.  This closes the crash window between the
                    # fsynced battles append and the aggregate checkpoint: a
                    # resumed process can reconstruct the exact record even
                    # when it died before records_checkpoint.json was replaced.
                    row["record_checkpoint"] = _serialize_record(closed)
                    row["sample_key_strength"] = (
                        "state_hash"
                        if sample_key and sample_key.startswith("state-hash:")
                        else "run_fallback"
                        if sample_key
                        else "unavailable"
                    )
                    _append_jsonl(battles_path, row)
                    existing_ids.add(closed.battle_id)
                    if sample_key is not None:
                        existing_sample_keys.add(sample_key)
                    records.append(closed)
                    write_records_checkpoint(checkpoint_path, records)
                    # This checkpoint is intentionally after the fsynced
                    # journal append; on restart a journal row is never lost
                    # because a checkpoint got ahead of it.
                    _persist(final=False)
                    stats = closed.turn_stats()
                    print(
                        f"[{closed.battle_id}] hp {closed.hp_start}->{closed.hp_end} "
                        f"turns={stats['turns_total']} deviated={stats['turns_deviated']}",
                        flush=True,
                    )
                time.sleep(poll_seconds)
        finally:
            signal.signal(signal.SIGTERM, old_sigterm)
    except KeyboardInterrupt:
        stopped_reason = "interrupt"
    except _StopBatch as exc:
        stopped_reason = exc.reason
    except SeedContractError as exc:
        stopped_reason = "seed_contract"
        error_message = str(exc)
        print(f"seed contract blocked batch: {exc}", file=sys.stderr)
    except Exception as exc:
        if stopped_reason == "running":
            stopped_reason = "abnormal_termination"
        error_message = f"{type(exc).__name__}: {exc}"
        print(f"batch stopped: {error_message}", file=sys.stderr)
    finally:
        try:
            _persist(final=True)
        except Exception as exc:
            print(f"could not publish batch artifacts: {exc}", file=sys.stderr)
            if error_message is None:
                error_message = f"artifact publication failed: {exc}"

    final_status = _status_for(stopped_reason, error_message, True)
    final_verdict = {
        "summary": str(out_dir / "summary.json"),
        "manifest": str(out_dir / "manifest.json"),
        "status": final_status,
        "stopped_reason": stopped_reason,
        "error": error_message,
    }
    print(json.dumps(final_verdict, ensure_ascii=False))
    return 1 if error_message or stopped_reason in {
        "seed_contract",
        "bridge_disconnected",
        "bridge_start_timeout",
        "abnormal_termination",
    } else 0


if __name__ == "__main__":
    raise SystemExit(main())
