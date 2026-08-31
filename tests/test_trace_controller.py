from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge.trace_controller import (
    ActionPermissionError,
    STS2MCPController,
    TraceRecorder,
    VersionLock,
    decision_id,
)


class TraceControllerTests(unittest.TestCase):
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
