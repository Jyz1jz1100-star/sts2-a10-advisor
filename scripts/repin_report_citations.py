"""Move an engine citation's number in the report *and* the anchor table together, or refuse.

`scripts/verify_engine_citations.py` fails when a pointer no longer sits on the code its prose claims.
The repair is never "edit the number alone": this tool derives the new number from the anchor -- the
line of engine source the prose says is there -- and rewrites both halves in one step.

Rule, in order of authority:

0. a pointer whose window already reads its anchor is **not** a move candidate, however the other
   rules would place it -- the guard asks for the anchor inside the window, not a canonical window,
   and re-deriving a healthy pointer is how this tool used to trade two overlapping windows back and
   forth on every run;
1. the report already carries a pointer covering the anchor line -- the report was re-pinned first,
   so its number wins and only the table key moves;
2. the anchor matches exactly one engine line -- the new window starts there and keeps its height;
3. the anchor matches several lines -- pick the one closest to the local slide, interpolated from
   the anchors that resolved uniquely in the same file, and say so, because an ambiguous anchor is
   the case where guessing shipped fidelity v2's 25 pointers that resolved against unrelated code.

Refused, never guessed: an anchor that matches no line at all. That is code changed or deleted, not
code that slid -- the citations into the demo-seed / retained-trace branches deleted by
`sts2sim-campaign-fidelity-v5` are the standing example, and those were resolved by editing the prose
to say the code is gone. A move that would put two different anchors on one table key is also
refused: a duplicate key silently deletes a check, which is a gate that stopped being able to fail.

Dry run by default.

    python scripts/repin_report_citations.py            # show the proposed moves
    python scripts/repin_report_citations.py --apply    # rewrite report + table
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "build_emulator_provenance", ROOT / "scripts" / "build_emulator_provenance.py")
_builder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_builder)

from tests.test_emulator_provenance import (  # noqa: E402
    CITATION_ANCHORS, REPORT, BARE_FILE, locate)

__all__ = ["REPORT", "BARE_FILE", "engine_source", "resolve", "derive_moves", "apply_moves"]


def engine_source(name: str) -> Path | None:
    """The single engine file for a bare filename, or None if the tree is ambiguous or missing."""
    found = [p for p in (_builder.DEFAULT_EMULATOR / "src").rglob(name)
             if "bin" not in p.parts and "obj" not in p.parts]
    return found[0] if len(found) == 1 else None


def resolve(token: str) -> Path:
    """The engine file a pointer token refers to, bare pointers included."""
    name = token[:token.rindex(".cs") + 3] if ".cs" in token else BARE_FILE
    path = engine_source(name)
    if path is None:
        raise FileNotFoundError(f"{name} is not uniquely findable under the engine src/")
    return path


def _cited(name: str, first: int, last: int) -> str:
    """The engine text at a window, read exactly the way the guard reads it."""
    path = engine_source(name)
    return _builder.cited_text(path, first, last) if path else ""


def _re_token(token: str, name: str, at: int, height: int) -> str:
    last = at + height - 1
    if ".cs" in token:
        return f"{name}:{at}" if height == 1 else f"{name}:{at}-{last}"
    if token.startswith(":"):
        return f":{at}" if height == 1 else f":{at}-{last}"
    return f"同文件 {at}" if height == 1 else f"同文件 {at}-{last}"


def derive_moves() -> tuple[list[tuple[str, str, str]], list[tuple[str, str]]]:
    """``(moves, refusals)`` with ``moves = (old, new, how))`` -- nothing is written here."""
    report = REPORT.read_text(encoding="utf-8")
    files = {locate(t)[0]: None for t in CITATION_ANCHORS}
    for name in files:
        path = engine_source(name)
        files[name] = path.read_text(encoding="utf-8", errors="replace").splitlines() if path else []

    carried: dict[str, list[tuple[int, int]]] = {}
    for name, first, last in re.findall(r"([A-Za-z]+\.cs):(\d+)(?:-(\d+))?", report):
        carried.setdefault(name, []).append((int(first), int(last) if last else int(first)))
    for first, last in re.findall(r"(?<![\w.]):(\d+)(?:-(\d+))?", report):
        carried.setdefault(BARE_FILE, []).append((int(first), int(last) if last else int(first)))

    anchors = {t: [i + 1 for i, line in enumerate(files[locate(t)[0]])
                   if CITATION_ANCHORS[t] in line] for t in CITATION_ANCHORS}
    observed = [(locate(t)[0], locate(t)[1], anchors[t][0] - locate(t)[1])
                for t in CITATION_ANCHORS if len(anchors[t]) == 1]

    def slide(name: str, old_first: int) -> int:
        near = sorted((abs(o - old_first), d) for (f, o, d) in observed if f == name)
        return near[0][1] if near else 0

    moves, refusals = [], []
    for token, anchor in CITATION_ANCHORS.items():
        name, first, last = locate(token)
        height = last - first + 1
        hits = anchors[token]
        current = _cited(name, first, last)
        if anchor in current:
            # Rule 0: a pointer that already reads its anchor is not broken. Re-deriving it anyway is
            # how this tool used to chase itself -- two overlapping windows covering the same anchor
            # would trade places on every run, both green, neither intended.
            refusals.append((token, "already sits on its anchor"))
            continue
        if not hits:
            refusals.append((token, "no engine line holds this anchor -- the code changed or was "
                                    "deleted, so this is prose work, not a re-pin"))
            continue
        if len(hits) == 1:
            at, how = hits[0], "anchor-unique"
        else:
            expected = first + slide(name, first)
            at = min(hits, key=lambda h: abs(h - expected))
            how = f"AMBIGUOUS: {len(hits)} hits, picked {at}, interpolated {expected}"
        covering = [(lo, hi) for (lo, hi) in carried.get(name, [])
                    if lo <= at <= hi and (lo, hi) != (first, first + height - 1)]
        if covering:
            lo, hi = min(covering, key=lambda w: abs(w[0] - at))
            at, height = lo, hi - lo + 1
            how += " | the report was re-pinned first, its number wins"
        new = _re_token(token, name, at, height)
        window = _cited(name, at, at + height - 1)
        if anchor not in window:
            refusals.append((token, f"the window at {new} does not read its anchor -- refused"))
        elif new == token:
            refusals.append((token, "already sits on its anchor"))
        else:
            moves.append((token, new, how))

    seen: dict[str, list[str]] = {}
    for old, new, _how in moves:
        seen.setdefault(new, []).append(old)
    safe = []
    for old, new, how in moves:
        if len(seen[new]) > 1:
            refusals.append((old, f"would collide onto key {new} with {seen[new]} -- "
                                  "a duplicate key silently drops a check"))
        else:
            safe.append((old, new, how))
    return safe, refusals


def apply_moves(moves: list[tuple[str, str, str]]) -> None:
    """Rewrite the report, then the table, and refuse to write a table with duplicate keys."""
    text = REPORT.read_text(encoding="utf-8")
    for old, new, _how in sorted(moves, key=lambda m: len(m[0]), reverse=True):
        if old.startswith(":"):        # bare pointers may also occur inside a named citation
            text, n = re.subn(r"(?<![\w.:-])" + re.escape(old) + r"(?![\d-])", new, text)
        else:
            n = text.count(old)
            text = text.replace(old, new)
        print(f"report: {old} -> {new} ({n}x)")
    REPORT.write_text(text, encoding="utf-8")

    path = ROOT / "tests" / "test_emulator_provenance.py"
    src = path.read_text(encoding="utf-8")
    for old, new, _how in sorted(moves, key=lambda m: len(m[0]), reverse=True):
        src = src.replace(f'    "{old}":', f'    "{new}":')
    keys = re.findall(r'^    "([^"]+)":', src, re.M)
    dupes = {k for k in keys if keys.count(k) > 1}
    if dupes:
        raise SystemExit(f"table would carry duplicate keys {dupes}; report is rewritten, aborting")
    path.write_text(src, encoding="utf-8")
    print(f"table rewritten ({len(keys)} keys, no duplicates)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="rewrite the report and the table")
    args = ap.parse_args()

    moves, refusals = derive_moves()
    for old, new, how in moves:
        print(f"{old:28s} -> {new:22s} {how}")
    for old, why in refusals:
        print(f"REFUSE {old:28s} {why}")
    print(f"\nmoves: {len(moves)}  refusals: {len(refusals)}")
    if not moves:
        return 0
    if args.apply:
        apply_moves(moves)
    else:
        print("dry run; pass --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
