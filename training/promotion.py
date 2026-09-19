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


#: Gate clauses whose input is a metric with a dataclass default.  Reading such a
#: field straight off a rehydrated payload cannot distinguish "measured as zero"
#: from "this file predates the field", and the confusion runs both ways: a missing
#: ``min`` input invents a rejection, a missing ``max`` input invents a clean bill.
#: ``requirement`` is None when the clause is always active.
DEFAULTED_GATE_INPUTS: tuple[tuple[str, str | None], ...] = (
    ("defect_truncation_rate", None),
    ("unclassified_dead_ends", None),
    ("boundary_rate", "min_boundary_rate"),
    ("boundary_wilson_95_low", "min_boundary_wilson_lower"),
)


def decide_promotion(
    metrics: EvaluationMetrics, requirements: PromotionConfig
) -> PromotionDecision:
    reasons: list[str] = []
    for metric_name, requirement_name in DEFAULTED_GATE_INPUTS:
        if requirement_name is not None and getattr(requirements, requirement_name) is None:
            continue
        if metric_name in metrics.absent_metrics:
            reasons.append(
                f"{metric_name} was not recorded in these metrics, so its clause "
                "cannot be scored"
            )
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
    # Curriculum boundary truncations are stage completions, not defects: the
    # cap applies to truncations excluding successful boundary hits.  For
    # stages without a floor boundary this equals the raw truncation rate.
    if "defect_truncation_rate" not in metrics.absent_metrics:
        if metrics.defect_truncation_rate > requirements.max_truncation_rate:
            reasons.append(
                "defect_truncation_rate "
                f"{metrics.defect_truncation_rate:.4f} > allowed "
                f"{requirements.max_truncation_rate:.4f}"
            )
    if metrics.illegal_actions > requirements.max_illegal_actions:
        reasons.append(
            f"illegal_actions {metrics.illegal_actions} > allowed "
            f"{requirements.max_illegal_actions}"
        )
    if requirements.min_boundary_rate is not None:
        if "boundary_rate" not in metrics.absent_metrics:
            if metrics.boundary_rate < requirements.min_boundary_rate:
                reasons.append(
                    "boundary_rate "
                    f"{metrics.boundary_rate:.4f} < required "
                    f"{requirements.min_boundary_rate:.4f}"
                )
    if requirements.min_boundary_wilson_lower is not None:
        if "boundary_wilson_95_low" not in metrics.absent_metrics:
            if metrics.boundary_wilson_95_low < requirements.min_boundary_wilson_lower:
                reasons.append(
                    "boundary_wilson_95_low "
                    f"{metrics.boundary_wilson_95_low:.4f} < required "
                    f"{requirements.min_boundary_wilson_lower:.4f}"
                )
    if "unclassified_dead_ends" not in metrics.absent_metrics:
        if metrics.unclassified_dead_ends > 0:
            reasons.append(
                "unclassified_dead_ends "
                f"{metrics.unclassified_dead_ends} > allowed 0"
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
            "boundary_rate": metrics.boundary_rate,
            "boundary_wilson_95_low": metrics.boundary_wilson_95_low,
            "truncation_rate": metrics.truncation_rate,
            "defect_truncation_rate": metrics.defect_truncation_rate,
            "illegal_actions": metrics.illegal_actions,
            "mean_final_floor": metrics.mean_final_floor,
            "mean_final_hp_fraction": metrics.mean_final_hp_fraction,
            "unclassified_dead_ends": metrics.unclassified_dead_ends,
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
    metrics = EvaluationMetrics.from_payload(raw)
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
