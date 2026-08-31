from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

from training.trace_contract import (
    CURRENT_PUBLIC_BETA_BUILD,
    DEFAULT_ASCENSION,
    DEFAULT_CHARACTER,
    ValidationIssue,
    find_seed_leaks,
    validate_record,
)


@dataclass(frozen=True)
class ValidationReport:
    files: int
    nonempty_lines: int
    parsed_records: int
    valid_records: int
    splits: dict[str, int]
    issues: list[ValidationIssue]

    @property
    def ok(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["ok"] = self.ok
        return payload


def validate_files(
    paths: Sequence[Path],
    *,
    expected_build: str | None = CURRENT_PUBLIC_BETA_BUILD,
    expected_character: str | None = DEFAULT_CHARACTER,
    expected_ascension: int | None = DEFAULT_ASCENSION,
    require_no_save_load: bool = True,
) -> ValidationReport:
    issues: list[ValidationIssue] = []
    parsed: list[tuple[int, dict[str, Any]]] = []
    split_counts: Counter[str] = Counter()
    nonempty_lines = 0
    globally_unique_line = 0

    for path in paths:
        with path.open("r", encoding="utf-8-sig") as handle:
            for source_line, raw_line in enumerate(handle, start=1):
                if not raw_line.strip():
                    continue
                nonempty_lines += 1
                globally_unique_line += 1
                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    issues.append(
                        ValidationIssue(
                            line=globally_unique_line,
                            code="invalid_json",
                            path=f"{path}:{source_line}",
                            message=f"{exc.msg} at column {exc.colno}",
                        )
                    )
                    continue

                record_issues = validate_record(
                    record,
                    line=globally_unique_line,
                    expected_build=expected_build,
                    expected_character=expected_character,
                    expected_ascension=expected_ascension,
                    require_no_save_load=require_no_save_load,
                )
                issues.extend(record_issues)
                if isinstance(record, dict):
                    parsed.append((globally_unique_line, record))
                    split = record.get("split")
                    if isinstance(split, str):
                        split_counts[split] += 1

    issues.extend(find_seed_leaks(parsed))

    invalid_lines = {issue.line for issue in issues}
    valid_records = sum(1 for line, _ in parsed if line not in invalid_lines)
    return ValidationReport(
        files=len(paths),
        nonempty_lines=nonempty_lines,
        parsed_records=len(parsed),
        valid_records=valid_records,
        splits=dict(sorted(split_counts.items())),
        issues=issues,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate local STS2 decision-trace JSONL files and detect seed leakage"
    )
    parser.add_argument("paths", nargs="+", type=Path, help="JSONL files to validate")
    parser.add_argument(
        "--expected-build",
        default=CURRENT_PUBLIC_BETA_BUILD,
        help="required build string; pass 'any' to disable build pinning",
    )
    parser.add_argument(
        "--character",
        default=DEFAULT_CHARACTER,
        help="required character id; pass 'any' to disable character pinning",
    )
    parser.add_argument("--ascension", type=int, default=DEFAULT_ASCENSION)
    parser.add_argument(
        "--allow-save-load",
        action="store_true",
        help="accept traces where save_load_used is true (not valid for NOSL acceptance)",
    )
    parser.add_argument("--json", action="store_true", help="emit a machine-readable report")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    missing = [path for path in args.paths if not path.is_file()]
    if missing:
        for path in missing:
            print(f"missing file: {path}", file=sys.stderr)
        return 2

    report = validate_files(
        args.paths,
        expected_build=None if args.expected_build == "any" else args.expected_build,
        expected_character=None if args.character == "any" else args.character,
        expected_ascension=args.ascension,
        require_no_save_load=not args.allow_save_load,
    )
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        status = "PASS" if report.ok else "FAIL"
        print(
            f"{status}: files={report.files} records={report.parsed_records} "
            f"valid={report.valid_records} splits={report.splits} issues={len(report.issues)}"
        )
        for issue in report.issues:
            print(issue.render())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

