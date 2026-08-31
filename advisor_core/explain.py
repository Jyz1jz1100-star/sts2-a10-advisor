from __future__ import annotations

from .contracts import Candidate, Recommendation


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_zh(rec: Recommendation) -> str:
    """Render only computed facts; never invent strategic causality."""

    rec.validate()
    primary = rec.primary
    lines = [f"【建议】{primary.label}"]
    if "win_probability" in primary.metrics:
        lines.append(f"估计通关率：{_pct(primary.metrics['win_probability'])}")
    if "expected_hp_loss" in primary.metrics:
        lines.append(f"预计掉血：{primary.metrics['expected_hp_loss']:.1f}")
    if primary.confidence:
        lines.append(f"置信度：{_pct(primary.confidence)}")
    lines.extend(f"理由：{fact}" for fact in primary.facts)

    if rec.alternatives:
        alt = rec.alternatives[0]
        gap = primary.score - alt.score
        lines.append(f"【次选】{alt.label}（评分低 {gap:.3f}）")
    lines.append(
        f"模型：{rec.model_id}｜游戏版本：{rec.game_build}｜搜索节点：{rec.search_nodes}"
    )
    lines.extend(f"警告：{warning}" for warning in rec.warnings)
    return "\n".join(lines) + "\n"
