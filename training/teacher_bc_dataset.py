"""Materialize behaviour-cloning samples from audited teacher records.

Teacher records store *replayable prefixes*, not raw observations.  This
module rebuilds each labelled decision state by replaying the embedded prefix
through the real V2 contract stack (``NativeRunCore`` -> ``V2FlatActionEnv``)
and then emits exactly the tensors a student consumes at inference:

* the expanded V2 observation vector (hashed for provenance),
* the flat ``(action, target)`` legal-action mask,
* the teacher's chosen flat index (label) and its score gap.

The rebuilt state must hash equal to the teacher-recorded final-state hash
(same ``state_hashes`` function and same native buffers), so a dataset that
converts cleanly carries the same replay proof the batch audit checked -- any
drift raises and the conversion aborts.  Output JSONL is
``simulator_act1``-scoped.

Usage::

    python -m training.teacher_bc_dataset \
        --records data/teacher/batch0/v2_batch0.jsonl.shard00 \
        --out data/teacher/batch0/v2_batch0_bc.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Iterator

from .prefix_replay_teacher import (
    ActionTarget,
    ReplayPrefix,
    state_hashes,
)
from .v2_flat_env import V2FlatActionEnv, flat_index

BC_SAMPLE_VERSION = 1


def materialize_records(
    records: Iterator[dict[str, Any]],
    *,
    core_factory: Callable[[], Any],
) -> Iterator[dict[str, Any]]:
    """Yield one BC sample per teacher record with rebuilt state tensors."""

    for record in records:
        prefix_payload = record["prefix"]
        if prefix_payload.get("seed") != record["seed"]:
            raise ValueError("embedded prefix seed disagrees with record seed")
        prefix = ReplayPrefix.from_json(prefix_payload)
        if prefix.sha256 != record["prefix_sha256"]:
            raise ValueError("embedded prefix hash mismatch; refuse to label")
        if record.get("scope") != prefix.scope:
            raise ValueError("record/prefix scope mismatch")

        # No-op rejection mode: batch-0 prefixes may contain steps the engine
        # silently ignored (mask-legal, native status -1, state frozen).  The
        # engine *was* the semantics there -- replaying history must reproduce
        # the engine's state, not the training wrapper's episode-end choice.
        # The final hash comparison below proves equivalence either way.
        env = V2FlatActionEnv(core_factory(), rejection_mode="noop")
        try:
            observation, _info = env.reset(seed=record["seed"])
            for step in prefix.steps:
                flat = flat_index(step.decision.action, step.decision.target)
                observation, _reward, terminated, truncated, _info = env.step(flat)
                if terminated or truncated:
                    raise ValueError(
                        f"prefix ended early at step {step!r}; state drift"
                    )
            raw = env.raw_observation()
            base_mask = tuple(bool(value) for value in env.base_action_mask())
            rebuilt = state_hashes(raw, base_mask)
            if rebuilt != prefix.final_state:
                raise ValueError(
                    "rebuilt decision state hashes diverge from the teacher: "
                    f"{prefix.final_state} != {rebuilt}"
                )
            mask = env.action_masks()
            label = flat_index(*record["best_pair"])
            if not bool(mask[label]):
                raise ValueError("teacher label is mask-illegal on the rebuilt state")
            yield {
                "sample_version": BC_SAMPLE_VERSION,
                "scope": record["scope"],
                "seed": record["seed"],
                "prefix_sha256": record["prefix_sha256"],
                "decision_index": record["decision_index"],
                "source": record.get("source", "teacher"),
                "phase": record["phase"],
                "floor": record["floor"],
                "score_gap": record["score_gap"],
                "label_flat_action": int(label),
                "label_action_id": record["best_action_id"],
                "legal_flat_actions": [int(i) for i in mask.nonzero()[0]],
                # The vector itself is stored (with its hash) so BC training
                # never has to touch the emulator; the hash lets any loader
                # prove the payload matches the replayed state.
                "observation": [int(value) for value in observation],
                "observation_sha256": hashlib.sha256(
                    observation.astype("int32").tobytes()
                ).hexdigest(),
            }
        finally:
            env.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", nargs="+", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--emulator-root", type=Path,
                        default=Path(__file__).resolve().parents[1].parent
                        / "third_party/slay-the-spire-2-emulator-main")
    args = parser.parse_args(argv)

    import sys

    sys.path.insert(0, str(args.emulator_root / "src"))
    from sts2_gym import native  # noqa: PLC0415

    from .v2_native_env import NativeRunCore  # noqa: PLC0415

    def core_factory():
        return NativeRunCore(native, max_episode_steps=1200)

    def record_stream():
        for path in args.records:
            # utf-8-sig: subset files hand-made with PowerShell carry a BOM.
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                if line.strip():
                    yield json.loads(line)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with args.out.open("w", encoding="utf-8") as handle:
        for sample in materialize_records(record_stream(), core_factory=core_factory):
            handle.write(json.dumps(sample, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            count += 1
    print(json.dumps({"samples": count, "out": str(args.out)}, ensure_ascii=False))
    return 0 if count else 1


__all__ = ["BC_SAMPLE_VERSION", "materialize_records"]

if __name__ == "__main__":
    raise SystemExit(main())
