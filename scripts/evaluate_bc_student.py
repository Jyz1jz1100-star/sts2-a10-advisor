"""Evaluate a distilled V2 BC student on the real simulator contract stack.

Runs the same ``training.evaluation.evaluate_policy`` harness the PPO
baselines use (boundary rate, defect truncations, unclassified dead ends,
illegal actions, scope simulator_act1) on the V2 stage stacks, using seeds
from the evaluated stage's checkpoint partition — untouched by the teacher
data and by any training.  This is the honest first number for whether
teacher distillation beats the V1 0–1% true-win plateau.

Usage::

    python scripts/evaluate_bc_student.py --checkpoint models/bc_v2_batch0.pt \
        --stages floor3 floor6 act1 --episodes 50
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--config", type=Path,
                        default=PROJECT_ROOT / "config" / "training_v2.toml")
    parser.add_argument("--emulator-root", type=Path,
                        default=PROJECT_ROOT.parent
                        / "third_party/slay-the-spire-2-emulator-main")
    parser.add_argument("--stages", nargs="+", default=["floor3", "floor6", "act1"])
    parser.add_argument("--episodes", type=int, default=50)
    parser.add_argument("--device", default="auto")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(args.emulator_root / "src"))
    import sts2_gym  # noqa: PLC0415,E402

    from training.behavior_clone_v2 import load_model  # noqa: E402
    from training.bc_policy_adapter import BCFlatPolicy  # noqa: E402
    from training.evaluation import evaluate_policy  # noqa: E402
    from training.v2_config import load_v2_training_config  # noqa: E402
    from training.v2_curriculum import _environment_factory  # noqa: E402

    config = load_v2_training_config(args.config)
    device = args.device
    if device == "auto":
        import torch

        device = "cuda" if torch.cuda.is_available() else "cpu"
    model = load_model(args.checkpoint)
    policy = BCFlatPolicy(model, device=device)

    report: dict[str, object] = {"checkpoint": str(args.checkpoint), "stages": {}}
    failures = 0
    for stage_name in args.stages:
        stage = config.stage(stage_name)
        seeds = config.partition(stage_name, "checkpoint").seeds(args.episodes)
        factory = _environment_factory(config, stage, sts2_gym)
        metrics = evaluate_policy(
            policy,
            env_factory=factory,
            seeds=seeds,
            stage=stage_name,
            split="bc-student-eval",
            scope=stage.scope,
            checkpoint=str(args.checkpoint),
            max_steps_per_episode=stage.max_episode_steps,
        )
        payload = metrics.to_dict()
        report["stages"][stage_name] = {
            "boundary_rate": payload["boundary_rate"],
            "win_rate": payload["win_rate"],
            "mean_final_floor": payload["mean_final_floor"],
            "mean_final_hp_fraction": payload.get("mean_final_hp_fraction"),
            "defect_truncation_rate": payload["defect_truncation_rate"],
            "unclassified_dead_ends": payload["unclassified_dead_ends"],
            "illegal_actions": payload["illegal_actions"],
            "mean_steps": payload["mean_steps"],
            "seed_sha256": payload["seed_sha256"],
        }
        failures += payload["illegal_actions"]
        failures += payload["unclassified_dead_ends"]
        print(json.dumps({stage_name: report["stages"][stage_name]},
                         ensure_ascii=False))
    print(json.dumps({"contract_violations": failures}, ensure_ascii=False))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
