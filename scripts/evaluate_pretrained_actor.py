"""Evaluate the BC student and the pretrained actor on the same 100 seeds.

Review item 3 (2026-09-01): the pretrained MaskablePPO actor must be measured
on the 100-seed checkpoint partition BEFORE PPO starts, next to the BC
student it was distilled from, on the identical harness (boundary rate,
defect truncations, unclassified dead ends, illegal actions, mean final HP
fraction, mean steps — scope ``simulator_act1``).

The seeds are each stage's ``checkpoint`` partition limited to 100; they are
disjoint from every training partition, from the teacher-data seeds
(1.4e9+), and from the promotion ranges, so neither policy has ever seen
them.

Usage::

    python scripts/evaluate_pretrained_actor.py \
        --bc-checkpoint models/bc_v2_r3_leakfree.pt \
        --pretrained models/bc_pretrain_r3/pretrained_actor.zip \
        --stages floor3 floor6 act1
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def _evaluate(
    policy,
    *,
    label: str,
    config,
    sts2_gym,
    stages: list[str],
    episodes: int,
) -> dict[str, object]:
    from training.evaluation import evaluate_policy
    from training.v2_curriculum import _environment_factory

    report: dict[str, object] = {}
    for stage_name in stages:
        stage = config.stage(stage_name)
        seeds = config.partition(stage_name, "checkpoint").seeds(episodes)
        factory = _environment_factory(config, stage, sts2_gym)
        metrics = evaluate_policy(
            policy,
            env_factory=factory,
            seeds=seeds,
            stage=stage_name,
            split=f"pretrain-eval:{label}",
            scope=stage.scope,
            checkpoint=label,
            max_steps_per_episode=stage.max_episode_steps,
        )
        payload = metrics.to_dict()
        report[stage_name] = {
            "episodes": payload["episodes"],
            "boundary_rate": payload["boundary_rate"],
            "win_rate": payload["win_rate"],
            "mean_final_floor": payload["mean_final_floor"],
            "mean_final_hp_fraction": payload.get("mean_final_hp_fraction"),
            "mean_steps": payload["mean_steps"],
            "defect_truncation_rate": payload["defect_truncation_rate"],
            "unclassified_dead_ends": payload["unclassified_dead_ends"],
            "illegal_actions": payload["illegal_actions"],
            "seed_sha256": payload["seed_sha256"],
        }
        print(json.dumps({f"{label}:{stage_name}": report[stage_name]},
                         ensure_ascii=False))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bc-checkpoint", type=Path, required=True)
    parser.add_argument("--pretrained", type=Path, required=True)
    parser.add_argument("--config", type=Path,
                        default=PROJECT_ROOT / "config" / "training_v2.toml")
    parser.add_argument("--emulator-root", type=Path,
                        default=PROJECT_ROOT.parent
                        / "third_party/slay-the-spire-2-emulator-main")
    parser.add_argument("--stages", nargs="+",
                        default=["floor3", "floor6", "act1"])
    parser.add_argument("--episodes", type=int, default=100)
    parser.add_argument("--device", default="auto")
    parser.add_argument("--out", type=Path, default=None,
                        help="where to write the comparison JSON")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(args.emulator_root / "src"))
    import sts2_gym  # noqa: PLC0415,E402

    from sb3_contrib import MaskablePPO  # noqa: PLC0415,E402

    from training.bc_policy_adapter import BCFlatPolicy  # noqa: PLC0415,E402
    from training.behavior_clone_v2 import load_model  # noqa: PLC0415,E402
    from training.metrics import atomic_write_json  # noqa: PLC0415,E402
    from training.v2_config import load_v2_training_config  # noqa: PLC0415,E402

    config = load_v2_training_config(args.config)
    device = args.device
    if device == "auto":
        import torch

        device = "cuda" if torch.cuda.is_available() else "cpu"

    bc_model = load_model(args.bc_checkpoint)
    bc_policy = BCFlatPolicy(bc_model, device=device)

    pretrained_model = MaskablePPO.load(args.pretrained, env=None, device=device)
    actor_policy = pretrained_model.policy

    report: dict[str, object] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": "simulator_act1",
        "episodes_per_stage": args.episodes,
        "seed_note": "stage checkpoint partitions, 100 seeds, never trained on",
        "bc_checkpoint": str(args.bc_checkpoint),
        "pretrained_actor": str(args.pretrained),
        "policies": {},
    }
    for label, policy in (("bc_student", bc_policy),
                          ("pretrained_actor", actor_policy)):
        report["policies"][label] = _evaluate(
            policy,
            label=label,
            config=config,
            sts2_gym=sts2_gym,
            stages=args.stages,
            episodes=args.episodes,
        )

    out_path = args.out or (
        PROJECT_ROOT / "runs" / "pretrain_eval"
        / f"comparison-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    atomic_write_json(out_path, report)
    print(json.dumps({"out": str(out_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
