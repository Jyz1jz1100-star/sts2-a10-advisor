"""STS2MCP HTTP client. READ-ONLY BY CONSTRUCTION.

This module exposes only GET reads. There is deliberately no method that issues
a POST / game action, so the advisor cannot alter the game even by mistake.

Implemented on the standard library (urllib) so the small runtime venv stays
dependency-free.
"""
from __future__ import annotations

import json
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen


class BridgeError(Exception):
    """Raised when the bridge is unreachable or returns an error."""


class BridgeClient:
    def __init__(self, base_url: str, state_path: str, health_path: str,
                 timeout: float = 4.0,
                 compendium_path: str | None = "/api/v1/compendium"):
        self.base_url = base_url.rstrip("/")
        self.state_path = state_path
        self.health_path = health_path
        self.timeout = timeout
        # STS2MCP's singleplayer state omits the run seed on some screens.
        # The compendium endpoint reads the authoritative value from the
        # active current_run.save.  Keep this optional so callers can disable
        # the enrichment when talking to an older bridge.
        self.compendium_path = compendium_path

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = self.base_url + path
        if params:
            url = f"{url}?{urlencode(params)}"
        try:
            with urlopen(url, timeout=self.timeout) as resp:
                if resp.status != 200:
                    raise BridgeError(f"GET {url} returned HTTP {resp.status}")
                body = resp.read()
        except BridgeError:
            raise
        except (URLError, OSError, TimeoutError) as exc:
            raise BridgeError(f"GET {url} failed: {exc}") from exc
        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise BridgeError(f"GET {url} returned non-JSON: {exc}") from exc

    def _merge_current_run_seed(self, state: dict) -> dict:
        """Add the authoritative active-run seed when state omitted it.

        ``GET /api/v1/singleplayer`` is intentionally the primary state read.
        STS2MCP exposes the real seed separately as
        ``compendium.current_run.seed`` (read from ``current_run.save``), so
        use that endpoint only as a read-only enrichment.  Any unavailable,
        malformed, or non-active compendium response leaves the original state
        untouched; fixed-seed callers then fail closed instead of receiving a
        fabricated value.
        """
        if not isinstance(state, dict) or not self.compendium_path:
            return state
        run = state.get("run")
        if not isinstance(run, dict):
            return state
        current_seed = run.get("seed")
        if current_seed is not None and current_seed != "":
            return state
        try:
            compendium = self._get(self.compendium_path)
        except BridgeError:
            return state
        if not isinstance(compendium, dict):
            return state
        current_run = compendium.get("current_run")
        if not isinstance(current_run, dict) or current_run.get("is_in_progress") is not True:
            return state
        seed = current_run.get("seed")
        if isinstance(seed, bool) or not isinstance(seed, (int, str)):
            return state
        if isinstance(seed, str) and not seed.strip():
            return state

        enriched = dict(state)
        enriched_run = dict(run)
        enriched_run["seed"] = seed
        enriched_run["seed_source"] = "current_run.save"
        run_id = current_run.get("run_id")
        if isinstance(run_id, str) and run_id:
            enriched_run["run_id"] = run_id
        enriched["run"] = enriched_run
        provenance = {
            "source": "GET /api/v1/compendium current_run.seed",
            "save_source": "current_run.save",
        }
        if isinstance(run_id, str) and run_id:
            provenance["run_id"] = run_id
        enriched["run_seed_provenance"] = provenance
        return enriched

    def get_state(self) -> dict:
        """Read current state and enrich a missing seed using GET only."""
        state = self._get(self.state_path, params={"format": "json"})
        return self._merge_current_run_seed(state)

    def get_compendium(self) -> dict:
        """Read the authoritative current-run identity using GET only."""
        if not self.compendium_path:
            raise BridgeError("compendium endpoint is not configured")
        return self._get(self.compendium_path)

    def is_up(self) -> bool:
        """Cheap reachability check against the health endpoint."""
        try:
            self._get(self.health_path)
            return True
        except BridgeError:
            return False
