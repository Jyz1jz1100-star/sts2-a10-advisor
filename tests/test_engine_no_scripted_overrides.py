"""The shipped engine must not carry run-state overrides keyed on a snapshot.

Removed 2026-09-23: a "retained trace" subsystem replayed one recorded
playthrough by testing the live state against the trace's exact floor plus HP and
gold values and then overwriting combat results, rewards, routing and events.
41 of its branches also compared `State.StringSeed` against a golden run code, but
the Python environment passes ``str(int_seed)``, so that comparison can never match
and those branches guarded nothing we were protected by -- the other 54 applied to
every training seed.  One of them answered any targeted play of hand slot 4 in the
Act-1 floor-6 Punch Construct with a finished, won combat, which is a reward an
agent can look up rather than earn.

The invariant is therefore about shape, not about the two seed strings: a floor
number conjoined with an exact HP or gold value, in a branch that writes run or
combat state, is a snapshot test masquerading as a game rule.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EMULATOR = ROOT.parent / "third_party" / "slay-the-spire-2-emulator-main"
ENGINE_SRC = EMULATOR / "src" / "Sts2Emulator"

GOLDEN_SEED_CODES = ("7MS1YN8NWB", "FKSYQMYRRV")

FLOOR_TEST = re.compile(r"\bFloor\s*==\s*\d+")
SNAPSHOT_TEST = re.compile(r"\b(?:PlayerHp|Gold|PlayerGold)\s*==\s*\d+")
STATE_WRITE = re.compile(
    r"\b(?:PlayerHp|PlayerGold|Gold|Terminal|PlayerWon|Hp|MaxHp|EventId"
    r"|CurrentNodeType|CurrentMapCoord|EncounterId|RewardCards|RewardGold|RelicReward)\s*="
    r"|result with|\.Hp = 0"
)
PREPROCESSOR = re.compile(r"^\s*#(if|elif|else|endif)\b")


def engine_sources() -> list[Path]:
    if not ENGINE_SRC.is_dir():
        raise unittest.SkipTest(f"engine sources not installed at {ENGINE_SRC}")
    return sorted(p for p in ENGINE_SRC.rglob("*.cs") if "obj" not in p.parts)


def brace_end(lines: list[str], start: int) -> int:
    depth = 0
    seen = False
    for i in range(start, len(lines)):
        depth += lines[i].count("{") - lines[i].count("}")
        if "{" in lines[i]:
            seen = True
        if seen and depth == 0:
            return i
    return len(lines) - 1


def if_branches(path: Path):
    """Yield (line_number, condition, body) for every `if (` in the file."""
    lines = path.read_text(encoding="utf-8").split("\n")
    for index, line in enumerate(lines):
        if line.strip() != "if (" and not line.strip().startswith("if ("):
            continue
        depth = 0
        cursor = index
        while cursor < len(lines):
            depth += lines[cursor].count("(") - lines[cursor].count(")")
            if depth <= 0 and ")" in lines[cursor]:
                break
            cursor += 1
        condition = "\n".join(lines[index : cursor + 1])
        end = brace_end(lines, cursor)
        yield index + 1, condition, "\n".join(lines[cursor + 1 : end + 1])


class NoScriptedRunOverridesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sources = engine_sources()

    def test_no_branch_writes_run_state_from_a_floor_and_exact_value_test(self) -> None:
        offenders = []
        for path in self.sources:
            for number, condition, body in if_branches(path):
                if FLOOR_TEST.search(condition) and SNAPSHOT_TEST.search(condition):
                    if STATE_WRITE.search(body):
                        offenders.append(
                            "%s:%d" % (path.name, number)
                        )
        self.assertEqual(
            offenders,
            [],
            "state overrides keyed on an exact floor/HP/gold snapshot have returned; "
            "these replay one recorded run instead of modelling a rule",
        )

    def test_the_engine_does_not_special_case_a_recorded_run_code(self) -> None:
        found = []
        for path in self.sources:
            text = path.read_text(encoding="utf-8")
            for code in GOLDEN_SEED_CODES:
                if code in text:
                    found.append("%s:%s" % (path.name, code))
        self.assertEqual(
            found,
            [],
            "the engine compares StringSeed against a golden run code again; the run "
            "env passes str(int_seed), so such a branch is dead for training and its "
            "negated form is a silent behavioural fork",
        )

    def test_the_engine_ships_no_conditional_compilation(self) -> None:
        """The old overrides were reachable because nothing excluded them from the build.

        If a #if ever appears, an override could hide behind a configuration the
        gates do not build, and a green gate would stop meaning anything.
        """
        offenders = []
        for path in self.sources:
            for number, line in enumerate(
                path.read_text(encoding="utf-8").split("\n"), 1
            ):
                if PREPROCESSOR.search(line):
                    offenders.append("%s:%d %s" % (path.name, number, line.strip()))
        self.assertEqual(
            offenders,
            [],
            "conditional compilation added to the shipped engine; a build-time switch "
            "is not a substitute for removing scripted outcomes",
        )


if __name__ == "__main__":
    unittest.main()
