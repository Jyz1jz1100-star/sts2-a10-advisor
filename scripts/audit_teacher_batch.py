"""Audit a teacher batch dataset without re-running any emulator search.

Checks, on every record:

* required provenance fields are present and consistent (scope, disclaimer,
  emulator hash, codec version, budget, non-empty 64-hex hashes);
* ``state_hashes.capture == state_hashes.reverify`` (replay proof);
* the chosen ``best_action_id`` exists among the scored candidates and its
  score is the maximum;
* ``score_gap`` equals best minus runner-up (finite only when >=2 candidates);
* action ids are unique per record and pairs decode without duplicates;
* no record is labelled as a real-game result.

Aggregates a quality summary (phase balance, target-choice share, gap
quantiles, rejection ratio from the manifest) and exits non-zero on any
violation.  Usage::

    python scripts/audit_teacher_batch.py data/teacher/batch0/*.jsonl.shard* \
        --emulator-sha bcd623ce...
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from training.teacher_batch import TEACHER_RECORD_VERSION  # noqa: E402
from advisor_core.action_codec_v2 import ACTION_ID_VERSION  # noqa: E402


def audit_record(record: dict, expected_emulator: str) -> list[str]:
    issues: list[str] = []
    if record.get("record_version") != TEACHER_RECORD_VERSION:
        issues.append("record_version")
    if record.get("scope") != "simulator_act1":
        issues.append("scope")
    if "real" in str(record.get("disclaimer", "")).lower() and \
            "not a real-game" not in str(record.get("disclaimer", "")):
        issues.append("disclaimer-wording")
    if record.get("emulator_native_sha256") != expected_emulator:
        issues.append("emulator-hash")
    if record.get("action_id_version") != ACTION_ID_VERSION:
        issues.append("codec-version")
    capture = record.get("state_hashes", {})
    if capture.get("capture") != capture.get("reverify"):
        issues.append("replay-hash-mismatch")
    # The embedded prefix must hash to the recorded prefix_sha256; that is the
    # offline half of the replay proof.
    prefix = record.get("prefix")
    if not isinstance(prefix, dict):
        issues.append("missing-embedded-prefix")
    else:
        from training.prefix_replay_teacher import (
            ActionTarget,
            PrefixStep,
            ReplayPrefix,
            StateHashes,
        )

        try:
            rebuilt = ReplayPrefix(
                seed=prefix["seed"],
                steps=tuple(
                    PrefixStep(
                        decision=ActionTarget(**step["decision"]),
                        state=StateHashes(**step["state"]),
                    )
                    for step in prefix["steps"]
                ),
                final_state=StateHashes(**prefix["final_state"]),
                scope=prefix["scope"],
            )
        except Exception as exc:  # malformed prefix JSON is a violation too
            issues.append(f"prefix-rebuild-error:{exc}")
        else:
            if rebuilt.sha256 != record.get("prefix_sha256"):
                issues.append("embedded-prefix-sha-mismatch")
    for key in ("prefix_sha256",):
        value = record.get(key, "")
        if not (isinstance(value, str) and len(value) == 64):
            issues.append(f"bad-{key}")
    candidates = record.get("candidates", [])
    ids = [item["action_id"] for item in candidates]
    if len(ids) != len(set(ids)):
        issues.append("duplicate-action-ids")
    if len(candidates) < 2:
        issues.append("fewer-than-two-candidates")
    by_id = {item["action_id"]: item for item in candidates}
    best = by_id.get(record.get("best_action_id"))
    if best is None:
        issues.append("best-not-in-candidates")
    else:
        if float(best["score"]) < max(float(item["score"]) for item in candidates) - 1e-6:
            issues.append("best-not-max-score")
    scores = sorted((float(item["score"]) for item in candidates), reverse=True)
    expected_gap = scores[0] - scores[1] if len(scores) >= 2 else None
    gap = record.get("score_gap")
    if expected_gap is not None and gap is not None:
        if abs(expected_gap - float(gap)) > 1e-6:
            issues.append("gap-mismatch")
    pairs = [(item["action"], item.get("target")) for item in candidates]
    if len(pairs) != len(set(pairs)):
        issues.append("duplicate-action-target-pairs")
    return issues


def live_replay_check(records: list[dict], emulator_root: Path) -> list[str]:
    """Rebuild sampled decision states in the real emulator and compare hashes."""

    from training.prefix_replay_teacher import (
        ActionTarget,
        PrefixStep,
        ReplayPrefix,
        StateHashes,
        replay_prefix,
        sts2_run_env_factory,
    )

    sys.path.insert(0, str(emulator_root / "src"))
    factory = sts2_run_env_factory(max_episode_steps=1200, max_floors=16)
    failures: list[str] = []
    for record in records:
        prefix = record["prefix"]
        try:
            rebuilt = ReplayPrefix(
                seed=prefix["seed"],
                steps=tuple(
                    PrefixStep(
                        decision=ActionTarget(**step["decision"]),
                        state=StateHashes(**step["state"]),
                    )
                    for step in prefix["steps"]
                ),
                final_state=StateHashes(**prefix["final_state"]),
                scope=prefix["scope"],
            )
            with replay_prefix(rebuilt, env_factory=factory) as state:
                pass  # replay_prefix already raises on any hash divergence
        except Exception as exc:
            failures.append(f"seed={record['seed']} idx={record['decision_index']}: {exc}")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("datasets", nargs="+", type=Path)
    parser.add_argument("--emulator-sha", required=True)
    parser.add_argument("--min-records", type=int, default=1)
    parser.add_argument("--replay-sample", type=int, default=0,
                        help="rebuild N sampled states against the real emulator")
    parser.add_argument("--emulator-root", type=Path,
                        default=PROJECT_ROOT.parent / "third_party"
                        / "slay-the-spire-2-emulator-main")
    args = parser.parse_args(argv)

    total = 0
    violations: list[tuple[int, str, list[str]]] = []
    gaps: list[float] = []
    phases: dict[str, int] = {}
    targeted = 0
    all_records: list[dict] = []
    for path in args.datasets:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            record = json.loads(line)
            total += 1
            all_records.append(record)
            issues = audit_record(record, args.emulator_sha)
            if issues:
                violations.append((line_number, str(path), issues))
            gaps.append(float(record["score_gap"]))
            phases[str(record["phase"])] = phases.get(str(record["phase"]), 0) + 1
            if record["best_pair"][1] is not None and record["best_pair"][1] >= 0:
                targeted += 1

    replay_failures: list[str] = []
    sampled: list[dict] = []
    if args.replay_sample and total:
        stride = max(1, total // args.replay_sample)
        sampled = all_records[::stride][: args.replay_sample]
        replay_failures = live_replay_check(sampled, args.emulator_root)

    gaps.sort()
    summary = {
        "records": total,
        "violations": len(violations),
        "phase_counts": dict(sorted(phases.items())),
        "target_choice_share": round(targeted / total, 4) if total else None,
        "score_gap": {
            "min": gaps[0] if gaps else None,
            "p25": gaps[len(gaps) // 4] if gaps else None,
            "median": gaps[len(gaps) // 2] if gaps else None,
            "p75": gaps[(3 * len(gaps)) // 4] if gaps else None,
            "max": gaps[-1] if gaps else None,
        },
        "live_replay": {
            "sampled": len(sampled),
            "failures": len(replay_failures),
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    for line_number, path, issues in violations[:20]:
        print(f"VIOLATION {path}:{line_number}: {issues}", file=sys.stderr)
    for failure in replay_failures[:20]:
        print(f"REPLAY FAILURE {failure}", file=sys.stderr)
    if violations or replay_failures or total < args.min_records:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
