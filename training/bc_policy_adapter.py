"""Adapter: V2 BC checkpoint as a student policy in the training/eval loops.

Gives the phase-split scorer the ``predict(observation, *, action_masks,
deterministic)`` surface ``training.evaluation.evaluate_policy`` and SB3
already speak, so the distilled student is measured by exactly the same
harness (boundary rate, defect truncations, unclassified dead ends, illegal
actions) as the PPO baselines -- an apples-to-apples ``simulator_act1`` number.

Selection is argmax over masked scores; illegal candidates are pinned at
-1e9 by the network itself, so ``deterministic=False`` (Gumbel-noised) is the
only stochastic mode and it still cannot emit an illegal action.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import torch

from .behavior_clone_v2 import PhaseSplitActionScorer, masked_scores
from .v2_constants import PHASE_COMBAT
from .v2_observation import BLOCK_OFFSETS


class BCFlatPolicy:
    """Deterministic/stochastic argmax student over the flat action space."""

    def __init__(self, model: PhaseSplitActionScorer, *, device: str = "cpu") -> None:
        self._model = model.to(device).eval()
        self._device = device

    @torch.no_grad()
    def predict(
        self,
        observation: Any,
        *,
        action_masks: Any,
        deterministic: bool = True,
    ) -> tuple[np.ndarray, None]:
        vector = torch.as_tensor(
            np.asarray(observation, dtype=np.int32), dtype=torch.int32
        ).view(1, -1).to(self._device)
        legal = torch.as_tensor(
            np.asarray(action_masks, dtype=bool), dtype=torch.bool
        ).view(1, -1).to(self._device)
        # Phase flag: combat means the native observation's phase slot.
        combat = torch.tensor(
            [_phase_of(vector) == PHASE_COMBAT], dtype=torch.bool, device=self._device
        )
        logits = masked_scores(self._model(vector, combat), legal)
        if deterministic:
            choice = int(logits.argmax(dim=-1).item())
        else:
            probabilities = torch.softmax(logits, dim=-1)
            choice = int(torch.multinomial(probabilities, 1).item())
        return np.array([choice], dtype=np.int64), None


def _phase_of(observation_row: torch.Tensor) -> int:
    """Native phase slot inside the expanded vector (run passthrough offset 0)."""

    return int(observation_row[0, BLOCK_OFFSETS["run_native_passthrough"]])


__all__ = ["BCFlatPolicy"]
