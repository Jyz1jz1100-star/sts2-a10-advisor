"""Re-verify every hash recorded in a run artifact against the file it names.

The campaign's evidence is a chain of recorded digests: metrics files store
``checkpoint_sha256`` and ``seed_sha256``, plans store the warm-start parent's
digest, ``origin.json`` stores the previous stage's, and the probes store the
seed list's.  Nothing checks them after the fact, so "hash chained" currently
means "somebody wrote hashes down".  This closes that gap with one command:

    python scripts/verify_run_artifact_hashes.py --root runtime --root runs

Each artifact is checked for (a) the named file still existing, (b) its digest
matching what was recorded, and (c) a recorded seed digest matching a digest
recomputed over the seed list the artifact can reconstruct.  Results are
reported as verified / mismatched / unresolvable, and unresolvable is called out
separately because a missing file is not the same finding as a wrong hash.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: artifact key -> the key that names the file it was computed over
PATH_KEYS = {
    "checkpoint_sha256": "checkpoint",
    "source_sha256": "source",
    "initialized_from_sha256": "initialized_from",
    "resumed_from_sha256": "resumed_from",
}
SKIP_DIR_NAMES = {".venv", "__pycache__", ".git", "node_modules"}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _candidate(path: Path, artifact: Path) -> Path | None:
    """Resolve a recorded path, which may be absolute or repo-relative."""
    for candidate in (path, ROOT / path, artifact.parent / path):
        try:
            if candidate.exists():
                return candidate.resolve()
        except OSError:
            continue
    return None


def _check_seeds(artifact: Path, payload: dict) -> str:
    """Recompute a recorded seed digest where the artifact can rebuild the list.

    Two conventions exist in the repo and share the key name ``seed_sha256``:
    the canonical ``training.seeds.seed_digest`` over ``"1,2,3"``, and
    ``evaluate_teacher_strength._seed_digest`` over ``json.dumps(seeds)``.
    Both are recognised here; a mismatch is only reported when neither fits.
    """
    recorded = payload.get("seed_sha256")
    if not isinstance(recorded, str):
        return "none"
    seeds = payload.get("seeds")
    if not (isinstance(seeds, list) and seeds and all(isinstance(s, int) for s in seeds)):
        return "unverifiable"
    encodings = {
        "csv": ",".join(str(seed) for seed in seeds).encode("ascii"),
        "json-list": json.dumps(seeds, separators=(",", ":")).encode("utf-8"),
    }
    for name, payload_bytes in encodings.items():
        if hashlib.sha256(payload_bytes).hexdigest() == recorded:
            return f"ok:{name}"
    return "MISMATCH"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, action="append",
                        help="directory tree to scan; default runtime/ and runs/")
    parser.add_argument("--strict", action="store_true",
                        help="exit non-zero if anything mismatches")
    args = parser.parse_args()

    roots = args.root or [Path("runtime"), Path("runs")]
    roots = [root if root.is_absolute() else ROOT / root for root in roots]
    files: list[Path] = []
    for root in roots:
        root = root if root.is_absolute() else ROOT / root
        if not root.exists():
            print(f"skip missing root {root}")
            continue
        files.extend(
            p for p in root.rglob("*.json")
            if not SKIP_DIR_NAMES.intersection(set(p.parts))
        )

    tally: Counter[str] = Counter()
    problems: list[str] = []
    seed_states: Counter[str] = Counter()
    for path in sorted(set(files)):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        records = []
        for digest_key, file_key in PATH_KEYS.items():
            if digest_key in payload and file_key in payload:
                records.append((digest_key, payload[file_key], payload[digest_key]))
        for entry in payload.get("results", []) or []:
            if isinstance(entry, dict):
                for digest_key, file_key in PATH_KEYS.items():
                    if digest_key in entry and file_key in entry:
                        records.append((digest_key, entry[file_key], entry[digest_key]))
        seed_state = _check_seeds(path, payload)
        seed_states[seed_state] += 1
        if seed_state == "MISMATCH":
            problems.append(
                f"SEED MISMATCH {path} seed_sha256={str(payload.get('seed_sha256'))[:16]} "
                f"matches neither the csv nor the json-list digest of its own seeds"
            )
        for digest_key, named, recorded in records:
            if not isinstance(recorded, str) or not isinstance(named, str):
                continue
            tally["checks"] += 1
            target = _candidate(Path(named), path)
            if target is None:
                tally["unresolvable"] += 1
                problems.append(f"UNRESOLVABLE {path.name}: {digest_key} names {named}")
                continue
            try:
                actual = _sha256(target)
            except OSError as error:
                tally["unreadable"] += 1
                problems.append(f"UNREADABLE  {target}: {error}")
                continue
            recorded_l, actual_l = recorded.lower(), actual.lower()
            # Exact, or a prefix of at least 16 hex chars.  A shorter "prefix"
            # would match by luck, which is worse than reporting nothing.
            if actual_l == recorded_l or (len(recorded_l) >= 16 and actual_l.startswith(recorded_l)):
                tally["verified"] += 1
            else:
                tally["MISMATCH"] += 1
                problems.append(
                    f"MISMATCH    {path} {digest_key} recorded={recorded[:16]} "
                    f"actual={actual[:16]} ({target})"
                )

    print(f"scanned {len(set(files))} json artifacts under {', '.join(str(r) for r in roots)}")
    for key in ("checks", "verified", "MISMATCH", "unresolvable", "unreadable"):
        if tally[key]:
            print(f"  {key:12} {tally[key]}")
    print(f"  seed digests: {dict(seed_states)}")
    for line in problems[:25]:
        print("  " + line)
    if len(problems) > 25:
        print(f"  ... {len(problems) - 25} more")
    if args.strict and (tally["MISMATCH"] or tally["unresolvable"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
