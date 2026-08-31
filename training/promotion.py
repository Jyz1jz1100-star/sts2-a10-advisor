from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import PromotionConfig, load_training_config
from .metrics import EvaluationMetrics, atomic_write_json


@dataclass(frozen=True)
class PromotionDecision:
    promoted: bool
    reasons: tuple[str, ...]
    observed: dict
    required: dict

    def to_dict(self) -> dict:
        return asdict(self)


def decide_promotion(
    metrics: EvaluationMetrics, requirements: PromotionConfig
) -> PromotionDecision:
    reasons: list[str] = []
    if metrics.episodes < requirements.min_episodes:
        reasons.append(
            f"episodes {metrics.episodes} < required {requirements.min_episodes}"
        )
    if metrics.win_rate < requirements.min_win_rate:
        reasons.append(
            f"win_rate {metrics.win_rate:.4f} < required {requirements.min_win_rate:.4f}"
        )
    if metrics.wilson_95_low < requirements.min_wilson_lower:
        reasons.append(
            "wilson_95_low "
            f"{metrics.wilson_95_low:.4f} < required "
            f"{requirements.min_wilson_lower:.4f}"
        )
    if metrics.truncation_rate > requirements.max_truncation_rate:
        reasons.append(
            "truncation_rate "
            f"{metrics.truncation_rate:.4f} > allowed "
            f"{requirements.max_truncation_rate:.4f}"
        )
    if metrics.illegal_actions > requirements.max_illegal_actions:
        reasons.append(
            f"illegal_actions {metrics.illegal_actions} > allowed "
            f"{requirements.max_illegal_actions}"
        )
    if requirements.min_mean_floor is not None:
        if metrics.mean_final_floor is None:
            reasons.append("mean_final_floor is unavailable")
        elif metrics.mean_final_floor < requirements.min_mean_floor:
            reasons.append(
                f"mean_final_floor {metrics.mean_final_floor:.2f} < required "
                f"{requirements.min_mean_floor:.2f}"
            )
    return PromotionDecision(
        promoted=not reasons,
        reasons=tuple(reasons),
        observed={
            "episodes": metrics.episodes,
            "win_rate": metrics.win_rate,
            "wilson_95_low": metrics.wilson_95_low,
            "truncation_rate": metrics.truncation_rate,
            "illegal_actions": metrics.illegal_actions,
            "mean_final_floor": metrics.mean_final_floor,
        },
        required=asdict(requirements),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply a configured promotion gate")
    parser.add_argument("--config", type=Path, default=Path("config/training.toml"))
    parser.add_argument("--stage", required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = load_training_config(args.config)
    raw = json.loads(args.metrics.read_text(encoding="utf-8"))
    metrics = EvaluationMetrics(**raw)
    if metrics.stage != args.stage:
        raise ValueError(
            f"metrics stage {metrics.stage!r} does not match {args.stage!r}"
        )
    decision = decide_promotion(metrics, config.stage(args.stage).promotion)
    payload = decision.to_dict()
    payload["metrics_path"] = str(args.metrics)
    payload["checkpoint"] = metrics.checkpoint
    payload["checkpoint_sha256"] = metrics.checkpoint_sha256
    atomic_write_json(args.output, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(0 if decision.promoted else 1)


if __name__ == "__main__":
    main()
