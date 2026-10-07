"""#52's instrument must be able to FAIL, and its result is a split verdict, not a percentage.

Two runs of one seed agree on everything the seed owns (map path, room contents, offer lists,
encounter ids) and disagree inside fights, because the CombatSolver owns combat and searches under a
wall-clock budget. A tool that lumped those together would either overstate the seed ("runs are
reproducible!") or understate it ("rows differ"), so the comparison is split by screen class and the
combat frames are excluded from the content digest on purpose.

These tests exist because the earlier version of the control was tautological -- it compared a list
with itself, which cannot report a difference -- and a check that cannot fail is not evidence.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "compare_seeded_replays", ROOT / "scripts" / "compare_seeded_replays.py")
M = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(M)


def _row(screen="map", floor=3, offers=None, enemies=None, hp=70):
    return {"sequence": 1, "ts": "t", "act": 1, "floor": floor, "screen": screen, "room": None,
            "enemies": enemies or [], "offers": offers or {}, "hp": hp, "posted": None}


CONTENT_FIELDS = ("act", "floor", "screen", "room", "enemies", "offers")


class AgreementTests(unittest.TestCase):
    def test_identical_rows_agree(self) -> None:
        rows = [_row(), _row(floor=4)]
        got = M._agree(rows, [dict(r) for r in rows], CONTENT_FIELDS)
        self.assertEqual(got["differing"], 0, got)
        self.assertEqual(got["compared"], 2, got)

    def test_a_planted_offer_difference_is_detected_and_classified(self) -> None:
        a = [_row(offers={"cards": ["STRIKE_IRONCLAD", "DEFEND_IRONCLAD"]})]
        b = [_row(offers={"cards": ["STRIKE_IRONCLAD", "BASH"]})]
        got = M._agree(a, b, CONTENT_FIELDS)
        self.assertEqual(got["differing"], 1, got)
        self.assertEqual(M._classify(got["first"][1], got["first"][2]), "offer_contents")

    def test_a_planted_map_step_is_detected_as_a_path_divergence(self) -> None:
        got = M._agree([_row(floor=5)], [_row(floor=6)], CONTENT_FIELDS)
        self.assertEqual(got["differing"], 1, got)
        self.assertEqual(M._classify(got["first"][1], got["first"][2]), "map_path")

    def test_a_different_enemy_list_is_detected(self) -> None:
        got = M._agree([_row(enemies=["LAGAVULIN_0"])],
                       [_row(enemies=["CRUSHER_0", "ROCKET_0"])],
                       CONTENT_FIELDS)
        self.assertEqual(got["differing"], 1, got)
        self.assertEqual(M._classify(got["first"][1], got["first"][2]), "encounter")

    def test_hp_is_not_content_because_the_solver_owns_how_it_got_there(self) -> None:
        """Deliberate: HP at a decision frame is a consequence of the last fight.

        Counting it as content would make every seeded pair "diverge" for a reason that has nothing
        to do with the seed, and would let a reader conclude the run is not reproducible.
        """
        a, b = [_row(hp=70)], [_row(hp=41)]
        self.assertEqual(M._agree(a, b, CONTENT_FIELDS)["differing"], 0)
        self.assertEqual(M._agree(a, b, ("act", "floor", "screen", "hp"))["differing"], 1)

    def test_combat_frames_are_not_in_the_content_bucket(self) -> None:
        self.assertNotIn("monster", M.CONTENT_SCREENS)
        self.assertNotIn("boss", M.CONTENT_SCREENS)
        self.assertIn("map", M.CONTENT_SCREENS)
        self.assertIn("card_reward", M.CONTENT_SCREENS)


class CommittedMeasurementTests(unittest.TestCase):
    """The three pairs actually measured on 2026-10-07, read from the committed artifacts."""

    PAIRS = ("AB", "AC", "BC")

    def _load(self, pair: str) -> dict:
        path = ROOT / "docs" / "evidence" / f"seed_reproduction_{pair}_20261007.json"
        self.assertTrue(path.exists(), str(path))
        return json.loads(path.read_text(encoding="utf-8"))

    def test_every_pair_requested_the_same_registered_seed_on_the_same_build(self) -> None:
        for pair in self.PAIRS:
            d = self._load(pair)
            self.assertTrue(d["same_seed_requested"], pair)
            self.assertEqual(d["seed_a"], "1600000000", pair)
            self.assertTrue(d["same_build"], pair)

    def test_content_reproduced_in_every_pair(self) -> None:
        for pair in self.PAIRS:
            d = self._load(pair)
            self.assertEqual(d["verdict"], "content_identical", pair)
            self.assertEqual(d["content_agreement"]["differing"], 0, pair)
            self.assertGreaterEqual(d["content_agreement"]["compared"], 100,
                                    f"{pair}: a 132-row agreement is not vacuous")

    def test_combat_frames_diverged_and_the_tool_said_so_rather_than_hiding_it(self) -> None:
        for pair in self.PAIRS:
            d = self._load(pair)
            self.assertGreaterEqual(d["combat_agreement"]["differing"], 1, pair)
            self.assertNotEqual(d["whole_trace_verdict"], "identical", pair)

    def test_each_artifact_records_what_the_result_does_not_establish(self) -> None:
        for pair in self.PAIRS:
            lines = " ".join(self._load(pair)["not_established"])
            self.assertIn("combat", lines, pair)
            self.assertIn("shorter", lines, pair)


if __name__ == "__main__":
    unittest.main()
