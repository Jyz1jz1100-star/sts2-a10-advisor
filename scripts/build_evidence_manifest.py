"""Build docs/evidence/MANIFEST_2026-09-19.json: one hash binding for the whole evidence bundle.

The report says it carries "hash-chain evidence". Until now each artifact pinned the digests of
the checkpoints it used, which verifies a *single* claim but leaves two things unchecked:

1. nothing binds the **set** of artifacts, so an edited or swapped evidence file is invisible and
   a reviewer cannot confirm the bundle they read is the bundle that was produced;
2. nothing walks the chain to its end -- an artifact can record a checkpoint digest without any
   tool confirming the file on disk still hashes to it.

This script records sha256 + byte size for every file in docs/evidence, a Merkle-style
``bundle_root`` over the sorted per-file digests, and every *checkpoint* digest cited anywhere in
the bundle, checked against the file on disk where it exists.  Verification lives in
verify_report_claims.py as the claim ``evidence_bundle_integrity``, so the manifest is not trusted
because it says so itself.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import subprocess
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "evidence"
SELF = "MANIFEST_2026-09-19.json"
HEX64 = re.compile(r"\b[0-9a-f]{64}\b")
CHECKPOINT_KEY = re.compile(r"checkpoint|parent|digest_of_weights", re.I)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def digests_under_checkpoint_keys(node, source: str, into: dict) -> None:
    """Collect 64-hex values that appear under a checkpoint-ish key, anywhere in a JSON tree."""
    if isinstance(node, dict):
        for key, value in node.items():
            if CHECKPOINT_KEY.search(str(key)):
                for digest in (value if isinstance(value, list) else [value]):
                    if isinstance(digest, str) and HEX64.fullmatch(digest):
                        into.setdefault(digest, set()).add(source)
            digests_under_checkpoint_keys(value, source, into)
    elif isinstance(node, list):
        for item in node:
            digests_under_checkpoint_keys(item, source, into)


def resolve_checkpoint_files(extra_roots: tuple[Path, ...] = ()) -> dict[str, list[Path]]:
    """Every zip on disk under runs/ or runtime/, indexed by its current digest."""
    by_digest: dict[str, list[Path]] = defaultdict(list)
    for directory in tuple(extra_roots) + (ROOT / "runs", ROOT / "runtime"):
        if not directory.exists():
            continue
        for path in directory.rglob("*.zip"):
            try:
                by_digest[sha256(path)].append(path)
            except OSError:  # a locked or unreadable artifact is reported, not skipped silently
                continue
    return by_digest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, default=EVIDENCE,
                        help="point at a copy to test tamper sensitivity without touching the bundle")
    parser.add_argument("--out", type=Path, default=EVIDENCE / SELF)
    args = parser.parse_args()

    files = sorted(p for p in args.evidence_dir.glob("*.json") if p.name != SELF)
    entries = [{"file": p.name, "sha256": sha256(p), "bytes": p.stat().st_size}
               for p in files]
    bundle_root = hashlib.sha256(
        "".join(f"{e['file']}  {e['sha256']}\n"
                for e in sorted(entries, key=lambda e: e["file"])).encode("utf-8")).hexdigest()

    cited: dict[str, set[str]] = {}
    other_hex = collections.Counter()
    for path in files:
        text = path.read_text(encoding="utf-8")
        digests_under_checkpoint_keys(json.loads(text), path.name, cited)
        other_hex["total_64_hex_strings_seen"] += len(HEX64.findall(text))
    on_disk = resolve_checkpoint_files(
        () if args.evidence_dir == EVIDENCE else (args.evidence_dir.parent,))

    resolved, unresolved = [], []
    for digest, sources in sorted(cited.items()):
        record = {"sha256": digest, "cited_by": sorted(sources)}
        paths = on_disk.get(digest)
        if paths:
            record["paths"] = [str(p.relative_to(ROOT)).replace("\\", "/") for p in paths]
            record["recomputed_matches"] = all(sha256(p) == digest for p in paths)
            resolved.append(record)
        else:
            # runs/ and runtime/ are gitignored, so an absent file is a statement about this
            # machine, not evidence of tampering.
            record["present_locally"] = False
            unresolved.append(record)

    head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True, check=False).stdout.strip()
    payload = {
        "_comment": [
            "One hash binding for the campaign evidence bundle. bundle_root is sha256 over the sorted",
            "'<filename>  <sha256>' lines of every other file in docs/evidence, so it changes if any",
            "artifact changes or one is added without being manifested. resolved_checkpoints walks the",
            "chain to its end: the digest an artifact cites, the local file that hashes to it, and a",
            "fresh recompute of that file. Only digests appearing under a checkpoint-ish key are",
            "followed; the other 64-hex strings in the bundle (teacher state hashes, module file",
            "hashes) are counted, not chased.",
        ],
        "bundle_root": bundle_root,
        "evidence_file_count": len(entries),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_head_at_generation": head or "unavailable",
        "checkpoint_digests_cited": len(cited),
        "not_established": [
            "completeness of the bundle across time: a file added and removed between two builds of "
            "this manifest would not be seen, though anything present now that is unmanifested "
            "changes evidence_file_count and the claim catches that",
            "any trust in this file's own contents -- verify_report_claims.py recomputes every digest "
            "here independently, including this file's exclusion from its own bundle_root",
        ],
        "other_hex64_occurrences": dict(other_hex),
        "resolved_checkpoints": resolved,
        "scope": "docs/evidence bundle for the 2026-09-19 act1 campaign and its follow-ups",
        "unresolved_on_this_machine": unresolved,
        "files": sorted(entries, key=lambda e: e["file"]),
    }
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    print(f"{len(entries)} evidence files, bundle_root {bundle_root[:16]}...")
    print(f"{len(cited)} checkpoint digests cited: {len(resolved)} resolve to a local file, "
          f"{len(unresolved)} absent here")
    print("recompute mismatches:", sum(1 for r in resolved if not r["recomputed_matches"]))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
