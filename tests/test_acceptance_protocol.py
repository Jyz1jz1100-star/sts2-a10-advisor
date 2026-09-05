"""Guards for the human-facing full-run acceptance protocol."""
from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AcceptanceProtocolTests(unittest.TestCase):
    def test_protocol_uses_version_lock_as_single_source_of_truth(self) -> None:
        protocol = (ROOT / "docs" / "ACCEPTANCE.md").read_text(encoding="utf-8")
        lock = json.loads(
            (ROOT / "config" / "live_version.lock.json").read_text(encoding="utf-8")
        )
        self.assertIn("config/live_version.lock.json", protocol)
        # This was the stale build previously printed in the protocol.  A
        # historical value must never be able to look like the current target.
        self.assertNotIn("24489008", protocol)
        self.assertTrue(lock["game"]["steam_build_id"])
        self.assertTrue(lock["game"]["commit"])
        self.assertTrue(lock["game"]["main_assembly_hash"])


if __name__ == "__main__":
    unittest.main()
