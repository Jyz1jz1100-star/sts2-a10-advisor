"""The claim verifier must not bless a check that came out False.

Expectations are snapshots compared for equality, so before this gate a pinned ``false``
read exactly like a passing run -- "32/32 claims match the disk" with a failed check inside
it.  These tests lock the gate down, and lock the committed expectations to no false checks.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "verify_report_claims", ROOT / "scripts" / "verify_report_claims.py")
V = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(V)


class ReportClaimGateTests(unittest.TestCase):
    def _run_with(self, claim_result: dict, pinned: dict, extra: dict | None = None) -> int:
        """Drive main() over one synthetic claim, restoring the module afterwards."""
        original = dict(V.CLAIMS)
        argv = list(sys.argv)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "expectations.json"
            expectations = {"demo": pinned}
            expectations.update(extra or {})
            path.write_text(json.dumps(expectations), encoding="utf-8")
            V.CLAIMS.clear()
            V.CLAIMS["demo"] = (lambda: claim_result, "demo claim")
            sys.argv = ["verify_report_claims.py", "--expect", str(path), "--quiet"]
            try:
                return V.main()
            finally:
                sys.argv = argv
                V.CLAIMS.clear()
                V.CLAIMS.update(original)

    def test_a_pinned_false_check_is_reported_as_drift(self) -> None:
        result = {"holds": False, "other": True}
        self.assertEqual(1, self._run_with(result, result))

    def test_a_pinned_true_check_still_passes(self) -> None:
        result = {"holds": True, "other": True}
        self.assertEqual(0, self._run_with(result, result))

    def test_a_declared_false_check_is_allowed_through(self) -> None:
        # An author may legitimately pin a refutation; that has to be declared, not implicit.
        code = self._run_with({"holds": False}, {"holds": False},
                              extra={"_expected_false_checks": {"demo": ["holds"]}})
        self.assertEqual(0, code)

    def test_committed_expectations_contain_no_false_checks(self) -> None:
        expectations = json.loads(
            (ROOT / "docs" / "evidence" / "act1_report_expectations.json").read_text(
                encoding="utf-8"))
        deliberate = expectations.get("_expected_false_checks", {})
        false: dict[str, list[str]] = {}
        for name, checks in expectations.items():
            if not isinstance(checks, dict) or name.startswith("_"):
                continue
            allowed = set(deliberate.get(name, ()))
            bad = sorted(key for key, value in checks.items()
                         if value is False and key not in allowed)
            if bad:
                false[name] = bad
        self.assertEqual({}, false, "a false check is pinned as if it were a passing one")

    def test_every_registered_claim_is_pinned(self) -> None:
        expectations = json.loads(
            (ROOT / "docs" / "evidence" / "act1_report_expectations.json").read_text(
                encoding="utf-8"))
        unpinned = sorted(set(V.CLAIMS) - set(expectations))
        self.assertEqual([], unpinned, "an unpinned claim reports UNPINNED and counts as drift")


if __name__ == "__main__":
    unittest.main()
