from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Candidate:
    """One legal, scored decision candidate.

    `score` is always larger-is-better. For a trained policy it is normally an
    estimated run-win value. Baselines may use a documented heuristic score.
    """

    action: dict[str, Any]
    label: str
    score: float
    confidence: float = 0.0
    facts: tuple[str, ...] = ()
    metrics: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Recommendation:
    phase: str
    primary: Candidate
    alternatives: tuple[Candidate, ...] = ()
    model_id: str = "untrained-baseline"
    game_build: str = "unknown"
    search_nodes: int = 0
    warnings: tuple[str, ...] = ()

    def validate(self) -> None:
        if not self.primary.action:
            raise ValueError("primary action must not be empty")
        if not 0.0 <= self.primary.confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")
