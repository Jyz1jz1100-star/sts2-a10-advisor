from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge.trace_controller import (
    ActionPermissionError,
    BridgeProtocolError,
    STS2MCPController,
    TraceRecorder,
    VersionLock,
    canonicalize_game_seed,
    decision_id,
    merge_verified_run_identity,
    validate_requested_seed,
)


class TraceControllerTests(unittest.TestCase):
    def test_seed_canonicalization_preserves_raw_alphanumeric_contract(self) -> None:
        self.assertEqual(canonicalize_game_seed(" 2450zarO1EF "), "2450ZAR01EF")
        self.assertEqual(validate_requested_seed("1600000000"), "1600000000")

    def test_seed_validation_rejects_empty_or_non_alphanumeric_values(self) -> None:
        with self.assertRaises(ValueError):
            validate_requested_seed("   ")
        with self.assertRaises(ValueError):
            validate_requested_seed("seed-7")

    def test_seeded_menu_confirmation_posts_exact_raw_seed(self) -> None:
        controller = STS2MCPController(
            "http://127.0.0.1:15526", allow_actions=True
        )
        with patch.object(
            controller,
            "send_action",
            return_value=({"status": "ok"}, None),
        ) as send_action, patch.object(
            controller,
            "wait_for_new_decision",
            return_value=({"state_type": "menu"}, "next"),
        ):
            state, state_id = controller._menu_action_and_wait(
                "before", "confirm", 1.0, seed=" 2450zarO1EF "
            )
        self.assertEqual(state_id, "next")
        self.assertEqual(state["state_type"], "menu")
        self.assertEqual(
            send_action.call_args.args[0],
            {
                "action": "menu_select",
                "option": "confirm",
                "seed": " 2450zarO1EF ",
            },
        )

    def test_run_identity_merges_live_and_authoritative_save(self) -> None:
        identity = merge_verified_run_identity(
            {
                "state_type": "map",
                "game_mode": "standard",
                "run": {
                    "run_id": "live-run-1",
                    "ascension": 10,
                },
                "player": {
                    "character_id": "IRONCLAD",
                    "character": "The Ironclad",
                },
            },
            {
                "current_run": {
                    "is_in_progress": True,
                    "run_id": "live-run-1",
                    "seed": "2450ZAR9EF",
                    "ascension": 10,
                    "game_mode": "Standard",
                    "save_scope": "local",
                }
            },
            requested_seed="2450zar9ef",
            canonical_seed="2450ZAR9EF",
        )
        self.assertEqual(
            identity,
            {
                "run_id": "live-run-1",
                "seed": "2450ZAR9EF",
                "character": "IRONCLAD",
                "ascension": 10,
                "game_mode": "standard",
                "character_id": "IRONCLAD",
                "character_title": "The Ironclad",
                "save_scope": "local",
                "seed_requested": "2450zar9ef",
                "seed_canonical": "2450ZAR9EF",
            },
        )

    def test_run_identity_rejects_conflicting_sources(self) -> None:
        with self.assertRaises(BridgeProtocolError):
            merge_verified_run_identity(
                {
                    "run": {"run_id": "live-run-1", "ascension": 10},
                    "player": {"character_id": "IRONCLAD"},
                },
                {
                    "current_run": {
                        "is_in_progress": True,
                        "run_id": "saved-run-2",
                        "seed": "2450ZAR9EF",
                        "ascension": 10,
                        "game_mode": "standard",
                    }
                },
            )

    def test_run_identity_falls_back_to_verified_live_run_id(self) -> None:
        identity = merge_verified_run_identity(
            {
                "run": {"run_id": "live-only", "ascension": 10},
                "player": {"character_id": "IRONCLAD"},
            },
            {
                "current_run": {
                    "is_in_progress": True,
                    "seed": "1600000000",
                    "ascension": 10,
                    "game_mode": "standard",
                }
            },
        )
        self.assertEqual(identity["run_id"], "live-only")

    def test_run_identity_rejects_missing_run_id_in_both_sources(self) -> None:
        with self.assertRaises(BridgeProtocolError):
            merge_verified_run_identity(
                {
                    "run": {"ascension": 10},
                    "player": {"character_id": "IRONCLAD"},
                },
                {
                    "current_run": {
                        "is_in_progress": True,
                        "seed": "1600000000",
                        "ascension": 10,
                        "game_mode": "standard",
                    }
                },
            )

    def test_seed_identity_polls_missing_save_then_verifies(self) -> None:
        controller = STS2MCPController("http://127.0.0.1:15526")
        responses = iter(
            [
                {"current_run": {"is_in_progress": True}},
                {
                    "current_run": {
                        "is_in_progress": True,
                        "run_id": "run-1",
                        "seed": "1600000000",
                        "ascension": 10,
                        "game_mode": "standard",
                    }
                },
            ]
        )
        with patch.object(controller, "get_compendium", side_effect=lambda **_: next(responses)):
            identity = controller._wait_for_verified_run_identity(
                {
                    "game_mode": "standard",
                    "run": {"ascension": 10},
                    "player": {"character_id": "IRONCLAD"},
                },
                requested_seed="1600000000",
                canonical_seed="1600000000",
                timeout=1.0,
            )
        self.assertEqual(identity["run_id"], "run-1")
        self.assertEqual(identity["seed"], "1600000000")

    def test_seed_identity_mismatch_fails_without_waiting(self) -> None:
        controller = STS2MCPController("http://127.0.0.1:15526")
        with patch.object(
            controller,
            "get_compendium",
            return_value={
                "current_run": {
                    "is_in_progress": True,
                    "run_id": "run-1",
                    "seed": "1600000001",
                }
            },
        ) as get_compendium:
            with self.assertRaises(BridgeProtocolError):
                controller._wait_for_verified_run_identity(
                    {
                        "run": {"ascension": 10},
                        "player": {"character_id": "IRONCLAD"},
                    },
                    requested_seed="1600000000",
                    canonical_seed="1600000000",
                    timeout=1.0,
                )
        get_compendium.assert_called_once()

    def test_decision_id_prefers_server_value(self) -> None:
        self.assertEqual(decision_id({"decision_id": "server-7"}), "server-7")

    def test_fallback_decision_id_is_canonical(self) -> None:
        left = decision_id({"state_type": "menu", "options": ["a"], "x": 1})
        right = decision_id({"x": 1, "options": ["a"], "state_type": "menu"})
        self.assertEqual(left, right)
        self.assertTrue(left.startswith("local-sha256:"))

    def test_post_is_blocked_before_any_network_request(self) -> None:
        controller = STS2MCPController("http://127.0.0.1:15526")
        with patch("bridge.trace_controller.urlopen") as mocked:
            with self.assertRaises(ActionPermissionError):
                controller.send_action({"action": "menu_select", "option": "quit"})
            mocked.assert_not_called()

    def test_trace_preserves_raw_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "trace.jsonl"
            recorder = TraceRecorder(path, {"mode": "read-only"})
            raw = {"state_type": "menu", "options": ["singleplayer"]}
            recorder.write("state", raw, decision_id=decision_id(raw))
            events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[0]["event_type"], "session")
            self.assertEqual(events[1]["raw"], raw)

    def test_version_lock_reads_release_and_steam_build(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            release = root / "release_info.json"
            manifest = root / "appmanifest.acf"
            release.write_text(
                json.dumps(
                    {
                        "version": "v0.111.0",
                        "commit": "41cef1ea",
                        "main_assembly_hash": 222455745,
                    }
                ),
                encoding="utf-8",
            )
            manifest.write_text(
                '"buildid" "24724944"\n"BetaKey" "public-beta"\n',
                encoding="utf-8",
            )
            lock = VersionLock(
                {
                    "schema_version": 1,
                    "game": {
                        "version": "v0.111.0",
                        "commit": "41cef1ea",
                        "main_assembly_hash": 222455745,
                        "steam_build_id": "24724944",
                        "branch": "public-beta",
                        "release_info_path": str(release),
                        "steam_manifest_path": str(manifest),
                    },
                    "bridge": {"version": "0.4.0"},
                }
            )
            observed = lock.verify_installed_game()
            self.assertEqual(observed["steam_build_id"], "24724944")


if __name__ == "__main__":
    unittest.main()
