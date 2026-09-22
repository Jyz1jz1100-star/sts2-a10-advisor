"""Re-point the campaign report's ``*.cs:line`` citations at the code the prose actually describes.

The fidelity-v2 publication regenerated the provenance snapshot without re-pinning the report, and
the digest check it feeds cannot catch that: it compares the text at each cited line against the
text recorded for that same line, so a pointer that slid a few lines sideways still "resolves" while
supporting nothing. Every citation here is therefore re-pinned to an anchor -- a fragment of the code
the prose claims -- and the run fails if the new location does not contain it.

The 2026-09-20 snapshot is the last build whose pointers were read by eye, so it is the baseline for
what each citation meant; where fidelity v2/v3 rewrote that code, the new range and the reason are
spelled out below rather than recovered by search.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "build_emulator_provenance", ROOT / "scripts/build_emulator_provenance.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)

EMULATOR = builder.DEFAULT_EMULATOR.resolve()
REPORT = ROOT / "docs/ACT1_CAMPAIGN_2026-09-19.md"

#: ``old citation -> (new citation, fragment the code there must contain)``.
REPINS: dict[str, tuple[str, str]] = {
    # -- moved, text unchanged: the v2/v3 edits grew the file above them --
    "RunEngine.cs:459-471": ("RunEngine.cs:465-477", "ApplyRetainedTraceVantomOpening"),
    "RunEngine.cs:690-695": ("RunEngine.cs:696-701", "State.MapNodeTypes[i] != RunConstants.NodeNone"),
    "RunEngine.cs:727": ("RunEngine.cs:733", "bool hasPotionSlot = State.PotionSlots.Any"),
    "RunEngine.cs:970": ("RunEngine.cs:973", "RunMapGenerator.ChooseMapNode"),
    "RunEngine.cs:1282": ("RunEngine.cs:1289", "State.LastPlayerWon = result.Terminal"),
    "RunEngine.cs:1286-1293": ("RunEngine.cs:1293-1300", "if (result.PlayerWon)"),
    "RunEngine.cs:1737": ("RunEngine.cs:1778", "State.RewardUpgraded[action]"),
    "RunEngine.cs:1909": ("RunEngine.cs:1953", "7MS1YN8NWB"),
    "RunEngine.cs:1953-1979": (
        "RunEngine.cs:2072-2098", "TryChooseRetainedTraceActTwoPath"),
    "RunEngine.cs:1957": ("RunEngine.cs:2076", 'StringSeed != "7MS1YN8NWB"'),
    "RunEngine.cs:2210": ("RunEngine.cs:2335", "new CardInstance("),
    # Two event arms with an identical five-line body; the case labels decide which is which.
    "RunEngine.cs:2671-2675": ("RunEngine.cs:2793-2800", "EventRanwidTheElder"),
    "RunEngine.cs:2772-2776": ("RunEngine.cs:2894-2901", "EventStoneOfAllTime"),
    "RunEngine.cs:3519": ("RunEngine.cs:3644", "State.PlayerHp > 8"),
    "RunEngine.cs:3543-3548": ("RunEngine.cs:3668-3673", "default:"),
    "RunMapGenerator.cs:189": (
        "RunMapGenerator.cs:283", "MapStartCol, RunConstants.MapBossRow"),
    "RunMapGenerator.cs:1127-1139": ("RunMapGenerator.cs:1162-1174", "RunConstants.NodeNone"),
    "RunRewardGenerator.cs:711-772": ("RunRewardGenerator.cs:716-777", "HasPendingRewards"),
    "RunRewardGenerator.cs:800": ("RunRewardGenerator.cs:805", "state.RewardUpgraded[i] ="),
    "RunRewardGenerator.cs:1015": (
        "RunRewardGenerator.cs:1020", "Math.Min(2, state.PotionSlots"),
    "RunRewardGenerator.cs:1127-1131": (
        "RunRewardGenerator.cs:1132-1136", "RollCardUpgrade"),
    # -- rewritten by fidelity v2/v3: the range is the successor, the prose says which --
    "RunEngine.cs:1905-1925": ("RunEngine.cs:1946-1972", "AdvanceAfterRelicReward"),
    "RunEngine.cs:1907-1920": (
        "RunEngine.cs:1950-1964", "CurrentNodeType == RunConstants.NodeBoss && !State.Campaign"),
    "RunEngine.cs:1907-1922": ("RunEngine.cs:1950-1968", "State.Phase = RunPhase.Complete"),
    "RunEngine.cs:1983-1997": ("RunEngine.cs:2102-2116", "private int AdvanceAfterNode"),
    "RunEngine.cs:1986-1989": (
        "RunEngine.cs:2114-2117", "MapBossRow * State.Act + 1"),
    "RunMapGenerator.cs:9-11": ("RunMapGenerator.cs:9-19", "actRng.NextBool()"),
}

#: ``同文件 1922-1923`` / ``:2263``-style pointers carry no filename, so the citation regex -- and
#: with it the whole provenance machinery -- never saw them rot. They continue a ``RunEngine.cs``
#: citation in their own paragraph, which is the only reason they can be resolved at all.
BARE_FILE = "RunEngine.cs"
BARE: dict[str, tuple[str, str]] = {
    "同文件 1922-1923": ("同文件 1966-1967", "State.Phase = RunPhase.Complete"),
    ":1746-1781": (":1787-1805", "ReturnToRewardScreenAfterCardReward"),
    ":1983-1997": (":2102-2116", "private int AdvanceAfterNode"),
    ":1984-1997": (":2114-2120", "MapBossRow * State.Act + 1"),
    ":2263": (":2388", "RunRewardGenerator.AddPotion"),
    ":2772-2776": (":2894-2901", "EventStoneOfAllTime"),
}


def resolve(citation: str) -> Path | None:
    name = citation.partition(":")[0]
    found = [
        path for path in (EMULATOR / "src").rglob(name)
        if "bin" not in path.parts and "obj" not in path.parts]
    return found[0] if len(found) == 1 else None


def check(citation: str, anchor: str) -> str:
    """Return the code at ``citation`` if it holds ``anchor``, else an error string."""
    name, _, span = citation.partition(":")
    first, _, last = span.partition("-")
    path = resolve(citation)
    if path is None:
        return f"{citation}: file not found"
    text = builder.cited_text(path, int(first), int(last or first))
    if anchor not in text:
        return f"{citation}: no {anchor!r} in {text[:90]!r}"
    return ""


def main() -> int:
    payload = json.loads((ROOT / "docs/evidence/emulator_source_provenance_20260920.json")
                         .read_text(encoding="utf-8"))
    baseline = {(c["file"], c["first_line"], c["last_line"]) for c in payload["citations"]}
    report = REPORT.read_text(encoding="utf-8")

    problems = [f"{new} {err}" for new, anchor in
                list(REPINS.values())
                + [(f"{BARE_FILE}:{re.sub(r'[^0-9-]+', '', target)}", anchor)
                   for target, anchor in BARE.values()]
                if (err := check(new, anchor))]
    # Completeness, not just correctness: a baseline pointer that no longer holds its own recorded
    # text and is not in the table above is one nobody looked at, which is how fidelity v2 shipped.
    by_key = {(c["file"], c["first_line"], c["last_line"]): c["text_sha256"]
              for c in payload["citations"]}
    unexamined = []
    for (name, first, last), want in sorted(by_key.items()):
        citation = f"{name}:{first}" + (f"-{last}" if last != first else "")
        path = resolve(citation)
        if path is None or citation in REPINS or citation not in report:
            continue
        got = hashlib.sha256(
            builder.cited_text(path, first, last).encode("utf-8")).hexdigest()
        if got != want:
            unexamined.append(citation)
    problems.extend(f"{cite}: drifted since 2026-09-20 and not in the table"
                    for cite in unexamined)
    # Every baseline pointer that is neither kept nor re-pinned is a pointer nobody looked at.
    untouched = sorted(
        f"{f}:{a}" + (f"-{b}" if b != a else "")
        for f, a, b in baseline
        if (f"{f}:{a}" + (f"-{b}" if b != a else "")) not in REPINS
        and (f"{f}:{a}" + (f"-{b}" if b != a else "")) not in report)
    if untouched:
        print(f"note: {len(untouched)} baseline citations absent from the report: {untouched}")

    pairs = {**{old: new for old, (new, _) in REPINS.items()},
             **{old: new for old, (new, _) in BARE.items()}}
    # Once applied, the superseded numbers are gone from the report, and that is the passing state --
    # so "not found" must never be an error here. What stays checkable forever is the anchor on the
    # new side, which `problems` already covers, and the completeness sweep above.
    stale = sorted(old for old in pairs if old in report)
    pattern = re.compile("|".join(
        re.escape(key) + r"(?![\d-])" for key in sorted(pairs, key=len, reverse=True)))
    hits: dict[str, int] = {}

    def swap(match: re.Match) -> str:
        key = match.group(0)
        hits[key] = hits.get(key, 0) + 1
        return pairs[key]

    rewritten = pattern.sub(swap, report)
    if problems:
        print("REFUSING TO WRITE:", *problems, sep="\n  ")
        return 1
    if not stale:
        print(f"nothing to re-pin: every one of the {len(pairs)} pointers holds its anchor and no "
              "superseded number is left in the report")
        return 0
    if "--apply" in sys.argv:
        REPORT.write_text(rewritten, encoding="utf-8")
    print(f"{'re-pinned' if '--apply' in sys.argv else 'would re-pin'} {len(stale)} pointers "
          f"in {sum(hits.values())} occurrences, every anchor verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
