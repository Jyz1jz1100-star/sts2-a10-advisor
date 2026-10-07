"""The instrument that judges the refusal fix has to be tested itself.

The acceptance contract counts refused posts per run, so a measurement that mis-attributes a post to
the wrong run, or folds two refusal mechanisms into one label, would let a real regression read as
clean.  Both are checked here against a synthetic trace instead of against a live batch.
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "measure_post_refusals", ROOT / "scripts" / "measure_post_refusals.py")
M = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(M)


def _rows(*events) -> str:
    return "".join(json.dumps(event) + "\n" for event in events)


def _identity(run_id: str) -> dict:
    return {"event_type": "run_identity", "raw": {"run_id": run_id}}


def _action() -> dict:
    return {"event_type": "action", "raw": {"action": "proceed"}}


def _result(status: str, error: str = "") -> dict:
    return {"event_type": "result", "raw": {"status": status, "error": error}}


class RefusalMeasurementTests(unittest.TestCase):
    def test_refusals_are_attributed_per_run_not_per_batch(self) -> None:
        trace = _rows(
            _identity("run-a"), _action(), _result("ok"), _action(),
            _result("error", "Rewards screen is not open"),
            _identity("run-b"), _action(), _result("ok"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.jsonl"
            path.write_text(trace, encoding="utf-8")
            report = M.measure(path)
        self.assertEqual(2, report["runs"])
        self.assertEqual(1, report["runs_with_zero_refusals"])
        by_id = {row["run_id"]: row for row in report["per_run"]}
        self.assertEqual(1, by_id["run-a"]["refused"])
        self.assertEqual(0, by_id["run-b"]["refused"])
        self.assertEqual({"screen_already_gone": 1}, by_id["run-a"]["refusal_classes"])

    def test_posts_before_any_run_identity_are_named_not_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.jsonl"
            path.write_text(_rows(_action(), _result("error", "Whatever")), encoding="utf-8")
            report = M.measure(path)
        self.assertEqual(["PRE_RUN"], [row["run_id"] for row in report["per_run"]])
        self.assertEqual(1, report["refused_total"])

    def test_the_two_refusal_mechanisms_do_not_share_a_label(self) -> None:
        self.assertEqual("screen_already_gone",
                         M.classify("Rewards screen is not open"))
        self.assertEqual("menu_option_not_accepted",
                         M.classify("Unknown menu option: standard"))
        self.assertEqual("decision_id_moved",
                         M.classify("Stale decision: expected local-sha256:aa, current bb"))

    def test_an_unrecognised_error_is_reported_as_other_rather_than_absorbed(self) -> None:
        self.assertEqual("other", M.classify("Some new bridge wording"))
        self.assertEqual("other", M.classify(""))


if __name__ == "__main__":
    unittest.main()
