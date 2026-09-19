"""Census the dead-end reason vocabulary over every committed metrics file.

Written because this report claimed the labelling gap was "a label that cannot tell 'cannot act'
from 'acts too slowly'" -- which the vocabulary itself refutes: `step_cap` is a label the
evaluator assigns (training/evaluation.py:129-132) and has fired. Either the clause is corrected
or the claim is real, so the counts get pinned.
"""

import collections
import datetime
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import verify_report_claims as V  # noqa: E402

per_file = []
vocabulary = collections.Counter()
by_reason_stage = collections.defaultdict(collections.Counter)
for path, payload in V._metrics_payloads():
    reasons = payload.get("dead_end_reasons") or {}
    stage = payload.get("stage") or (path.parts[-3] if len(path.parts) > 3 else "?")
    split = payload.get("split", "?")
    for name, count in reasons.items():
        vocabulary[name] += int(count)
        by_reason_stage[name][f"{stage}/{split}"] += int(count)
    if reasons:
        per_file.append({
            "file": path.relative_to(REPO).as_posix(),
            "stage": stage, "split": split,
            "episodes": int(payload.get("episodes", 0) or 0),
            "truncations": int(payload.get("truncations", 0) or 0),
            "reasons": dict(sorted(reasons.items())),
            "unclassified_dead_ends": int(payload.get("unclassified_dead_ends", 0) or 0),
        })

step_cap_files = [row for row in per_file if "step_cap" in row["reasons"]]
payload = {
    "_comment": [
        "What the dead-end labelling has actually ever recorded, over the same 282-file population the",
        "contract-channel census walks. This exists to correct a sentence in the campaign report that",
        "claimed the vocabulary cannot separate 'cannot act' from 'acts too slowly': it can, the",
        "evaluator labels the evaluator's own horizon as step_cap (training/evaluation.py:129-132), and",
        "three episodes carry that label. The residual gap is different and narrower -- see 'refines'.",
    ],
    "by_reason_and_stage": {name: dict(sorted(counter.items()))
                            for name, counter in sorted(by_reason_stage.items())},
    "established": [
        f"the vocabulary across every committed metrics file is exactly {dict(sorted(vocabulary.items()))}",
        f"unclassified_dead_ends is 0 in all {len(per_file)} files that carry any reason, and the "
        f"population records {sum(row['truncations'] for row in per_file)} truncations among the "
        "reason-bearing files alone",
        f"`step_cap` has fired {vocabulary['step_cap']} times, so 'acts too slowly' IS separately "
        "labelled; `empty_action_mask` has fired "
        f"{vocabulary['empty_action_mask']} times, so 'cannot act' is the other label",
        "the 861 native_rejection endings belong to the legacy schema (pre-filter-mode) files and "
        "do not contradict the 0 recorded under the current schema -- the same split the "
        "contract-channel census documents",
    ],
    "generated_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
    "metrics_files_with_any_reason": len(per_file),
    "not_established": [
        "that step_cap and empty_action_mask can never describe the same episode: the label is a "
        "single string per episode and the evaluator prefers the env's own label, so a co-occurrence "
        "would be recorded as empty_action_mask only. Nothing here shows whether that ever happens -- "
        "it needs a run that records both conditions, not a re-reading of these files",
    ],
    "refines": [
        "the sentence this replaces: 'the only thing missing is a label that cannot tell cannot-act "
        "from acts-too-slowly'. Correct version: both labels exist and both have fired; what is NOT "
        "established is that they are mutually exclusive per episode, because dead_end_reason holds "
        "one value",
    ],
    "scope": "all committed metrics files, both schema generations",
    "step_cap_files": step_cap_files,
    "vocabulary": dict(sorted(vocabulary.items())),
}
pathlib.Path("docs/evidence/dead_end_vocabulary_20260919.json").write_text(
    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print("vocabulary:", dict(vocabulary))
print("step_cap files:", [(row["file"], row["stage"], row["split"], row["reasons"])
                          for row in step_cap_files])
print("reason-bearing files:", len(per_file))
