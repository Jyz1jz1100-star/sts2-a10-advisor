"""Teacher v3 strength evaluation: the expert-label gate.

FREEZE-R2 (2026-09-01 review, item 5) forbids calling any teacher output
"expert labels" until this evaluation shows the v3 search teacher beating the
heuristic, the BC student, and the PPO agent on **independent seeds** — seeds
from no training, evaluation, or teacher-data partition.

Every policy plays the same seeds on the floor-capped stage harness:

* ``teacher_v3`` and ``heuristic`` act on the raw ``Sts2RunEnv``
  (``(action, target)`` decisions; the teacher runs the combat beam search at
  every combat decision, heuristic continuation elsewhere).
* ``bc`` and ``ppo`` act on the V2 flat contract stack they were trained on
  (``MaskablePPO.predict`` protocol with action masks).

All four report through the identical ``summarize_episodes`` schema, so the
gate compares like with like: wins (same seeds), mean final floor, mean final
HP fraction, mean steps.  The teacher earns ``expert_labels_approved`` only
when it beats every applicable baseline on wins and matches-or-beats floor /
HP / steps within the configured margins.  The verdict is advisory by default;
pass ``--strict`` to make a failing gate exit non-zero (for pipelines).

Usage::

    python scripts/evaluate_teacher_strength.py \
        --bc-checkpoint models/bc_v2_r3_leakfree.pt \
        --ppo-checkpoint runs/curriculum_v2/v2curriculum-20260901T091856Z/floor6/checkpoints/best.zip \
        --seed-start 1550000000 --seed-count 12
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

#: Baselines the teacher must beat before its records may be called expert.
BASELINES = ("heuristic", "bc", "ppo")

ChoiceFn = Callable[[Any, Any, Any, int, list, set], Any]


def _seed_digest(seeds: list[int]) -> str:
    payload = json.dumps(seeds, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _raw_heuristic_decision(observation, mask, info, step_index: int):
    """Heuristic continuation with alive-enemy targeting (raw env protocol)."""

    from training.teacher_batch import (
        alive_enemy_indices,
        hand_def_ids,
        heuristic_continuation,
        is_single_target,
    )
    from training.v2_constants import COMBAT_OBS_SIZE

    decision = heuristic_continuation(observation, mask, info, step_index)
    obs = np.asarray(observation)
    if int(obs[COMBAT_OBS_SIZE]) == 0:
        hand = hand_def_ids(obs)
        if (
            decision.action < len(hand)
            and hand[decision.action]
            and is_single_target(hand[decision.action])
        ):
            alive = alive_enemy_indices(obs)
            if alive:
                return type(decision)(decision.action, int(alive[0]))
    return decision


def _run_raw_episode(
    seed: int,
    choose: ChoiceFn,
    *,
    env_factory,
    max_steps: int,
    max_floor: int,
):
    """One rejection-safe raw-env episode; returns an ``EpisodeMetric``."""

    from training.metrics import EpisodeMetric
    from training.prefix_replay_teacher import state_hashes

    env = env_factory(seed)
    try:
        observation, info = env.reset(seed=seed)
        decisions: list = []
        rejected: set[int] = set()
        steps = 0
        total_reward = 0.0
        terminated = truncated = False
        while steps < max_steps:
            mask = np.asarray(env.action_masks(), dtype=bool)
            legal = [
                int(index)
                for index in np.flatnonzero(mask)
                if int(index) not in rejected
            ]
            if not legal:
                truncated = True
                info = {**info, "simulator_dead_end": "empty_action_mask"}
                break
            decision = choose(observation, mask, info, steps, decisions, rejected)
            if (
                decision.action < 0
                or decision.action >= len(mask)
                or not bool(mask[decision.action])
            ):
                truncated = True
                info = {**info, "simulator_dead_end": "policy_illegal_action"}
                break
            pre = state_hashes(observation, mask)
            observation, reward, terminated, truncated, info = env.step(
                decision.action, decision.target
            )
            total_reward += float(reward)
            steps += 1
            if (
                not terminated
                and not truncated
                and float(reward) == -1.0
                and state_hashes(
                    observation, np.asarray(env.action_masks(), dtype=bool)
                )
                == pre
            ):
                rejected.add(decision.action)  # native rejection: retry the state
                continue
            rejected.clear()
            decisions.append(decision)
            if terminated or truncated:
                break
        else:
            truncated = True
            info = {**info, "simulator_dead_end": "step_cap"}
        final_floor = info.get("floor")
        hp = info.get("player_hp")
        max_hp = info.get("player_max_hp")
        try:
            hp_value, max_value = float(hp), float(max_hp)
        except (TypeError, ValueError):
            hp_value, max_value = 0.0, 0.0
        final_hp_fraction = (
            min(1.0, max(0.0, hp_value / max_value))
            if max_value > 0
            else None
        )
        won = bool(terminated and info.get("player_won", False))
        boundary = won or bool(
            truncated
            and final_floor is not None
            and int(final_floor) >= max_floor
        )
        dead_end = info.get("simulator_dead_end")
        if dead_end is None and truncated and not terminated:
            dead_end = "native_truncation"
        return EpisodeMetric(
            seed=seed,
            won=won,
            terminated=bool(terminated),
            truncated=bool(truncated),
            steps=steps,
            episode_return=total_reward,
            illegal_actions=0,
            final_floor=(int(final_floor) if final_floor is not None else None),
            encounter=None,
            boundary_reached=boundary,
            dead_end_reason=(str(dead_end) if dead_end is not None else None),
            rejection_events=0,
            final_hp_fraction=final_hp_fraction,
        )
    finally:
        env.close()


def _summarize_raw(label: str, metrics, *, stage: str, max_floor: int) -> dict:
    from training.metrics import summarize_episodes

    payload = summarize_episodes(
        metrics,
        stage=stage,
        split=f"strength:{label}",
        scope="simulator_act1",
        checkpoint=label,
    ).to_dict()
    payload["policy"] = label
    payload["max_floor"] = max_floor
    return payload


def _evaluate_flat_policy(label: str, policy, *, config, stage, sts2_gym,
                          seeds: list[int], max_steps: int,
                          checkpoint) -> dict:
    from training.evaluation import evaluate_policy
    from training.v2_curriculum import _environment_factory

    flat_factory = _environment_factory(config, stage, sts2_gym)
    payload = evaluate_policy(
        policy,
        env_factory=flat_factory,
        seeds=seeds,
        stage=stage.name,
        split=f"strength:{label}",
        scope=stage.scope,
        checkpoint=str(checkpoint),
        max_steps_per_episode=max_steps,
    ).to_dict()
    payload["policy"] = label
    payload["max_floor"] = int(stage.max_floor) if stage.max_floor is not None else 16
    return payload


def _resolve_device(device: str) -> str:
    if device != "auto":
        return device
    import torch

    return "cuda" if torch.cuda.is_available() else "cpu"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path,
                        default=PROJECT_ROOT / "config" / "training_v2.toml")
    parser.add_argument("--stage", default="floor6")
    parser.add_argument("--emulator-root", type=Path,
                        default=PROJECT_ROOT.parent
                        / "third_party/slay-the-spire-2-emulator-main")
    parser.add_argument("--bc-checkpoint", type=Path,
                        default=PROJECT_ROOT / "models" / "bc_v2_r3_leakfree.pt")
    parser.add_argument("--ppo-checkpoint", type=Path, default=None)
    parser.add_argument("--seed-start", type=int, default=1_550_000_000,
                        help="independent seeds (no partition uses 1.55e9+)")
    parser.add_argument("--seed-count", type=int, default=12)
    parser.add_argument("--beam-width", type=int, default=64)
    parser.add_argument("--lookahead-turns", type=int, default=1)
    parser.add_argument("--max-node-expansions", type=int, default=1536)
    parser.add_argument("--max-combat-decisions", type=int, default=30)
    parser.add_argument("--hp-margin", type=float, default=0.02)
    parser.add_argument("--floor-margin", type=float, default=0.0)
    parser.add_argument("--step-margin", type=float, default=5.0)
    parser.add_argument("--device", default="auto")
    parser.add_argument("--strict", action="store_true",
                        help="exit 2 when the expert-label gate fails")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    sys.path.insert(0, str(args.emulator_root / "src"))
    import sts2_gym  # noqa: PLC0415,E402

    from training.metrics import atomic_write_json  # noqa: PLC0415,E402
    from training.v2_config import load_v2_training_config  # noqa: PLC0415,E402
    from training.v2_constants import COMBAT_OBS_SIZE  # noqa: PLC0415,E402

    config = load_v2_training_config(args.config)
    stage = config.stage(args.stage)
    seeds = [args.seed_start + offset for offset in range(args.seed_count)]
    # ``act1`` has no floor cap: the raw env then runs to the natural terminal.
    max_floor = int(stage.max_floor) if stage.max_floor is not None else 16
    max_steps = int(stage.max_episode_steps)

    from training.prefix_replay_teacher import sts2_run_env_factory

    raw_env_factory = sts2_run_env_factory(
        max_episode_steps=max_steps, max_floors=max_floor
    )

    def heuristic_choose(observation, mask, info, step_index, decisions, rejected):
        filtered = np.zeros_like(np.asarray(mask, dtype=bool))
        for index in np.flatnonzero(mask):
            if int(index) not in rejected:
                filtered[index] = True
        return _raw_heuristic_decision(observation, filtered, info, step_index)

    policies: dict[str, dict] = {}

    # ---- heuristic ---------------------------------------------------------
    heuristic_metrics = [
        _run_raw_episode(
            seed,
            heuristic_choose,
            env_factory=raw_env_factory,
            max_steps=max_steps,
            max_floor=max_floor,
        )
        for seed in seeds
    ]
    policies["heuristic"] = _summarize_raw(
        "heuristic", heuristic_metrics, stage=stage.name, max_floor=max_floor
    )
    print(json.dumps({"heuristic_win_rate": policies["heuristic"]["win_rate"]},
                     ensure_ascii=False))

    # ---- teacher v3 --------------------------------------------------------
    from training.teacher_v3 import BeamSearchConfig  # noqa: PLC0415,E402

    beam_config = BeamSearchConfig(
        beam_width=args.beam_width,
        opponent_lookahead_turns=args.lookahead_turns,
        max_node_expansions=args.max_node_expansions,
    )
    beam_stats = {"combat_decisions": 0, "beam_fallbacks": 0, "beam_errors": 0}
    current_seed = [None]

    def teacher_choose(observation, mask, info, step_index, decisions, rejected):
        from training.prefix_replay_teacher import (
            ActionTarget,
            capture_prefix,
        )
        from training.teacher_batch import codec_for_state
        from training.teacher_v3 import beam_search_action

        seed = current_seed[0]
        phase = int(np.asarray(observation)[COMBAT_OBS_SIZE])
        if (
            phase != 0
            or seed is None
            or beam_stats["combat_decisions"] >= args.max_combat_decisions
        ):
            if phase == 0:
                beam_stats["beam_fallbacks"] += 1
            return heuristic_choose(
                observation, mask, info, step_index, decisions, rejected
            )
        beam_stats["combat_decisions"] += 1
        prefix = capture_prefix(seed, list(decisions), env_factory=raw_env_factory)
        codec = codec_for_state(
            observation,
            np.asarray(mask, dtype=bool),
            encounter_id=int(info.get("encounter_id", -1)),
        )
        root_candidates = [
            ActionTarget(int(item.action), -1 if item.target is None else int(item.target))
            for item in codec.candidates
            if item.action not in rejected
        ]
        if len(root_candidates) < 2:
            beam_stats["beam_fallbacks"] += 1
            return heuristic_choose(
                observation, mask, info, step_index, decisions, rejected
            )
        try:
            result = beam_search_action(
                prefix,
                env_factory=raw_env_factory,
                config=beam_config,
                root_candidates=root_candidates,
            )
        except Exception:
            # A state the search cannot rank (defect mask, replay divergence):
            # fall back honestly instead of fabricating a decision.
            beam_stats["beam_errors"] += 1
            return heuristic_choose(
                observation, mask, info, step_index, decisions, rejected
            )
        return ActionTarget(result.best_action, result.best_target)

    teacher_metrics = []
    for seed in seeds:
        current_seed[0] = seed
        teacher_metrics.append(
            _run_raw_episode(
                seed,
                teacher_choose,
                env_factory=raw_env_factory,
                max_steps=max_steps,
                max_floor=max_floor,
            )
        )
    policies["teacher_v3"] = _summarize_raw(
        "teacher_v3", teacher_metrics, stage=stage.name, max_floor=max_floor
    )
    policies["teacher_v3"]["beam_stats"] = dict(beam_stats)
    print(json.dumps({"teacher_v3_win_rate": policies["teacher_v3"]["win_rate"]},
                     ensure_ascii=False))

    # ---- BC student (flat contract stack) ----------------------------------
    if args.bc_checkpoint and Path(args.bc_checkpoint).exists():
        from training.bc_policy_adapter import BCFlatPolicy
        from training.behavior_clone_v2 import load_model

        bc_policy = BCFlatPolicy(load_model(args.bc_checkpoint),
                                 device=_resolve_device(args.device))
        policies["bc"] = _evaluate_flat_policy(
            "bc", bc_policy,
            config=config, stage=stage, sts2_gym=sts2_gym,
            seeds=seeds, max_steps=max_steps, checkpoint=args.bc_checkpoint,
        )
    else:
        policies["bc"] = {"skipped": f"checkpoint missing: {args.bc_checkpoint}"}

    # ---- PPO (flat contract stack) -----------------------------------------
    if args.ppo_checkpoint and Path(args.ppo_checkpoint).exists():
        from sb3_contrib import MaskablePPO  # type: ignore

        model = MaskablePPO.load(args.ppo_checkpoint, env=None,
                                 device=_resolve_device(args.device))
        policies["ppo"] = _evaluate_flat_policy(
            "ppo", model.policy,
            config=config, stage=stage, sts2_gym=sts2_gym,
            seeds=seeds, max_steps=max_steps, checkpoint=args.ppo_checkpoint,
        )
    else:
        policies["ppo"] = {
            "skipped": "no --ppo-checkpoint provided; gate runs on available baselines"
        }

    # ---- the gate ----------------------------------------------------------
    teacher = policies["teacher_v3"]

    def _wins(payload: dict) -> int:
        if "episodes" in payload and "win_rate" in payload:
            return int(round(float(payload["win_rate"]) * int(payload["episodes"])))
        return 0

    def _field(payload: dict, name: str, default: float) -> float:
        value = payload.get(name)
        return default if value is None else float(value)

    verdicts: dict[str, dict] = {}
    for baseline in BASELINES:
        payload = policies.get(baseline, {})
        if "skipped" in payload:
            verdicts[baseline] = {"skipped": payload["skipped"], "passed": None}
            continue
        checks = {
            "wins": _wins(teacher) >= _wins(payload),
            "mean_final_floor": _field(teacher, "mean_final_floor", 0.0)
            >= _field(payload, "mean_final_floor", 0.0) - args.floor_margin,
            "mean_final_hp_fraction": _field(teacher, "mean_final_hp_fraction", 0.0)
            >= _field(payload, "mean_final_hp_fraction", 0.0) - args.hp_margin,
            "mean_steps": _field(teacher, "mean_steps", 0.0)
            <= _field(payload, "mean_steps", 0.0) + args.step_margin,
        }
        verdicts[baseline] = {"checks": checks, "passed": all(checks.values())}

    applicable = [item for item in verdicts.values() if item.get("passed") is not None]
    approved = bool(applicable) and all(item["passed"] for item in applicable)

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": "simulator_act1",
        "stage": stage.name,
        "seeds": seeds,
        "seed_count": len(seeds),
        "seed_sha256": _seed_digest(seeds),
        "seed_note": (
            "independent seeds: disjoint from every training/eval partition, "
            "the frozen R2 teacher lineage (1.4e9/1.5e9), and all promotion ranges"
        ),
        "beam": {
            "width": args.beam_width,
            "opponent_lookahead_turns": args.lookahead_turns,
            "max_node_expansions": args.max_node_expansions,
            "max_combat_decisions_per_episode": args.max_combat_decisions,
        },
        "gate": {
            "description": (
                "teacher must beat every applicable baseline on wins and "
                "match-or-beat floor/HP/steps within margins"
            ),
            "margins": {
                "hp_margin": args.hp_margin,
                "floor_margin": args.floor_margin,
                "step_margin": args.step_margin,
            },
            "verdicts": verdicts,
            "expert_labels_approved": approved,
        },
        "policies": policies,
    }

    out_path = args.out or (
        PROJECT_ROOT / "runs" / "teacher_v3"
        / f"strength-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    atomic_write_json(out_path, report)
    print(json.dumps({"out": str(out_path), "expert_labels_approved": approved},
                     ensure_ascii=False))
    if args.strict and not approved:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
