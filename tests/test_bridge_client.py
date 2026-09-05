"""Tests for read-only STS2MCP state enrichment."""
from __future__ import annotations

import unittest
from unittest.mock import call, patch

from bridge.client import BridgeClient, BridgeError


class BridgeSeedEnrichmentTests(unittest.TestCase):
    def _client(self) -> BridgeClient:
        return BridgeClient(
            "http://127.0.0.1:15526",
            "/api/v1/singleplayer",
            "/",
            compendium_path="/api/v1/compendium",
        )

    def test_missing_state_seed_is_enriched_from_active_compendium(self) -> None:
        state = {
            "state_type": "monster",
            "run": {"act": 1, "floor": 3, "seed": None},
            "player": {"hp": 80},
        }
        compendium = {
            "current_run": {
                "is_in_progress": True,
                "run_id": "local:profile0:123",
                "seed": "2450ZAR9EF",
            }
        }
        client = self._client()
        with patch.object(client, "_get", side_effect=[state, compendium]) as get:
            enriched = client.get_state()

        self.assertEqual(enriched["run"]["seed"], "2450ZAR9EF")
        self.assertEqual(enriched["run"]["seed_source"], "current_run.save")
        self.assertEqual(enriched["run"]["run_id"], "local:profile0:123")
        self.assertEqual(
            enriched["run_seed_provenance"],
            {
                "source": "GET /api/v1/compendium current_run.seed",
                "save_source": "current_run.save",
                "run_id": "local:profile0:123",
            },
        )
        self.assertIsNone(state["run"]["seed"])
        self.assertEqual(
            get.call_args_list,
            [
                call("/api/v1/singleplayer", params={"format": "json"}),
                call("/api/v1/compendium"),
            ],
        )

    def test_existing_state_seed_does_not_require_compendium(self) -> None:
        state = {"state_type": "monster", "run": {"floor": 3, "seed": 7}}
        client = self._client()
        with patch.object(client, "_get", return_value=state) as get:
            self.assertIs(client.get_state(), state)
        get.assert_called_once_with("/api/v1/singleplayer", params={"format": "json"})

    def test_compendium_failure_keeps_seed_missing_for_fail_closed_audit(self) -> None:
        state = {"state_type": "monster", "run": {"floor": 3, "seed": None}}
        client = self._client()
        with patch.object(
            client, "_get", side_effect=[state, BridgeError("bridge down")]
        ):
            result = client.get_state()
        self.assertIs(result, state)
        self.assertIsNone(result["run"]["seed"])

    def test_non_active_compendium_run_is_not_injected(self) -> None:
        state = {"state_type": "monster", "run": {"floor": 3, "seed": None}}
        client = self._client()
        with patch.object(
            client,
            "_get",
            side_effect=[state, {"current_run": {"is_in_progress": False, "seed": 7}}],
        ):
            result = client.get_state()
        self.assertIs(result, state)
        self.assertIsNone(result["run"]["seed"])

    def test_malformed_compendium_seed_is_not_fabricated(self) -> None:
        state = {"state_type": "monster", "run": {"floor": 3, "seed": None}}
        client = self._client()
        with patch.object(
            client,
            "_get",
            side_effect=[state, {"current_run": {"is_in_progress": True, "seed": {}}}],
        ):
            result = client.get_state()
        self.assertIsNone(result["run"]["seed"])

    def test_compendium_is_exposed_as_read_only_identity_read(self) -> None:
        client = self._client()
        compendium = {"current_run": {"game_mode": "standard", "ascension": 10}}
        with patch.object(client, "_get", return_value=compendium) as get:
            self.assertIs(client.get_compendium(), compendium)
        get.assert_called_once_with("/api/v1/compendium")

    def test_compendium_read_fails_closed_when_endpoint_is_disabled(self) -> None:
        client = BridgeClient("http://127.0.0.1:15526", "/state", "/", compendium_path=None)
        with self.assertRaises(BridgeError):
            client.get_compendium()


if __name__ == "__main__":
    unittest.main()
