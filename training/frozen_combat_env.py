"""G1: hand combat to a frozen executor so the agent's gradient never sees a fight.

The delivery contract says the real client's fights are played by a third-party solver and this
project's policy decides *between* fights.  The measured training stream did not match that: 80.2%
of the steps a campaign arm updates on are in-fight card selection
(`docs/evidence/phase_action_share_20261007.json`).  So the product's own division of labour is
made structural here -- the wrapper answers for every combat state with a policy that is not being
updated, and returns control only at an out-of-combat state.

What this does *not* do is make the fights disappear.  Every combat transition still happens,
still moves the simulator, and still pays its reward; it is absorbed into the one transition the
agent is charged for, and the count is reported as ``combat_steps`` in the returned info.  The
reward stays correct because the shaping is potential-based: consecutive
``gamma * Phi(s') - Phi(s)`` terms telescope across the absorbed chain, so the sum the agent sees
equals the shaping of the compressed transition, with any terminal bonus paid exactly once.

Two consequences a reader should not have to rediscover:

* A stage's ``timesteps`` budget now counts *out-of-combat* decisions, so it buys roughly five
  times as many episodes as the same number did before this wrapper.  Arms are comparable at
  equal episodes, never at equal timesteps.
* A fight that ends the run (death, or the final boss) terminates the *returned* transition, so
  the agent is credited for the map node it walked into.  An executor that stalls is not silently
  truncated into a normal step: hitting ``max_combat_steps`` sets
  ``combat_executor_step_cap`` and truncates, which is what makes a broken executor visible.
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

import gymnasium as gym
import numpy as np

#: The value ``V2FlatActionEnv`` writes into ``info["phase_name"]`` for a fight.
COMBAT_PHASE = "combat"

#: A fight that has not ended after this many executor actions is not being finished by the
#: executor; it is stuck.  Measured ceiling is far below it -- the campaign's fights run to the
#: engine's own turn limit -- so this only fires on an executor that cannot advance the state.
DEFAULT_MAX_COMBAT_STEPS = 400

__all__ = [
    "COMBAT_PHASE",
    "DEFAULT_MAX_COMBAT_STEPS",
    "FrozenCombatExecutor",
    "IllegalExecutorAction",
    "frozen_maskable_executor",
]


class IllegalExecutorAction(RuntimeError):
    """The frozen executor proposed an action the contract mask says is illegal.

    Raised rather than rounded to a legal pick: an executor that cannot name a legal action is
    broken, and quietly substituting one would hide that from every measurement below it.
    """


def phase_of(info: Any) -> str:
    return str((info or {}).get("phase_name") or "")


class FrozenCombatExecutor(gym.Wrapper):
    """Play every combat state with ``executor``; expose only out-of-combat states.

    ``executor`` is ``callable(observation, action_mask) -> int`` and is expected to be frozen --
    it must not be the learner itself, or the loss would still reach combat through the side door.
    """

    def __init__(
        self,
        env: gym.Env,
        executor: Callable[[Any, np.ndarray], int],
        *,
        max_combat_steps: int = DEFAULT_MAX_COMBAT_STEPS,
    ) -> None:
        super().__init__(env)
        if int(max_combat_steps) <= 0:
            raise ValueError("max_combat_steps must be positive")
        self._executor = executor
        self.max_combat_steps = int(max_combat_steps)
        self.absorbed_combat_steps = 0
        self.absorbed_combat_episodes = 0

    # sb3_contrib's ActionMasker calls this on the wrapped env, and gymnasium 1.x does not
    # forward unknown attributes through Wrapper.__getattr__.
    def action_masks(self) -> np.ndarray:
        return self.env.action_masks()  # type: ignore[attr-defined]

    def reset(self, *, seed: int | None = None, options: dict | None = None):
        observation, info = self.env.reset(seed=seed, options=options)
        # The run should open at the ancient's offer, but a stack that hands back a fight here
        # would leak a combat state into the agent's first observation, so settle it anyway.
        observation, _r, _t, _tr, info = self._drive(observation, 0.0, False, False, info)
        return observation, info

    def step(self, action: Any):
        observation, reward, terminated, truncated, info = self.env.step(int(action))
        if terminated or truncated:
            return observation, float(reward), bool(terminated), bool(truncated), info
        return self._drive(observation, float(reward), bool(terminated), bool(truncated), info)

    # --------------------------------------------------------------- internals

    def _drive(self, observation, reward, terminated, truncated, info):
        combat_steps = 0
        while not terminated and not truncated and phase_of(info) == COMBAT_PHASE:
            if combat_steps >= self.max_combat_steps:
                info = {**(info or {}), "combat_executor_step_cap": True,
                        "combat_steps": combat_steps}
                self._record(combat_steps)
                return observation, reward, False, True, info
            mask = np.asarray(self.action_masks(), dtype=bool)
            chosen = int(self._executor(observation, mask))
            if not 0 <= chosen < mask.size or not bool(mask[chosen]):
                raise IllegalExecutorAction(
                    f"frozen executor chose flat action {chosen} which the "
                    f"{phase_of(info)} mask does not advertise "
                    f"({int(mask.sum())} legal of {mask.size})")
            observation, step_reward, terminated, truncated, info = self.env.step(chosen)
            reward += float(step_reward)
            combat_steps += 1
        if combat_steps:
            info = {**(info or {}), "combat_steps": combat_steps}
            self._record(combat_steps)
        return observation, reward, bool(terminated), bool(truncated), info

    def _record(self, steps: int) -> None:
        self.absorbed_combat_steps += steps
        self.absorbed_combat_episodes += 1


def frozen_maskable_executor(
    checkpoint,
    *,
    device: str = "cpu",
    deterministic: bool = True,
) -> Callable[[Any, Sequence[Any]], int]:
    """Load ``checkpoint`` once and return it as a frozen ``executor`` callable.

    The learner is a separate object, so SB3 updating its own copy cannot change what this one
    plays -- that is the property G1 needs.  A checkpoint loaded with ``env=`` would also bind the
    model to a vector env; this path deliberately does not, and only calls ``predict``.
    """
    from sb3_contrib import MaskablePPO

    model = MaskablePPO.load(str(checkpoint), device=device)

    def executor(observation, action_mask) -> int:
        raw, _state = model.predict(
            observation, action_masks=np.asarray(action_mask), deterministic=deterministic)
        return int(raw.item() if hasattr(raw, "item") else raw)

    return executor
