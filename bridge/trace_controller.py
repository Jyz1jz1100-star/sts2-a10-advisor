"""Version-locked STS2MCP trace recorder and opt-in controller.

The default CLI mode performs GET requests only.  POST is unavailable unless
the process was created with ``--allow-actions``.  Every observed state and any
opted-in action/result pair is preserved as raw JSONL for parity work.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK_PATH = PROJECT_ROOT / "config" / "live_version.lock.json"
DEFAULT_TRACE_DIR = PROJECT_ROOT / "runs" / "live_traces"


class BridgeProtocolError(RuntimeError):
    """The local bridge was unavailable or violated its JSON contract."""


class ActionPermissionError(PermissionError):
    """A POST was attempted without the explicit action capability."""


class VersionLockError(RuntimeError):
    """The installed game or bridge differs from the locked evaluation build."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def decision_id(state: dict[str, Any]) -> str:
    """Return the server decision id, or a conservative hash of the raw state.

    STS2MCP v0.4.0 does not expose a decision id.  Hashing the full canonical
    state is intentionally conservative: any visible-state change invalidates
    a pending action instead of risking a stale POST.
    """
    direct = state.get("decision_id")
    if isinstance(direct, (str, int)) and str(direct):
        return str(direct)
    nested = state.get("decision")
    if isinstance(nested, dict):
        nested_id = nested.get("id") or nested.get("decision_id")
        if isinstance(nested_id, (str, int)) and str(nested_id):
            return str(nested_id)
    canonical = json.dumps(
        state, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return "local-sha256:" + hashlib.sha256(canonical).hexdigest()


def canonicalize_game_seed(seed: str) -> str:
    """Mirror v0.111.0 ``SeedHelper.CanonicalizeSeed`` for verification.

    The compatibility bridge still records the caller's raw seed.  The game
    trims whitespace, uppercases the seed, and disambiguates the two letters
    that are not in its displayed alphabet (``O`` -> ``0`` and ``I`` -> ``1``)
    before constructing ``RunRngSet``.  This helper is deliberately limited to
    that stable, observed v0.111.0 transformation; it never hashes or converts
    a seed to an integer.
    """
    if not isinstance(seed, str):
        raise TypeError("seed must be a string")
    return seed.strip().upper().replace("O", "0").replace("I", "1")


def validate_requested_seed(seed: str) -> str:
    """Validate and return the game's canonical seed without mutating input."""
    canonical = canonicalize_game_seed(seed)
    if not canonical or any(
        not ("0" <= character <= "9" or "A" <= character <= "Z")
        for character in canonical
    ):
        raise ValueError("seed must be a non-empty ASCII alphanumeric string")
    return canonical


def _nonempty(value: Any) -> Any | None:
    if value is None or value is False:
        return None
    if isinstance(value, str):
        return value if value.strip() else None
    return value


def _as_metadata_int(value: Any, field: str) -> int | None:
    value = _nonempty(value)
    if value is None:
        return None
    if isinstance(value, bool):
        raise BridgeProtocolError(f"run identity field {field!r} is boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise BridgeProtocolError(f"run identity field {field!r} is not an integer")


def _canonical_character(value: Any) -> str | None:
    value = _nonempty(value)
    if value is None:
        return None
    token = str(value).strip().upper()
    if token in {"铁甲战士", "THE IRONCLAD", "IRONCLAD"}:
        return "IRONCLAD"
    return token


def _canonical_game_mode(value: Any) -> str | None:
    value = _nonempty(value)
    if value is None:
        return None
    return str(value).strip().lower()


def merge_verified_run_identity(
    state: dict[str, Any],
    compendium: dict[str, Any],
    *,
    requested_seed: str | None = None,
    canonical_seed: str | None = None,
) -> dict[str, Any]:
    """Merge live state and authoritative save identity without guessing.

    The live state is the source for the already-verified character and run
    metadata; ``current_run`` is the source for the saved seed and run id.  If
    both expose a field they must agree.  Missing fields are an error here so
    callers can decide whether to poll or fail closed; no run id/seed is
    derived from a timestamp, hash, or action payload.
    """
    live_run = state.get("run")
    if not isinstance(live_run, dict):
        live_run = {}
    live_player = state.get("player")
    if not isinstance(live_player, dict):
        live_player = {}
    saved_run = compendium.get("current_run")
    if not isinstance(saved_run, dict):
        saved_run = {}
    if saved_run.get("is_in_progress") is not True:
        raise BridgeProtocolError("authoritative current_run is not in progress")

    live_run_id = _nonempty(live_run.get("run_id") or state.get("run_id"))
    saved_run_id = _nonempty(saved_run.get("run_id"))
    if live_run_id is not None and saved_run_id is not None and live_run_id != saved_run_id:
        raise BridgeProtocolError(
            f"run identity conflict: live run_id={live_run_id!r}, "
            f"saved run_id={saved_run_id!r}"
        )
    run_id = saved_run_id or live_run_id
    if not isinstance(run_id, str) or not run_id.strip():
        raise BridgeProtocolError("authoritative run identity has no run_id")

    live_seed = _nonempty(live_run.get("seed"))
    saved_seed = _nonempty(saved_run.get("seed"))
    if live_seed is not None and saved_seed is not None and live_seed != saved_seed:
        raise BridgeProtocolError(
            f"run identity conflict: live seed={live_seed!r}, "
            f"saved seed={saved_seed!r}"
        )
    seed = saved_seed or live_seed
    if not isinstance(seed, str) or not seed.strip():
        raise BridgeProtocolError("authoritative run identity has no string seed")
    if requested_seed is not None and seed != canonical_seed:
        raise BridgeProtocolError(
            f"seed mismatch: requested canonical {canonical_seed!r}, observed {seed!r}"
        )

    live_character_id = _nonempty(live_player.get("character_id"))
    live_character_title = _nonempty(live_player.get("character"))
    live_character = live_character_id or live_character_title
    saved_character = _nonempty(
        saved_run.get("character_id") or saved_run.get("character")
    )
    if (
        live_character is not None
        and saved_character is not None
        and _canonical_character(live_character) != _canonical_character(saved_character)
    ):
        raise BridgeProtocolError(
            f"run identity conflict: live character={live_character!r}, "
            f"saved character={saved_character!r}"
        )
    character = live_character or saved_character
    if character is None:
        raise BridgeProtocolError("verified live state has no character")

    live_ascension = _as_metadata_int(live_run.get("ascension"), "ascension")
    saved_ascension = _as_metadata_int(saved_run.get("ascension"), "ascension")
    if (
        live_ascension is not None
        and saved_ascension is not None
        and live_ascension != saved_ascension
    ):
        raise BridgeProtocolError(
            f"run identity conflict: live ascension={live_ascension}, "
            f"saved ascension={saved_ascension}"
        )
    ascension = saved_ascension if saved_ascension is not None else live_ascension
    if ascension is None:
        raise BridgeProtocolError("run identity has no ascension")

    live_mode = _canonical_game_mode(
        state.get("game_mode") or live_run.get("game_mode")
    )
    saved_mode = _canonical_game_mode(saved_run.get("game_mode"))
    if live_mode is not None and saved_mode is not None and live_mode != saved_mode:
        raise BridgeProtocolError(
            f"run identity conflict: live game_mode={live_mode!r}, "
            f"saved game_mode={saved_mode!r}"
        )
    game_mode = saved_mode or live_mode
    if game_mode is None:
        raise BridgeProtocolError("run identity has no game_mode")

    identity: dict[str, Any] = {
        "run_id": run_id,
        "seed": seed,
        "character": character,
        "ascension": ascension,
        "game_mode": game_mode,
    }
    if live_character_id is not None:
        identity["character_id"] = live_character_id
    if live_character_title is not None:
        identity["character_title"] = live_character_title
    save_scope = _nonempty(saved_run.get("save_scope"))
    if save_scope is not None:
        identity["save_scope"] = save_scope
    if requested_seed is not None:
        identity["seed_requested"] = requested_seed
        identity["seed_canonical"] = canonical_seed
    return identity


@dataclass(frozen=True)
class VersionLock:
    raw: dict[str, Any]

    @classmethod
    def load(cls, path: Path) -> "VersionLock":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VersionLockError(f"Cannot read version lock {path}: {exc}") from exc
        if raw.get("schema_version") != 1:
            raise VersionLockError("Unsupported live version lock schema")
        return cls(raw=raw)

    @property
    def game(self) -> dict[str, Any]:
        return self.raw["game"]

    @property
    def bridge(self) -> dict[str, Any]:
        return self.raw["bridge"]

    def verify_installed_game(self) -> dict[str, Any]:
        release_path = Path(self.game["release_info_path"])
        manifest_path = Path(self.game["steam_manifest_path"])
        try:
            release = json.loads(release_path.read_text(encoding="utf-8"))
            manifest_text = manifest_path.read_text(encoding="utf-8")
        except (OSError, json.JSONDecodeError) as exc:
            raise VersionLockError(f"Cannot inspect installed game: {exc}") from exc

        def manifest_value(name: str) -> str | None:
            match = re.search(rf'"{re.escape(name)}"\s+"([^"]+)"', manifest_text)
            return match.group(1) if match else None

        observed = {
            "version": release.get("version"),
            "commit": release.get("commit"),
            "main_assembly_hash": release.get("main_assembly_hash"),
            "steam_build_id": manifest_value("buildid"),
            "branch": manifest_value("BetaKey") or "public",
        }
        # The release file does not contain Steam's app id.  It is stable
        # lock metadata, so carry it into the recorded observation when the
        # lock declares it; otherwise downstream acceptance checks would
        # reject an otherwise verified trace as missing game identity.
        if self.game.get("app_id") is not None:
            observed["app_id"] = self.game["app_id"]
        expected = {
            "version": self.game["version"],
            "commit": self.game["commit"],
            "main_assembly_hash": self.game["main_assembly_hash"],
            "steam_build_id": self.game["steam_build_id"],
            "branch": self.game["branch"],
        }
        mismatches = {
            key: {"expected": expected[key], "observed": observed[key]}
            for key in expected
            if expected[key] != observed[key]
        }
        if mismatches:
            raise VersionLockError(
                "Installed game does not match live_version.lock.json: "
                + json.dumps(mismatches, ensure_ascii=False, sort_keys=True)
            )
        return observed

    def verify_bridge_health(self, health: dict[str, Any]) -> None:
        message = str(health.get("message", ""))
        expected = str(self.bridge["version"])
        if health.get("status") != "ok" or f"v{expected}" not in message:
            raise VersionLockError(
                f"Expected STS2MCP v{expected}, observed health={health!r}"
            )
        for prefix in ("dll", "manifest"):
            path_value = self.bridge.get(f"{prefix}_path")
            expected_hash = self.bridge.get(f"{prefix}_sha256")
            if not path_value or not expected_hash:
                continue
            path = Path(path_value)
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest().upper()
            except OSError as exc:
                raise VersionLockError(f"Cannot hash locked bridge file {path}: {exc}") from exc
            if digest != str(expected_hash).upper():
                raise VersionLockError(
                    f"Locked bridge {prefix} hash differs: expected {expected_hash}, "
                    f"observed {digest}"
                )

    def verify_live_mods(self) -> list[str]:
        environment = self.raw.get("evaluation_environment") or {}
        allowed = {str(value) for value in environment.get("allowed_mod_ids") or []}
        roots = [Path(value) for value in environment.get("live_mod_roots") or []]
        observed: set[str] = set()
        for root in roots:
            if not root.exists():
                continue
            for manifest in root.rglob("*.json"):
                try:
                    payload = json.loads(manifest.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                mod_id = payload.get("id")
                if isinstance(mod_id, str) and mod_id:
                    observed.add(mod_id)
        unexpected = sorted(observed - allowed)
        if unexpected:
            raise VersionLockError(
                "Unexpected live mods would contaminate evaluation: "
                + ", ".join(unexpected)
            )
        missing = sorted(allowed - observed)
        if missing:
            raise VersionLockError("Required live mods are missing: " + ", ".join(missing))
        return sorted(observed)


class TraceRecorder:
    def __init__(self, path: Path, metadata: dict[str, Any] | None = None):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.session_id = str(uuid.uuid4())
        self.sequence = 0
        self.write("session", metadata or {})

    def write(self, event_type: str, raw: Any, **fields: Any) -> None:
        event = {
            "schema_version": 1,
            "session_id": self.session_id,
            "sequence": self.sequence,
            "timestamp_utc": utc_now(),
            "event_type": event_type,
            **fields,
            "raw": raw,
        }
        self.sequence += 1
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


class STS2MCPController:
    def __init__(
        self,
        base_url: str,
        state_path: str = "/api/v1/singleplayer",
        health_path: str = "/",
        compendium_path: str = "/api/v1/compendium",
        timeout: float = 2.0,
        allow_actions: bool = False,
        recorder: TraceRecorder | None = None,
    ):
        parsed = urlsplit(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("STS2MCP base URL must be loopback HTTP")
        self.base_url = base_url.rstrip("/")
        self.state_path = state_path
        self.health_path = health_path
        self.compendium_path = compendium_path
        self.timeout = timeout
        self.allow_actions = allow_actions
        self.recorder = recorder
        # The last start carries a complete, bridge-verified identity.  The
        # out-of-combat allocation driver consumes this instead of guessing a
        # run id/seed from the transient first state after Embark.
        self.last_run_identity: dict[str, Any] | None = None

    def _json_request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        url = self.base_url + path
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, method=method, headers=headers)
        try:
            with urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise BridgeProtocolError(f"{method} {url} -> HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise BridgeProtocolError(f"{method} {url} failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise BridgeProtocolError(f"{method} {url} returned non-JSON") from exc
        if not isinstance(data, dict):
            raise BridgeProtocolError(f"{method} {url} returned non-object JSON")
        return data

    def health(self, record: bool = True) -> dict[str, Any]:
        raw = self._json_request("GET", self.health_path)
        if record and self.recorder:
            self.recorder.write("health", raw, method="GET", path=self.health_path)
        return raw

    def get_state(self, record: bool = True) -> tuple[dict[str, Any], str]:
        separator = "&" if "?" in self.state_path else "?"
        path = self.state_path + separator + "format=json"
        raw = self._json_request("GET", path)
        current_id = decision_id(raw)
        if record and self.recorder:
            self.recorder.write(
                "state", raw, method="GET", path=path, decision_id=current_id
            )
        return raw, current_id

    def get_compendium(self, record: bool = True) -> dict[str, Any]:
        raw = self._json_request("GET", self.compendium_path)
        if record and self.recorder:
            self.recorder.write(
                "compendium", raw, method="GET", path=self.compendium_path
            )
        return raw

    def send_action(
        self,
        payload: dict[str, Any],
        *,
        expected_decision_id: str | None = None,
        capture_resulting_state: bool = True,
    ) -> tuple[dict[str, Any], tuple[dict[str, Any], str] | None]:
        if not self.allow_actions:
            raise ActionPermissionError(
                "POST is disabled. Restart with --allow-actions to grant it explicitly."
            )
        if not isinstance(payload.get("action"), str):
            raise ValueError("Action payload must contain a string 'action'")
        before, before_id = self.get_state(record=True)
        if expected_decision_id and expected_decision_id != before_id:
            raise BridgeProtocolError(
                f"Stale decision: expected {expected_decision_id}, current {before_id}"
            )
        if self.recorder:
            self.recorder.write(
                "action",
                payload,
                method="POST",
                path=self.state_path,
                decision_id=before_id,
                state_type=before.get("state_type"),
            )
        result = self._json_request("POST", self.state_path, payload)
        if self.recorder:
            self.recorder.write(
                "result",
                result,
                method="POST",
                path=self.state_path,
                decision_id=before_id,
            )
        resulting_state = self.get_state(record=True) if capture_resulting_state else None
        return result, resulting_state

    def wait_for_new_decision(
        self,
        previous_id: str,
        *,
        timeout: float = 8.0,
        poll: float = 0.1,
        predicate: Callable[[dict[str, Any]], bool] | None = None,
    ) -> tuple[dict[str, Any], str]:
        deadline = time.monotonic() + timeout
        last_state: dict[str, Any] | None = None
        last_id = previous_id
        while time.monotonic() < deadline:
            last_state, last_id = self.get_state(record=True)
            if last_id != previous_id and (predicate is None or predicate(last_state)):
                return last_state, last_id
            time.sleep(poll)
        raise BridgeProtocolError(
            f"Timed out waiting for a new decision after {previous_id}; "
            f"last={last_id}, state_type={(last_state or {}).get('state_type')}"
        )

    @staticmethod
    def _menu_options(state: dict[str, Any]) -> dict[str, bool]:
        result: dict[str, bool] = {}
        for option in state.get("options") or []:
            if isinstance(option, str):
                result[option] = True
            elif isinstance(option, dict) and option.get("name"):
                result[str(option["name"])] = bool(option.get("enabled", True))
        return result

    @staticmethod
    def _visible_ascension(state: dict[str, Any]) -> int | None:
        candidates = [
            state.get("ascension"),
            (state.get("run") or {}).get("ascension"),
            (state.get("lobby") or {}).get("ascension"),
            (state.get("character_select") or {}).get("ascension"),
        ]
        for value in candidates:
            if isinstance(value, int):
                return value
        return None

    def _menu_action_and_wait(
        self,
        state_id: str,
        option: str,
        timeout: float,
        *,
        seed: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        payload: dict[str, Any] = {"action": "menu_select", "option": option}
        if seed is not None:
            # Keep the exact caller string in the POST and trace.  The game
            # bridge returns its canonical form separately; no numeric cast is
            # permitted here.
            payload["seed"] = seed
        result, _ = self.send_action(
            payload,
            expected_decision_id=state_id,
        )
        if result.get("status") != "ok":
            raise BridgeProtocolError(f"menu_select({option}) failed: {result}")
        return self.wait_for_new_decision(state_id, timeout=timeout)

    def _wait_for_verified_run_identity(
        self,
        state: dict[str, Any],
        *,
        requested_seed: str | None,
        canonical_seed: str | None,
        timeout: float,
    ) -> dict[str, Any]:
        """Poll the active save until its complete identity is readable.

        A newly-started run can expose live state before ``current_run.save``
        exists.  Missing identity fields are retried only for this bounded
        interval.  A non-empty wrong seed is permanent evidence of a failed
        injection and stops immediately.
        """
        deadline = time.monotonic() + max(timeout, 0.0)
        last_error: BridgeProtocolError | None = None
        while True:
            compendium = self.get_compendium(record=True)
            current_run = compendium.get("current_run")
            observed_seed = (
                current_run.get("seed")
                if isinstance(current_run, dict)
                else None
            )
            if requested_seed is not None and observed_seed not in (None, ""):
                # Do not wait for a mismatching/non-string seed to become the
                # requested seed: the current authoritative save is wrong.
                if observed_seed != canonical_seed:
                    if self.recorder:
                        self.recorder.write(
                            "guard",
                            {
                                "status": "blocked",
                                "reason": "seed_mismatch",
                                "seed_requested": requested_seed,
                                "seed_canonical": canonical_seed,
                                "observed_seed": observed_seed,
                            },
                        )
                    raise BridgeProtocolError(
                        "Seeded start was not verified: "
                        f"requested canonical seed {canonical_seed!r}, "
                        f"observed {observed_seed!r}"
                    )
            try:
                return merge_verified_run_identity(
                    state,
                    compendium,
                    requested_seed=requested_seed,
                    canonical_seed=canonical_seed,
                )
            except BridgeProtocolError as exc:
                last_error = exc
                if time.monotonic() >= deadline:
                    if self.recorder:
                        self.recorder.write(
                            "guard",
                            {
                                "status": "blocked",
                                "reason": "run_identity_not_verified",
                                "seed_requested": requested_seed,
                                "seed_canonical": canonical_seed,
                                "last_error": str(last_error),
                            },
                        )
                    raise BridgeProtocolError(
                        "Timed out waiting for authoritative run identity: "
                        f"{last_error}"
                    ) from exc
                time.sleep(min(0.1, max(deadline - time.monotonic(), 0.0)))

    def start_ironclad_a10(
        self,
        *,
        confirm_ui_a10: bool = False,
        timeout: float = 10.0,
        seed: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        """Navigate to an Ironclad A10 run, with a strict ascension guard.

        The project-local STS2MCP compatibility build exposes and validates the
        singleplayer ascension panel.  It sets A10 before Embark and then verifies
        the resulting run state.  When ``seed`` is provided, the exact caller
        string is sent with the standard singleplayer confirmation and the
        authoritative ``current_run.seed`` must match the game's canonical form;
        otherwise this method raises and never claims a fixed-seed start.
        ``confirm_ui_a10`` is retained only for backward CLI compatibility and is
        not used when the level is machine-readable.
        """
        if not self.allow_actions:
            raise ActionPermissionError("Starting a run requires --allow-actions")
        self.last_run_identity = None
        canonical_seed = None
        if seed is not None:
            # Validate before the first GET so an invalid fixed-seed request
            # cannot accidentally continue an existing/interactive run.
            canonical_seed = validate_requested_seed(seed)
        state, current_id = self.get_state(record=True)
        if state.get("run") and seed is not None:
            raise BridgeProtocolError(
                "Refusing seeded start while a run is already active; "
                "no seed was injected"
            )
        selected_ironclad = False
        for _ in range(8):
            if state.get("run"):
                break
            if state.get("state_type") != "menu":
                raise BridgeProtocolError("Refusing to start: game is not on a menu")
            screen = state.get("menu_screen")
            if screen == "main":
                if not self._menu_options(state).get("singleplayer"):
                    raise BridgeProtocolError("Singleplayer is not currently actionable")
                state, current_id = self._menu_action_and_wait(
                    current_id, "singleplayer", timeout
                )
            elif screen == "singleplayer":
                if not self._menu_options(state).get("standard"):
                    raise BridgeProtocolError("Standard mode is not currently actionable")
                state, current_id = self._menu_action_and_wait(
                    current_id, "standard", timeout
                )
            elif screen == "character_select":
                options = self._menu_options(state)
                if not selected_ironclad:
                    if not options.get("IRONCLAD"):
                        raise BridgeProtocolError("IRONCLAD is unavailable or locked")
                    result, resulting = self.send_action(
                        {"action": "menu_select", "option": "IRONCLAD"},
                        expected_decision_id=current_id,
                    )
                    if result.get("status") != "ok" or resulting is None:
                        raise BridgeProtocolError(f"Selecting IRONCLAD failed: {result}")
                    # Re-selecting the already active character is a valid idempotent
                    # action and need not change the serialized state hash.
                    state, current_id = resulting
                    selected_ironclad = True
                    continue
                visible_ascension = self._visible_ascension(state)
                if visible_ascension is not None and visible_ascension != 10:
                    result, resulting = self.send_action(
                        {"action": "set_ascension", "level": 10},
                        expected_decision_id=current_id,
                    )
                    if result.get("status") != "ok" or resulting is None:
                        raise BridgeProtocolError(f"set_ascension(10) failed: {result}")
                    state, current_id = resulting
                    visible_ascension = self._visible_ascension(state)
                    if visible_ascension != 10:
                        raise BridgeProtocolError(
                            "Bridge reported success but character-select ascension "
                            f"is {visible_ascension!r}, expected 10"
                        )
                if visible_ascension is None and not confirm_ui_a10:
                    if self.recorder:
                        self.recorder.write(
                            "guard",
                            {
                                "status": "blocked",
                                "reason": "a10_not_machine_verifiable",
                                "visible_ascension": None,
                            },
                            decision_id=current_id,
                        )
                    raise BridgeProtocolError(
                        "Stopped before Embark: the bridge cannot read ascension on "
                        "this character-select screen."
                    )
                if self.recorder and visible_ascension == 10:
                    self.recorder.write(
                        "machine_verification",
                        {"character": "IRONCLAD", "ascension": 10},
                        decision_id=current_id,
                    )
                if not options.get("confirm") and not options.get("embark"):
                    # Character selection and Embark use deferred Godot callbacks.
                    # During the animation the button can briefly disappear while
                    # the run is already being created. Wait for the next stable
                    # state before treating this as a failure.
                    state, current_id = self.wait_for_new_decision(
                        current_id, timeout=timeout
                    )
                    continue
                state, current_id = self._menu_action_and_wait(
                    current_id,
                    "confirm",
                    timeout,
                    seed=seed,
                )
            else:
                raise BridgeProtocolError(f"Unsupported menu screen during start: {screen}")

            run = state.get("run") or {}
            if run:
                break
        else:
            raise BridgeProtocolError("Start workflow exceeded its transition budget")

        run = state.get("run") or {}
        player = state.get("player") or {}
        if run and not (player.get("character_id") or player.get("character")):
            # Run metadata becomes visible a few frames before LocalContext exposes
            # the player. Poll for the stable post-Embark state instead of failing
            # a correctly started run on that transient frame.
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                time.sleep(0.1)
                state, current_id = self.get_state(record=True)
                run = state.get("run") or {}
                player = state.get("player") or {}
                if player.get("character_id") or player.get("character"):
                    break
        character_id = str(
            player.get("character_id") or run.get("character_id") or ""
        ).upper()
        character = str(player.get("character") or run.get("character") or "")
        ascension = run.get("ascension")
        ironclad_titles = {"铁甲战士", "THE IRONCLAD", "IRONCLAD"}
        is_ironclad = character_id == "IRONCLAD" or character.upper() in ironclad_titles
        if not is_ironclad or ascension != 10:
            raise BridgeProtocolError(
                "Started run failed verification: "
                f"character_id={character_id!r}, character={character!r}, "
                f"ascension={ascension!r}"
            )
        identity = self._wait_for_verified_run_identity(
            state,
            requested_seed=seed,
            canonical_seed=canonical_seed,
            timeout=timeout,
        )
        self.last_run_identity = dict(identity)
        if self.recorder:
            self.recorder.write(
                "run_identity",
                identity,
                decision_id=current_id,
            )
        return state, current_id


def _default_trace_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return DEFAULT_TRACE_DIR / f"sts2mcp-{stamp}.jsonl"


def _project_path(path: Path) -> Path:
    return path if path.is_absolute() else PROJECT_ROOT / path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Version-locked STS2MCP recorder/controller (GET-only by default)"
    )
    parser.add_argument("--lock-file", type=Path, default=DEFAULT_LOCK_PATH)
    parser.add_argument("--trace", type=Path, default=None)
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument(
        "--allow-actions",
        action="store_true",
        help="enable POST actions for this process; omitted means GET-only",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("probe", help="verify versions and record health + one state")
    record = sub.add_parser("record", help="poll and record raw states")
    record.add_argument("--poll", type=float, default=0.25)
    record.add_argument("--max-polls", type=int, default=0)
    action = sub.add_parser("action", help="send one raw STS2MCP action")
    action.add_argument("--json", required=True, dest="action_json")
    action.add_argument("--decision-id")
    start = sub.add_parser("start-ironclad-a10", help="guarded Ironclad A10 start")
    start.add_argument(
        "--confirm-ui-a10",
        action="store_true",
        help="legacy fallback only when an unpatched bridge cannot expose ascension",
    )
    start.add_argument(
        "--seed",
        default=None,
        help="raw ASCII alphanumeric run seed; verified against current_run.seed",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command or "probe"
    lock = VersionLock.load(args.lock_file)
    observed_game = lock.verify_installed_game()
    observed_mods = lock.verify_live_mods()
    trace_path = _project_path(args.trace) if args.trace else _default_trace_path()
    recorder = TraceRecorder(
        trace_path,
        metadata={
            "mode": "actions-enabled" if args.allow_actions else "read-only",
            "version_lock": lock.raw,
            "observed_game": observed_game,
            "observed_mods": observed_mods,
            "argv": sys.argv if argv is None else argv,
        },
    )
    controller = STS2MCPController(
        base_url=lock.bridge["base_url"],
        state_path=lock.bridge["singleplayer_path"],
        health_path=lock.bridge["health_path"],
        compendium_path=lock.bridge.get(
            "compendium_path", "/api/v1/compendium"
        ),
        timeout=args.timeout,
        allow_actions=args.allow_actions,
        recorder=recorder,
    )
    health = controller.health()
    lock.verify_bridge_health(health)

    if command == "probe":
        state, current_id = controller.get_state()
        print(
            json.dumps(
                {
                    "mode": "actions-enabled" if args.allow_actions else "read-only",
                    "game": observed_game,
                    "bridge": health,
                    "state_type": state.get("state_type"),
                    "menu_screen": state.get("menu_screen"),
                    "decision_id": current_id,
                    "trace": str(trace_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if command == "record":
        polls = 0
        previous_id = None
        try:
            while args.max_polls <= 0 or polls < args.max_polls:
                state, current_id = controller.get_state()
                if current_id != previous_id:
                    print(f"{utc_now()} {state.get('state_type')} {current_id}")
                    previous_id = current_id
                polls += 1
                time.sleep(args.poll)
        except KeyboardInterrupt:
            return 0
        return 0
    if command == "action":
        payload = json.loads(args.action_json)
        result, resulting = controller.send_action(
            payload, expected_decision_id=args.decision_id
        )
        print(json.dumps({"result": result, "state": resulting}, ensure_ascii=False, indent=2))
        return 0
    if command == "start-ironclad-a10":
        state, current_id = controller.start_ironclad_a10(
            confirm_ui_a10=args.confirm_ui_a10,
            seed=args.seed,
        )
        print(
            json.dumps(
                {"status": "started", "decision_id": current_id, "state": state},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    raise AssertionError(command)


if __name__ == "__main__":
    raise SystemExit(main())
