"""The out-of-combat choice rules, stated as a versioned policy.

Until now these picks were a positional constant -- ``index: 0`` for a boss relic,
``options[0]`` for an event -- which is both unexplainable and undeletable-by-evidence:
nothing recorded *why* a relic was taken, so a survival difference could not be
attributed to a rule change.  This module makes the rule explicit and versioned so a
run's outcome can be tied to it.

Three constraints, deliberately:

* only a candidate the bridge advertised for *this* state may be chosen;
* nothing may be keyed on the seed, the floor, or a remembered encounter -- the
  ranking reads the same fields for every run, so it cannot encode an answer;
* the tie-break is the lowest advertised index, which is what the old constant did.
  Every change is therefore a strict refinement, never a silent relocation of risk.
"""
from __future__ import annotations

from typing import Any, Sequence

POLICY_VERSION = "conservative-visible-v1"

#: Costs.  Chosen from wording actually seen in recorded live states, not from a
#: guess about how the game is usually localised; unseen wording simply scores zero.
DOWNSIDE_MARKERS = ("失去", "无法", "不能", "不得", "减半", "每回合", "诅咒", "受伤", "扣除")
#: Pure gains, or things that shrink the deck -- the usual safe value in a game where
#: a smaller deck is stronger and Neow's own removal offer was already preferred.
UPSIDE_MARKERS = ("移除", "升级", "回复", "获得", "增加", "全部")
#: Shrinking the deck outranks a generic gain.  This was previously a Neow-only
#: special case keyed on the event id; as a wording rule it applies to every act's
#: Ancient, and -- because it no longer looks at `event_id` -- to no run in particular.
REMOVAL_MARKERS = ("移除", "去掉")
REMOVAL_BONUS = 5


def _text(option: dict[str, Any]) -> str:
    return " ".join(
        str(option.get(field) or "")
        for field in ("title", "description", "relic_name", "relic_description", "name")
    )


def score_option(option: dict[str, Any]) -> int:
    """A visible-info preference, higher being safer.  Not a utility estimate."""
    text = _text(option)
    penalties = sum(1 for marker in DOWNSIDE_MARKERS if marker in text)
    bonuses = sum(1 for marker in UPSIDE_MARKERS if marker in text)
    if any(marker in text for marker in REMOVAL_MARKERS):
        bonuses += REMOVAL_BONUS
    return bonuses - penalties


def choose(candidates: Sequence[dict[str, Any]], *, prefer_proceed: bool = False) -> int:
    """Return the advertised index of the preferred candidate.

    ``prefer_proceed`` is for plain events, where leaving the offer alone is the
    conservative action; an Ancient's offer *is* the reward, so there the first
    scored pick is correct and leaving would skip a run-defining boon.
    """
    legal = [option for option in candidates if isinstance(option, dict)]
    if not legal:
        return 0
    if prefer_proceed:
        leaving = [o for o in legal if o.get("is_proceed") is True]
        if leaving:
            return int(leaving[0].get("index", 0))
    unlocked = [o for o in legal if o.get("is_locked") is not True] or legal
    best = max(unlocked, key=lambda o: (score_option(o), -int(o.get("index", 0))))
    return int(best.get("index", 0))
