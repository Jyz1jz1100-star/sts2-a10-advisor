"""The provenance builder's two primitives have to fail loudly, because everything downstream
reads a mismatch as engine drift.

``cited_text`` is the function that decides whether a ``File.cs:12-34`` citation still points at
the code the report describes. If it returned a slice of an unrelated region, or an empty string
indistinguishable from "the file says nothing there", a moved file would keep verifying.

The second class guards the failure the digests cannot see. A snapshot is regenerated from the
numbers the report already carries, so re-publishing after an engine edit records the *new* text at
the *old* line and every check stays green while the pointer moves off the code it describes --
which is how fidelity v2 shipped 25 citations whose line no longer held what the prose claimed.
Each pointer repaired in that clean-up is pinned here to a fragment of the code it is supposed to
land on, so a future slide fails this test instead of passing the digest.
"""

from __future__ import annotations

import importlib.util
import re
import tempfile
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "build_emulator_provenance",
    Path(__file__).resolve().parents[1] / "scripts" / "build_emulator_provenance.py")
M = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(M)

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "docs" / "ACT1_CAMPAIGN_2026-09-19.md"

#: ``citation -> code the cited lines must contain``, for every pointer the v3 clean-up moved.
#: Bare pointers (``:2388``, ``同文件 1966-1967``) carry no filename in prose; they continue a
#: ``RunEngine.cs`` citation in their own paragraph, so they are keyed by the token as written.
CITATION_ANCHORS: dict[str, str] = {
    "RunEngine.cs:465-477": "ApplyRetainedTraceVantomOpening",
    "RunEngine.cs:696-701": "State.MapNodeTypes[i] != RunConstants.NodeNone",
    "RunEngine.cs:733": "bool hasPotionSlot = State.PotionSlots.Any",
    "RunEngine.cs:973": "RunMapGenerator.ChooseMapNode",
    "RunEngine.cs:1289": "State.LastPlayerWon = result.Terminal",
    "RunEngine.cs:1293-1300": "if (result.PlayerWon)",
    "RunEngine.cs:1778": "State.RewardUpgraded[action]",
    "RunEngine.cs:1787-1805": "ReturnToRewardScreenAfterCardReward",
    "RunEngine.cs:2072-2098": "TryChooseRetainedTraceActTwoPath",
    "RunEngine.cs:2076": 'StringSeed != "7MS1YN8NWB"',
    "RunEngine.cs:3644": "State.PlayerHp > 8",
    "RunEngine.cs:3668-3673": "default:",
    # -- fidelity v2/v3 rewrote the code under these, so the range is the successor --
    "RunEngine.cs:1946-1972": "AdvanceAfterRelicReward",
    "RunEngine.cs:1950-1964": "CurrentNodeType == RunConstants.NodeBoss && !State.Campaign",
    "RunEngine.cs:1950-1968": "State.Phase = RunPhase.Complete",
    "RunEngine.cs:1953": "7MS1YN8NWB",
    "RunEngine.cs:2102-2116": "private int AdvanceAfterNode",
    "RunEngine.cs:2114-2117": "MapBossRow * State.Act + 1",
    "RunEngine.cs:2114-2120": "MapBossRow * State.Act + 1",
    "RunEngine.cs:2335": "new CardInstance(",
    "RunEngine.cs:2793-2800": "EventRanwidTheElder",
    "RunEngine.cs:2894-2901": "EventStoneOfAllTime",
    "RunMapGenerator.cs:9-19": "actRng.NextBool()",
    "RunMapGenerator.cs:180": "SecondBossCoord",
    "RunMapGenerator.cs:283": "MapStartCol, RunConstants.MapBossRow",
    "RunMapGenerator.cs:1162-1174": "RunConstants.NodeNone",
    "RunRewardGenerator.cs:716-777": "HasPendingRewards",
    "RunRewardGenerator.cs:805": "state.RewardUpgraded[i] =",
    "RunRewardGenerator.cs:1020": "Math.Min(2, state.PotionSlots",
    "RunRewardGenerator.cs:1132-1136": "RollCardUpgrade",
    # -- bare pointers: invisible to the provenance regex, so only this table can catch them --
    ":2388": "RunRewardGenerator.AddPotion",
    ":2894-2901": "EventStoneOfAllTime",
    "同文件 1966-1967": "State.Phase = RunPhase.Complete",
}

#: Every pointer in :data:`CITATION_ANCHORS` that is written without its filename.
BARE_FILE = "RunEngine.cs"


def engine_source(name: str) -> Path | None:
    found = [path for path in (M.DEFAULT_EMULATOR / "src").rglob(name)
             if "bin" not in path.parts and "obj" not in path.parts]
    return found[0] if len(found) == 1 else None


def locate(token: str) -> tuple[str, int, int]:
    """``(file, first line, last line)`` for an anchored token, bare pointers included."""
    lines = re.search(r"(\d+)(?:-(\d+))?\Z", token)
    assert lines, token
    name = token[:token.rindex(".cs") + 3] if ".cs" in token else BARE_FILE
    return name, int(lines.group(1)), int(lines.group(2) or lines.group(1))



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


class CitationAnchorTests(unittest.TestCase):
    """A pointer must hold the code its prose claims, not merely resolve to something."""

    def setUp(self) -> None:
        if not (M.DEFAULT_EMULATOR / "src" / "Sts2Emulator").is_dir():
            self.fail(
                f"no engine source under {M.DEFAULT_EMULATOR}: the citation anchors below are read "
                "from the real tree, so a missing checkout has to stop the run rather than pass")

    def test_every_anchored_pointer_holds_the_code_the_report_claims(self) -> None:
        for token, anchor in CITATION_ANCHORS.items():
            with self.subTest(citation=token):
                name, first, last = locate(token)
                path = engine_source(name)
                self.assertIsNotNone(path, f"{name} is not uniquely findable in the engine tree")
                text = M.cited_text(path, first, last)
                self.assertIn(
                    anchor, text,
                    f"{token} reads {text[:90]!r}; the report says it is {anchor!r}. Re-pin it with "
                    "runtime/repin_citations.py's anchor rule rather than moving the number alone.")

    def test_the_report_still_carries_every_anchored_pointer(self) -> None:
        report = REPORT.read_text(encoding="utf-8")
        missing = [token for token in CITATION_ANCHORS if token not in report]
        self.assertEqual(
            [], missing,
            "these pointers left the report: a re-pin that silently moved them back is exactly how "
            "fidelity v2 shipped numbers that resolved against the wrong code")

    def test_bare_pointers_still_sit_next_to_the_file_they_borrow(self) -> None:
        # A bare pointer is invisible to the provenance regex, so its file association is the only
        # thing here that could quietly stop being true: if the paragraph above it changes to cite
        # another file, the anchor this table checks would be pointing at the wrong source.
        lines = REPORT.read_text(encoding="utf-8").splitlines()
        for token in (t for t in CITATION_ANCHORS if ".cs" not in t):
            with self.subTest(token=token):
                at = [i for i, line in enumerate(lines) if token in line]
                self.assertTrue(at, f"{token} left the report")
                for row in at:
                    context = " ".join(lines[max(0, row - 3):row + 1])
                    self.assertIn(f"{BARE_FILE}:", context,
                                  f"{token} no longer continues a {BARE_FILE} citation")


if __name__ == "__main__":
    unittest.main()
