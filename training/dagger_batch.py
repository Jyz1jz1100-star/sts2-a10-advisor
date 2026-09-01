"""DAgger batches: label the states the *student* actually visits.

Behaviour cloning on teacher traversals only ever sees the teacher's idea of
plausible states; the plan's step 4 requires the student to re-play the
simulator and have the teacher label the states where the student errs.  This
module does exactly that:

* the student rollout runs on the V2 contract stack (``NativeRunCore`` ->
  ``V2FlatActionEnv`` in ``noop`` rejection mode, the same mode the BC
  materializer proved zero-drift on 200/200 records), so rejected actions are
  engine-observed no-ops: they are excluded from the prefix (the batch-0
  lesson: prefixes may only contain engine-accepted steps) and the *state* is
  still labelled -- mask/native disagreement states are exactly the ones the
  student needs corrected;
* a visited state is labelled whenever it is *interesting* (elite/boss, low
  HP, multi-target, run-level choice) **or** the student's top-1/top-2 score
  margin says it is unsure;
* records reuse the teacher format (replayable prefix, budget, score gap,
  re-verified hashes) plus ``student`` provenance and a
  ``teacher_agreement`` flag.

Teacher labels in DAgger records override the student (that is the point);
the agreement flag lets downstream analysis weight corrections vs
confirmations.  Scope stays ``simulator_act1``.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Sequence

import numpy as np
import torch

from .behavior_clone_v2 import PhaseSplitActionScorer, masked_scores
from .prefix_replay_teacher import ActionTarget
from .teacher_batch import (
    TeacherBatchConfig,
    TraversalDecision,
    codec_for_state,
    is_interesting,
    label_decision,
)
from .v2_flat_env import SENTINEL_FLAT, V2FlatActionEnv, decode_flat, flat_index
from .v2_observation import BLOCK_OFFSETS


@dataclass(frozen=True)
class DaggerConfig(TeacherBatchConfig):
    """Teacher batch config plus student-uncertainty thresholds."""

    uncertainty_margin: float = 0.35
    safe_label_probability: float = 0.2  # thin coverage of confident boring states


def student_choice(
    model: PhaseSplitActionScorer,
    observation: np.ndarray,
    flat_mask: np.ndarray,
    *,
    device: str,
    excluded: Sequence[int] = (),
) -> tuple[int, float]:
    """Greedy legal flat action (minus exclusions) and its score margin."""

    mask = np.asarray(flat_mask, dtype=bool).copy()
    for index in excluded:
        mask[index] = False
    if not mask.any():
        return -1, float("inf")
    with torch.no_grad():
        vector = torch.as_tensor(observation, dtype=torch.int32).view(1, -1).to(device)
        legal = torch.as_tensor(mask, dtype=torch.bool).view(1, -1).to(device)
        combat = torch.tensor(
            [int(observation[BLOCK_OFFSETS["run_native_passthrough"]]) == 0],
            dtype=torch.bool,
            device=device,
        )
        scores = masked_scores(model(vector, combat), legal)[0]
    chosen = int(scores.argmax())
    ranked = scores[mask].sort(descending=True).values
    margin = float(ranked[0] - ranked[1]) if ranked.numel() >= 2 else float("inf")
    return chosen, margin


def traverse_with_student(
    seed: int | str,
    *,
    core_factory: Callable[[], Any],
    model: PhaseSplitActionScorer,
    device: str,
    rng: random.Random,
    config: DaggerConfig,
) -> Iterator[tuple[TraversalDecision, dict[str, Any]]]:
    """Yield (state, student info) along one student-driven contract rollout."""

    env = V2FlatActionEnv(core_factory(), rejection_mode="noop")
    try:
        observation, _info = env.reset(seed=seed)
        accepted: list[ActionTarget] = []
        rejected_flats: set[int] = set()
        for _ in range(config.traversal_steps):
            flat_mask = env.action_masks()
            base_mask = env.base_action_mask()
            raw = env.raw_observation()
            mask_candidates = [
                int(index)
                for index in np.flatnonzero(flat_mask)
                if int(index) not in rejected_flats
            ]
            if not mask_candidates:
                return  # empty mask, or every candidate already rejected
            chosen, margin = student_choice(
                model, observation, flat_mask, device=device,
                excluded=rejected_flats | {SENTINEL_FLAT},
            )
            yield TraversalDecision(
                seed=seed,
                decisions=list(accepted),
                raw_obs=raw,
                base_mask=base_mask.copy(),
                info=env.state_info(),
            ), {"chosen_flat": chosen, "margin": margin}
            action, target = decode_flat(chosen)
            observation, _reward, terminated, truncated, step_info = env.step(chosen)
            if terminated or truncated:
                accepted.append(ActionTarget(action, target))
                return
            if step_info.get("simulator_dead_end") == "native_rejection":
                # Noop mode labels engine-ignored steps.  Exclude this action
                # at this state, keep the episode, and let the student fall
                # back to its next-best legal choice -- the *state* is still
                # labelled above because the disagreement is exactly what
                # DAgger exists to correct.
                rejected_flats.add(chosen)
                continue
            rejected_flats.clear()
            accepted.append(ActionTarget(action, target))
    finally:
        env.close()


def generate_dagger_batch(
    seeds: Sequence[int],
    *,
    core_factory: Callable[[], Any],
    env_factory: Callable[[int | str], Any],
    model: PhaseSplitActionScorer,
    emulator_hash: str,
    device: str = "cpu",
    config: DaggerConfig = DaggerConfig(),
    progress: Callable[[dict[str, int]], None] | None = None,
) -> Iterator[dict[str, Any]]:
    """Label student-visited states; yields teacher-corrected records.

    ``core_factory`` builds the native run core for the student rollout;
    ``env_factory`` is the raw seed-keyed env the prefix teacher replays
    through (same signature as the batch generator).
    """

    stats = {"runs": 0, "visited": 0, "labelled": 0, "rejected": 0,
             "disagreements": 0}
    for seed in seeds:
        stats["runs"] += 1
        digest = hashlib.blake2b(f"dagger-traversal:{seed}".encode(), digest_size=8)
        rng = random.Random(int.from_bytes(digest.digest(), "big"))
        labelled = 0
        for decision, info in traverse_with_student(
            seed, core_factory=core_factory, model=model, device=device,
            rng=rng, config=config,
        ):
            stats["visited"] += 1
            worth = (
                is_interesting(decision)
                or info["margin"] < config.uncertainty_margin
                or rng.random() < config.safe_label_probability
            )
            if not worth or labelled >= config.max_decisions_per_run:
                continue
            try:
                record = label_decision(
                    decision, env_factory=env_factory,
                    emulator_hash=emulator_hash, config=config,
                )
            except Exception:
                stats["rejected"] += 1
                continue
            if record is None:
                stats["rejected"] += 1
                continue
            labelled += 1
            stats["labelled"] += 1
            chosen_action, chosen_target = decode_flat(info["chosen_flat"])
            student_id = _student_action_id(decision, chosen_action, chosen_target)
            record["source"] = "dagger"
            record["student"] = {
                "chosen_flat": info["chosen_flat"],
                "chosen_action_id": student_id,
                "margin": info["margin"],
            }
            record["teacher_agreement"] = bool(
                student_id is not None
                and student_id == record["best_action_id"]
            )
            if not record["teacher_agreement"]:
                stats["disagreements"] += 1
            yield record
        if progress is not None:
            progress(dict(stats))


def _student_action_id(decision: TraversalDecision, action: int, target: int):
    codec = codec_for_state(
        decision.raw_obs, decision.base_mask,
        encounter_id=int(decision.info.get("encounter_id", -1)),
    )
    try:
        index = codec.encode(action, -1 if target == -1 else target)
    except ValueError:
        return None
    return codec.candidates[index].action_id


__all__ = ["DaggerConfig", "generate_dagger_batch", "student_choice",
           "traverse_with_student"]
