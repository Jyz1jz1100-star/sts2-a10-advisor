from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from training.trace_contract import (
    CURRENT_PUBLIC_BETA_BUILD,
    find_seed_leaks,
    validate_record,
)
from training.validate_traces import validate_files


def valid_record(**overrides: object) -> dict:
    record = {
        "trace_version": 1,
        "run_id": "run-001",
        "decision_id": "run-001:1",
        "step": 1,
        "split": "train",
        "build": CURRENT_PUBLIC_BETA_BUILD,
        "seed": "AB12CD34",
        "character": "IRONCLAD",
        "ascension": 10,
        "save_load_used": False,
        "visible_state": {"state_type": "combat", "round": 1},
        "legal_actions": [
            {"action_id": "play:0:enemy:0", "action_type": "play_card"},
            {"action_id": "end_turn", "action_type": "end_turn"},
        ],
        "chosen_action": {
            "action_id": "play:0:enemy:0",
            "action_type": "play_card",
        },
        "result": {"status": "applied", "observed": True},
    }
    record.update(overrides)
    return record


class TraceContractTests(unittest.TestCase):
    def test_valid_public_beta_ironclad_a10_nosl_record(self) -> None:
        self.assertEqual(validate_record(valid_record()), [])

    def test_requires_all_core_trace_fields(self) -> None:
        record = valid_record()
        del record["visible_state"]
        del record["result"]
        codes = {(issue.code, issue.path) for issue in validate_record(record)}
        self.assertIn(("required", "$.visible_state"), codes)
        self.assertIn(("required", "$.result"), codes)

    def test_rejects_build_character_ascension_and_sl_mismatch(self) -> None:
        record = valid_record(
            build="public-beta-v0.110.0",
            character="SILENT",
            ascension=9,
            save_load_used=True,
        )
        codes = {issue.code for issue in validate_record(record)}
        self.assertTrue(
            {"build_mismatch", "character_mismatch", "ascension_mismatch", "sl_not_allowed"}
            <= codes
        )

    def test_chosen_action_must_be_legal_and_type_match(self) -> None:
        illegal = valid_record(
            chosen_action={"action_id": "console:win", "action_type": "debug"}
        )
        self.assertIn(
            "illegal_chosen_action", {issue.code for issue in validate_record(illegal)}
        )

        wrong_type = valid_record(
            chosen_action={"action_id": "end_turn", "action_type": "play_card"}
        )
        self.assertIn(
            "action_type_mismatch", {issue.code for issue in validate_record(wrong_type)}
        )

    def test_seed_leakage_is_case_and_whitespace_insensitive(self) -> None:
        train = valid_record(seed=" ab12cd34 ", split="train")
        test = valid_record(seed="AB12CD34", split="test")
        issues = find_seed_leaks([(1, train), (2, test)])
        self.assertEqual([issue.code for issue in issues], ["seed_leakage"])

    def test_same_seed_within_one_split_is_not_leakage(self) -> None:
        first = valid_record(seed=1234, split="train")
        second = valid_record(seed="1234", split="train")
        self.assertEqual(find_seed_leaks([(1, first), (2, second)]), [])

    def test_jsonl_reports_parse_errors_and_cross_file_leakage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            train_path = root / "train.jsonl"
            test_path = root / "test.jsonl"
            train_path.write_text(
                json.dumps(valid_record(split="train")) + "\n{bad json}\n",
                encoding="utf-8",
            )
            test_path.write_text(
                json.dumps(valid_record(split="test")) + "\n",
                encoding="utf-8",
            )
            report = validate_files([train_path, test_path])

        codes = {issue.code for issue in report.issues}
        self.assertIn("invalid_json", codes)
        self.assertIn("seed_leakage", codes)
        self.assertFalse(report.ok)


if __name__ == "__main__":
    unittest.main()

