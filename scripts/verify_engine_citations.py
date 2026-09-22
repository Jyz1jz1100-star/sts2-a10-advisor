"""Fail when an engine citation in the campaign report no longer sits on the code it claims.

The provenance snapshot hashes the text at whatever line the report already names, so it catches the
engine changing and not a pointer sliding: fidelity v2 shipped 25 of 66 citations sitting off their
own claim with every check green. The repair was an anchor per pointer, and the anchor table lives in
one place -- ``tests/test_emulator_provenance.py::CITATION_ANCHORS`` -- because a copy in a script is
a second thing to keep in sync and it can agree with itself while both are wrong.

This is the checker side of that: every claimed pointer must appear in the report and its lines must
hold the anchor. When it fails, the fix is to move the number in the report *and* the table together,
after reading the prose -- not to move the number alone.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "build_emulator_provenance", ROOT / "scripts" / "build_emulator_provenance.py")
builder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(builder)

from tests.test_emulator_provenance import (  # noqa: E402
    CITATION_ANCHORS, REPORT, engine_source, locate)

NUMBER = re.compile(r"(\d+)(?:-(\d+))?\Z")


def main() -> int:
    report = REPORT.read_text(encoding="utf-8")
    problems = []
    for token, anchor in CITATION_ANCHORS.items():
        name, first, last = locate(token)
        path = engine_source(name)
        if path is None:
            problems.append(f"{token}: {name} is not uniquely findable in the engine tree")
            continue
        window = builder.cited_text(path, first, last)
        if anchor not in window:
            problems.append(f"{token}: reads {window[:70]!r}, the prose claims {anchor!r}")
        if token not in report:
            problems.append(f"{token}: in the anchor table but not carried by the report")
    if problems:
        print("CITATION DRIFT:", *problems, sep="\n  ")
        print("move the number in the report *and* the table together, after reading the prose")
        return 1
    print(f"{len(CITATION_ANCHORS)} anchored engine citations all sit on the code their prose claims")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
