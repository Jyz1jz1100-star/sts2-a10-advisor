from __future__ import annotations

import math


def wilson_interval(wins: int, games: int, z: float = 1.959963984540054) -> tuple[float, float]:
    if games <= 0 or wins < 0 or wins > games:
        raise ValueError("require 0 <= wins <= games and games > 0")
    p = wins / games
    denom = 1 + z * z / games
    center = (p + z * z / (2 * games)) / denom
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * games)) / games) / denom
    return center - spread, center + spread
