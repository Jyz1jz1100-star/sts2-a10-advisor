"""STS2MCP HTTP client. READ-ONLY BY CONSTRUCTION.

This module exposes only GET reads. There is deliberately no method that issues
a POST / game action, so the advisor cannot alter the game even by mistake.
"""
from __future__ import annotations

import requests


class BridgeError(Exception):
    """Raised when the bridge is unreachable or returns an error."""


class BridgeClient:
    def __init__(self, base_url: str, state_path: str, health_path: str,
                 timeout: float = 4.0):
        self.base_url = base_url.rstrip("/")
        self.state_path = state_path
        self.health_path = health_path
        self.timeout = timeout
        self._session = requests.Session()

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = self.base_url + path
        try:
            resp = self._session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            raise BridgeError(f"GET {url} failed: {exc}") from exc
        except ValueError as exc:
            raise BridgeError(f"GET {url} returned non-JSON: {exc}") from exc

    def get_state(self) -> dict:
        """Read the current singleplayer game state (GET only)."""
        return self._get(self.state_path, params={"format": "json"})

    def is_up(self) -> bool:
        """Cheap reachability check against the health endpoint."""
        try:
            self._get(self.health_path)
            return True
        except BridgeError:
            return False
