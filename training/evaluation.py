from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from .metrics import EpisodeMetric, EvaluationMetrics, summarize_episodes


class MaskedPolicy(Protocol):
    def predict(
        self, observation: Any, *, action_masks: Any, deterministic: bool
    ) -> tuple[Any, Any]: ...


EnvironmentFactory = Callable[[int], Any]


def _scalar_action(action: Any) -> int:
    if hasattr(action, "item"):
        return int(action.item())
    if isinstance(action, (list, tuple)):
        if len(action) != 1:
            raise ValueError(f"expected one action, got {len(action)}")
        return _scalar_action(action[0])
    return int(action)


def evaluate_policy(
    model: MaskedPolicy,
    *,
    env_factory: EnvironmentFactory,
    seeds: list[int],
    stage: str,
    split: str,
    scope: str,
    checkpoint: Path | str,
    deterministic: bool = True,
    experimental: bool = False,
    max_steps_per_episode: int | None = None,
) -> EvaluationMetrics:
    """Roll out one episode per seed with a defensive step cap.

    ``max_steps_per_episode`` must come from the caller's stage configuration
    and must not rely on the environment's own truncation signal: the
    simulator's native invalid-action path (``run_step`` status != 0) returns
    ``(-1.0, False, False)`` and skips its internal episode counter, so a
    policy whose mask-legal action is rejected by the native layer would spin
    forever otherwise (observed live 2026-09-01: 9.1M steps in one episode).
    """

    if max_steps_per_episode is not None and max_steps_per_episode <= 0:
        raise ValueError("max_steps_per_episode must be positive")
    episode_metrics: list[EpisodeMetric] = []
    for seed in seeds:
        env = env_factory(seed)
        try:
            observation, reset_info = env.reset(seed=seed)
            total_reward = 0.0
            steps = 0
            illegal_actions = 0
            terminated = False
            truncated = False
            info = dict(reset_info)
            while not (terminated or truncated):
                mask = env.action_masks()
                action_raw, _ = model.predict(
                    observation,
                    action_masks=mask,
                    deterministic=deterministic,
                )
                action = _scalar_action(action_raw)
                if action < 0 or action >= len(mask) or not bool(mask[action]):
                    illegal_actions += 1
                    truncated = True
                    break
                observation, reward, terminated, truncated, info = env.step(action)
                total_reward += float(reward)
                steps += 1
                if max_steps_per_episode is not None and steps >= max_steps_per_episode:
                    truncated = True
            final_floor = info.get("floor")
            encounter = reset_info.get("encounter")
            episode_metrics.append(
                EpisodeMetric(
                    seed=seed,
                    won=bool(info.get("player_won", False)),
                    terminated=bool(terminated),
                    truncated=bool(truncated),
                    steps=steps,
                    episode_return=total_reward,
                    illegal_actions=illegal_actions,
                    final_floor=(int(final_floor) if final_floor is not None else None),
                    encounter=(str(encounter) if encounter is not None else None),
                )
            )
        finally:
            env.close()
    return summarize_episodes(
        episode_metrics,
        stage=stage,
        split=split,
        scope=scope,
        checkpoint=str(checkpoint),
        deterministic=deterministic,
        experimental=experimental,
    )
