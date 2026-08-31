from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from .contracts import Candidate, Recommendation
from .legal_actions import decision_actions


class Policy(ABC):
    @abstractmethod
    def recommend(self, state: dict[str, Any]) -> Recommendation:
        raise NotImplementedError


class SmokeBaselinePolicy(Policy):
    """A deterministic integration baseline, explicitly not a trained model."""

    model_id = "smoke-baseline-v1"

    def recommend(self, state: dict[str, Any]) -> Recommendation:
        legal = decision_actions(state)
        if not legal:
            raise ValueError(f"no supported decision for {state.get('state_type')!r}")

        # Stable ordering makes bridge, overlay and evaluator tests reproducible.
        candidates = [
            Candidate(action=action, label=label, score=-float(i), confidence=0.0,
                      facts=("这是接口联调用基线，尚未经过策略训练。",))
            for i, (action, label) in enumerate(legal)
        ]
        return Recommendation(
            phase=str(state.get("state_type") or "unknown"),
            primary=candidates[0],
            alternatives=tuple(candidates[1:3]),
            model_id=self.model_id,
            game_build=str((state.get("game") or {}).get("build") or "unknown"),
            warnings=("当前输出仅用于验证数据链路，不得计入 A10 胜率。",),
        )
