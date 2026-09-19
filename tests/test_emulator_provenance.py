"""The provenance builder's two primitives have to fail loudly, because everything downstream
reads a mismatch as engine drift.

``cited_text`` is the function that decides whether a ``File.cs:12-34`` citation still points at
the code the report describes. If it returned a slice of an unrelated region, or an empty string
indistinguishable from "the file says nothing there", a moved file would keep verifying.
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "build_emulator_provenance",
    Path(__file__).resolve().parents[1] / "scripts" / "build_emulator_provenance.py")
M = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(M)


class CitedTextTests(unittest.TestCase):
    def test_a_range_returns_its_own_lines_with_the_indentation_stripped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Run.cs"
            path.write_text("class A\n{\n    int B = 1;\n    int C = 2;\n}\n", encoding="utf-8")
            self.assertEqual("int B = 1; int C = 2;", M.cited_text(path, 3, 4))

    def test_a_range_past_the_end_is_refused_instead_of_reading_as_empty_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Run.cs"
            path.write_text("only one line\n", encoding="utf-8")
            self.assertEqual("", M.cited_text(path, 9, 12))
            self.assertEqual("", M.cited_text(path, 0, 1))

    def test_the_tree_digest_follows_content_and_not_the_clock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src" / "Sts2Emulator" / "Core").mkdir(parents=True)
            first = root / "src" / "Sts2Emulator" / "Core" / "A.cs"
            second = root / "src" / "Sts2Emulator" / "Core" / "B.cs"
            first.write_text("one\n", encoding="utf-8")
            second.write_text("two\n", encoding="utf-8")
            files = M.source_files(root)
            self.assertEqual(2, len(files), "bin/obj-free discovery must see both sources")
            stable = M.tree_digest(root, files)
            self.assertEqual(stable, M.tree_digest(root, sorted(files, reverse=True)))
            second.write_text("changed\n", encoding="utf-8")
            self.assertNotEqual(stable, M.tree_digest(root, M.source_files(root)))


if __name__ == "__main__":
    unittest.main()
