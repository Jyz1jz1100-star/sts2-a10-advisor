"""Merge BC sample files from multiple teacher sources, deduplicated.

Search batches and DAgger batches both materialize through
``training.teacher_bc_dataset``; a distillation round consumes the union.
Dedup key is ``(prefix_sha256, decision_index)`` — the same decision state
may legitimately appear in both sources (teacher traversal and student
traversal), and when it does the sources must *agree*; disagreement is a
hard error because both went through replay-verified labelling.

Output keeps every sample's provenance (``source`` is copied from the record
manifest by the caller convention: search records have no ``source`` field,
DAgger records carry ``"dagger"``).  Usage::

    python -m training.bc_dataset_merge \
        --inputs batch0_bc.jsonl batch1_bc.jsonl dagger0_bc.jsonl \
        --out combined_bc.jsonl --summary combined.summary.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def _prefer(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Order-independent winner between two labels of the same verified state.

    The teacher is deterministic, so equal budget means equal label (checked
    by the caller); when the same state arrives from both sources the
    *higher* score gap is the more confident label, and a DAgger record wins
    ties because student-distribution coverage is what DAgger adds.
    """

    gap_left = float(left.get("score_gap") or 0.0)
    gap_right = float(right.get("score_gap") or 0.0)
    if gap_left != gap_right:
        return left if gap_left > gap_right else right
    left_is_dagger = left.get("source") == "dagger"
    right_is_dagger = right.get("source") == "dagger"
    if left_is_dagger != right_is_dagger:
        return left if left_is_dagger else right
    return left


def merge_sample_files(inputs: list[Path], out: Path) -> dict[str, Any]:
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    duplicates = 0
    conflicts: list[dict[str, Any]] = []
    per_file: dict[str, int] = {}
    for path in inputs:
        kept = 0
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip():
                continue
            sample = json.loads(line)
            key = (str(sample["prefix_sha256"]), str(sample.get("decision_index", "")))
            previous = seen.get(key)
            if previous is not None:
                duplicates += 1
                if int(previous["label_flat_action"]) != int(sample["label_flat_action"]):
                    conflicts.append({
                        "key": key,
                        "labels": [previous["label_flat_action"],
                                   sample["label_flat_action"]],
                        "sources": [previous.get("source"), sample.get("source")],
                    })
                    raise ValueError(
                        f"label conflict for {key}: both sources were "
                        "replay-verified; refusing to merge"
                    )
                winner = _prefer(previous, sample)
                if winner is not previous:
                    seen[key] = winner
                continue
            seen[key] = sample
            kept += 1
        per_file[str(path)] = kept

    out.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with out.open("w", encoding="utf-8") as handle:
        for key in sorted(seen):
            line = json.dumps(seen[key], ensure_ascii=False, sort_keys=True) + "\n"
            handle.write(line)
            digest.update(line.encode("utf-8"))
    phases: dict[str, int] = {}
    sources: dict[str, int] = {}
    for sample in seen.values():
        phase = str(sample["phase"])
        phases[phase] = phases.get(phase, 0) + 1
        source = str(sample.get("source", "teacher"))
        sources[source] = sources.get(source, 0) + 1
    return {
        "inputs": {str(path): per_file[str(path)] for path in inputs},
        "merged_samples": len(seen),
        "duplicate_states": duplicates,
        "label_conflicts": len(conflicts),
        "phase_counts": dict(sorted(phases.items())),
        "source_counts": dict(sorted(sources.items())),
        "dataset_sha256": digest.hexdigest(),
        "out": str(out),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", nargs="+", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args(argv)
    summary = merge_sample_files(args.inputs, args.out)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    if args.summary:
        args.summary.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
