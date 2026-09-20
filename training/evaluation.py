from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from .campaign_content import CAMPAIGN_CONTENT_COVERAGE, CAMPAIGN_ENVIRONMENT_VERSION
from .metrics import EpisodeMetric, EvaluationMetrics, summarize_episodes


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


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
    campaign: bool = False,
) -> EvaluationMetrics:
    """Roll out one episode per seed with a defensive step cap.

    ``campaign`` asks the simulator for the three-act walk instead of the single act it
    draws per seed; it changes which act a seed plays, so an episode's ``won`` under
    campaign is not comparable to the same seed's one-act result.

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
            observation, reset_info = env.reset(
                seed=seed, options={"campaign": True} if campaign else None
            )
            total_reward = 0.0
            steps = 0
            illegal_actions = 0
            rejection_events = 0
            terminated = False
            truncated = False
            step_capped = False
            info = dict(reset_info)
            while not (terminated or truncated):
                mask = env.action_masks()
                # A reachable simulator defect can expose an all-zero mask
                # (seed 20000039, map phase after a shop on the checked-in
                # emulator).  There is no action the policy could legally
                # choose, so classify this as an environment truncation, not
                # as a policy illegal-action violation.
                if not any(bool(value) for value in mask):
                    truncated = True
                    info = {**info, "simulator_dead_end": "empty_action_mask"}
                    break
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
                # Filter-mode reports its absorbed native-rejection count as a
                # cumulative per-episode field on every step's info.
                rejection_events = max(
                    rejection_events, int(info.get("rejection_events", 0) or 0)
                )
                if max_steps_per_episode is not None and steps >= max_steps_per_episode:
                    truncated = True
                    step_capped = True
            final_floor = info.get("floor")
            act = reset_info.get("act")
            encounter = reset_info.get("encounter")
            won = bool(terminated and info.get("player_won", False))
            # Distinct from ``won``: the engine only sets run_cleared after a campaign run's
            # final act clears both of its bosses, so this cannot be bought with one act.
            campaign_cleared = bool(terminated and info.get("run_cleared", False))
            # End-of-episode HP fraction for the joint ablation metric
            # (mean_final_hp_fraction); the V2 info stack carries player_hp /
            # player_max_hp from the native run info on every transition.
            hp = _finite(info.get("player_hp"))
            max_hp = _finite(info.get("player_max_hp"))
            if hp is None or max_hp is None or max_hp <= 0:
                final_hp_fraction = None
            else:
                final_hp_fraction = min(1.0, max(0.0, hp / max_hp))
            # A V2 curriculum boundary truncation is a stage completion for
            # floor-limited levels; the run contract wrapper labels it.
            boundary = won or bool(
                truncated and info.get("curriculum_truncated", False)
            )
            dead_end = info.get("simulator_dead_end")
            if dead_end is None and illegal_actions:
                dead_end = "policy_illegal_action"
            if dead_end is None and step_capped:
                # The evaluator's own horizon is a classification, too: it
                # must never masquerade as an *unclassified* dead end.
                dead_end = "step_cap"
            episode_metrics.append(
                EpisodeMetric(
                    seed=seed,
                    # RunEngine.player_won is actually "the most recently
                    # completed combat was won" and remains true on later
                    # map/shop states. Only a terminal run may be counted as
                    # a run win; otherwise a truncated post-combat dead-end
                    # becomes a false positive (seed 20000039, 2026-09-01).
                    won=won,
                    campaign_cleared=campaign_cleared,
                    terminated=bool(terminated),
                    truncated=bool(truncated),
                    steps=steps,
                    episode_return=total_reward,
                    illegal_actions=illegal_actions,
                    final_floor=(int(final_floor) if final_floor is not None else None),
                    act=(int(act) if act is not None else None),
                    encounter=(str(encounter) if encounter is not None else None),
                    boundary_reached=boundary,
                    dead_end_reason=(str(dead_end) if dead_end is not None else None),
                    rejection_events=rejection_events,
                    final_hp_fraction=final_hp_fraction,
                )
            )
        finally:
            env.close()
    metrics = summarize_episodes(
        episode_metrics,
        stage=stage,
        split=split,
        scope=scope,
        checkpoint=str(checkpoint),
        deterministic=deterministic,
        experimental=experimental,
    )
    if campaign:
        # A campaign number without its coverage declaration reads as a real
        # three-act result, which is the one thing it is not.
        metrics = replace(
            metrics,
            environment_version=CAMPAIGN_ENVIRONMENT_VERSION,
            content_coverage=CAMPAIGN_CONTENT_COVERAGE,
        )
    return metrics
