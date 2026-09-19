"""The evidence bundle's hash binding must actually move when an artifact moves."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "build_evidence_manifest", ROOT / "scripts" / "build_evidence_manifest.py")
M = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(M)
EVIDENCE = ROOT / "docs" / "evidence"
SELF = "MANIFEST_2026-09-19.json"


def build(working: Path) -> dict:
    out = working / SELF
    argv = list(sys.argv)
    sys.argv = ["build_evidence_manifest.py", "--evidence-dir", str(working),
                "--out", str(out)]
    try:
        M.main()
    finally:
        sys.argv = argv
    return json.loads(out.read_text(encoding="utf-8"))


def root_of(files: list[dict]) -> str:
    return hashlib.sha256("".join(
        f"{entry['file']}  {entry['sha256']}\n"
        for entry in sorted(files, key=lambda e: e["file"])).encode("utf-8")).hexdigest()


class EvidenceManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.working = Path(tmp.name) / "evidence"
        self.working.mkdir()
        for path in EVIDENCE.glob("*.json"):
            if path.name != SELF:
                shutil.copy2(path, self.working / path.name)

    def test_bundle_root_is_deterministic(self) -> None:
        first, second = build(self.working), build(self.working)
        self.assertEqual(first["bundle_root"], second["bundle_root"])
        self.assertEqual(first["files"], second["files"])

    def test_editing_one_artifact_changes_the_root(self) -> None:
        before = build(self.working)["bundle_root"]
        victim = self.working / "act1_win_ledger_20260919.json"
        victim.write_text(victim.read_text(encoding="utf-8") + " ", encoding="utf-8")
        after = build(self.working)
        self.assertNotEqual(before, after["bundle_root"])
        entry = next(e for e in after["files"] if e["file"] == victim.name)
        self.assertNotEqual(hashlib.sha256(victim.read_bytes()).hexdigest(),
                            hashlib.sha256((EVIDENCE / victim.name).read_bytes()).hexdigest())
        self.assertEqual(entry["sha256"], hashlib.sha256(victim.read_bytes()).hexdigest())

    def test_an_unmanifested_addition_is_visible_in_the_listing(self) -> None:
        stale = build(self.working)
        (self.working / "invented_artifact_20260919.json").write_text("{}\n", encoding="utf-8")
        fresh = build(self.working)
        self.assertNotEqual(fresh["bundle_root"], stale["bundle_root"])
        # The verifier compares the manifest's listing against the directory, so a file that
        # exists but was never manifested is drift, not silence.
        self.assertNotEqual(sorted(e["file"] for e in stale["files"]),
                            sorted(e["file"] for e in fresh["files"]))

    def test_the_manifest_never_hashes_itself(self) -> None:
        manifest = build(self.working)
        self.assertNotIn(SELF, [entry["file"] for entry in manifest["files"]])

    def test_committed_manifest_still_matches_the_committed_bundle(self) -> None:
        committed = json.loads((EVIDENCE / SELF).read_text(encoding="utf-8"))
        on_disk = sorted(p.name for p in EVIDENCE.glob("*.json") if p.name != SELF)
        self.assertEqual(on_disk, sorted(e["file"] for e in committed["files"]))
        self.assertEqual(
            root_of(committed["files"]), committed["bundle_root"],
            "the committed bundle_root does not follow from the committed file digests")
        for entry in committed["files"]:
            path = EVIDENCE / entry["file"]
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), entry["sha256"],
                             f"{entry['file']} changed without the manifest being rebuilt")
            self.assertEqual(path.stat().st_size, entry["bytes"])


if __name__ == "__main__":
    unittest.main()
