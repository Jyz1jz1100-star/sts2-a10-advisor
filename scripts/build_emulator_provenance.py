"""Bind the report's engine findings to the exact emulator build they were read out of.

Every claim this repository makes about the simulator cites a source line -- ``RunEngine.cs:1286-1293``
for the loss-that-reports-Complete defect, ``RunRewardGenerator.cs:1127-1131`` for the upgrade stub.
A line number is a pointer into a file nobody hashed, and the emulator tree is not a git repository,
so there is no commit to point at either. The emulator's own guard compares the built library's
*mtime* against the newest source, which catches a rebuild that was forgotten but not a source that
changed under a preserved timestamp -- and this project has already been burned once by a build
changing underneath it (Workshop auto-update, commit 57f1055).

So this hashes the build input instead of dating it: the whole ``src/Sts2Emulator`` source tree as one
digest, the ``.cs`` files the report cites individually, the native library the evaluation actually
loads, and -- the part that makes a citation checkable -- the exact text sitting at each cited line
range. Regenerate after editing the report or the engine; a mismatch is drift, not a stale number.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EMULATOR = ROOT.parent / "third_party" / "slay-the-spire-2-emulator-main"
CITATION = re.compile(r"`?([A-Za-z0-9_.\-]+\.cs):(\d+)(?:-(\d+))?`?")
NATIVE_API_VERSIONS = {"Sts2_NativeApiVersion": 10, "Sts2Run_NativeApiVersion": 8}


def digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_files(emulator: Path) -> list[Path]:
    root = emulator / "src" / "Sts2Emulator"
    return sorted(
        path for path in root.rglob("*.cs")
        if "bin" not in path.parts and "obj" not in path.parts)


def tree_digest(emulator: Path, files: list[Path]) -> str:
    """One hash over every engine source file, in the manifest's ``path  hash`` line convention.

    Sorted here rather than by the caller: a digest that depended on directory-walk order would
    change when nothing changed.
    """
    ordered = sorted(files, key=lambda p: p.relative_to(emulator).as_posix())
    joined = "".join(
        f"{f.relative_to(emulator).as_posix()}  {digest_file(f)}\n" for f in ordered)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def cited_text(path: Path, first: int, last: int) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    if first > len(lines) or last > len(lines) or first < 1:
        return ""
    return " ".join(line.strip() for line in lines[first - 1:last] if line.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emulator", type=Path, default=DEFAULT_EMULATOR)
    parser.add_argument("--report", type=Path,
                        default=ROOT / "docs/ACT1_CAMPAIGN_2026-09-19.md")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    emulator = args.emulator.resolve()
    report = args.report.read_text(encoding="utf-8")
    matches = CITATION.findall(report)
    located: dict[str, list[Path]] = {}
    for name in sorted({m[0] for m in matches}):
        located[name] = sorted(
            p for p in (emulator / "src").rglob(name)
            if "bin" not in p.parts and "obj" not in p.parts)

    unresolved = {name: paths for name, paths in located.items() if len(paths) != 1}
    files = []
    for name, paths in sorted(located.items()):
        if len(paths) != 1:
            continue
        path = paths[0]
        files.append({"file": name,
                      "path": path.relative_to(emulator).as_posix(),
                      "sha256": digest_file(path),
                      "bytes": path.stat().st_size,
                      "lines": len(path.read_text(encoding="utf-8").splitlines())})

    citations = []
    for name, first, last in matches:
        first = int(first)
        last = int(last or first)
        path = located.get(name, [None])[0] if len(located.get(name, [])) == 1 else None
        text = cited_text(path, first, last) if path else ""
        citations.append({"file": name, "first_line": first, "last_line": last,
                          "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                          "snippet": text[:90],
                          "resolved": bool(text)})

    all_sources = source_files(emulator)
    library = emulator / "out" / "Sts2Emulator.dll"
    newest_source = max(all_sources, key=lambda p: p.stat().st_mtime) if all_sources else None
    payload = {
        "aggregates": {
            "citation_mentions": len(citations),
            "citations_resolved": sum(1 for c in citations if c["resolved"]),
            "cited_files": len(files),
            "cited_files_unresolved": sorted(unresolved),
            "engine_source_files_hashed": len(all_sources),
        },
        "citations": citations,
        "emulator_layout": "src/Sts2Emulator (excludes bin/ and obj/)",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "native_api": {
            "required_versions": NATIVE_API_VERSIONS,
            "library": {"bytes": library.stat().st_size if library.is_file() else None,
                        "path": "out/Sts2Emulator.dll",
                        "present": library.is_file(),
                        "sha256": digest_file(library) if library.is_file() else None},
            # The emulator's own staleness guard is an mtime comparison; recording both makes the
            # difference between "newer than" and "identical to" visible instead of assumed.
            "mtime_guard_newest_source": (
                f"{newest_source.relative_to(emulator).as_posix()} @ "
                f"{datetime.fromtimestamp(newest_source.stat().st_mtime, UTC).isoformat(timespec='seconds')}"
                if newest_source else None),
        },
        "cited_files": files,
        "not_established": [
            "that this tree is the one that produced the committed numbers -- it is the tree that "
            "resolves the report's citations today, and a drift in the engine now shows as DRIFT "
            "rather than as a silently re-pointed line number",
            "anything about the shipped game or the in-game mod build; this hashes the separate "
            "third_party emulator checkout",
            "the citations this repository makes to its own Python files (``*.py:line``): those "
            "live in a git-tracked tree, so their history is ``git log -L``, not this artifact"],
        "scope": ("the third_party slay-the-spire-2-emulator-main checkout this repository's "
                  "scripts import, plus every *.cs:line citation the campaign report carries"),
        "source_tree_digest": tree_digest(emulator, all_sources),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    agg = payload["aggregates"]
    print(f"citations: {agg['citations_resolved']}/{agg['citation_mentions']} resolved across "
          f"{agg['cited_files']} files; unresolved names {agg['cited_files_unresolved']}")
    print(f"engine source tree: {agg['engine_source_files_hashed']} files, "
          f"digest {payload['source_tree_digest'][:16]}...")
    print(f"native library present: {payload['native_api']['library']['present']} "
          f"{(payload['native_api']['library']['sha256'] or '')[:16]}...")
    print(f"wrote {args.out}")
    return 1 if agg["citations_resolved"] != agg["citation_mentions"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
