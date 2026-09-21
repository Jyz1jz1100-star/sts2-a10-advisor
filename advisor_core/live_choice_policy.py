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

POLICY_VERSION = "conservative-visible-v2"

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


# ------------------------------------------------------------------ potions
#
# Measured on the real client: the player held an any-time potion (``BLOOD_POTION``
# in 61 recorded frames, ``FRUIT_JUICE`` in 247) across 3,285 out-of-combat frames
# and never drank, and it sat through 5,179 shop frames where its potion slots were
# already full -- 5,119 of them with an affordable potion on the shelf it could not
# take.  Neither was a decision to keep the resource: the two actions were simply
# absent from the player's action space.

#: Wording that marks a heal, and wording that marks a permanent maximum-HP gain.
#: Read off the text the bridge publishes, so no potion id is baked into the rule
#: and a re-translation shows up as a rule that stopped firing rather than as a
#: rule that quietly kept acting on the wrong thing.
#:
#: The two sets cannot simply be "contains 最大生命" versus "contains 回复": the real
#: heal text is 回复你最大生命30%的生命, which mentions both.  A maximum-HP gain is
#: therefore the phrase *without* a heal verb, which is what separates ``果汁``
#: (提升N点最大生命) from ``血之药水``.
HEAL_MARKERS = ("回复", "恢复")
MAX_HP_MARKERS = ("最大生命", "生命上限")
#: Below this fraction of HP a between-fight heal is worth spending: at or above it,
#: a rest site is the cheaper instrument and the run keeps its potion.
HEAL_SIP_BELOW = 0.6


def has_out_of_combat_use(potion: dict[str, Any]) -> bool:
    """True only when the state itself says the potion may be drunk now.

    ``usage`` is published by the staged bridge candidate; the installed one
    publishes only ``can_use_in_combat``, which is equally true for ``CombatOnly``
    and ``AnyTime``.  So with the installed bridge this returns False for every
    potion and the driver keeps its hands off -- the honest alternative to
    guessing which of the 64 potions the game lets you drink on the map.
    """
    if str(potion.get("usage") or "") != "AnyTime":
        return False
    # A potion that needs an enemy, a card or a pile has nothing to aim at between
    # fights, and posting one is a refused action.  ``AnyPlayer`` is on the list
    # because that is what the recorded real texts say for the two potions this
    # rule exists to use: 鲜血药水 ("回复你最大生命值的20%。") and 果汁
    # ("获得5点最大生命值。") both publish target AnyPlayer.
    return str(potion.get("target_type") or "").lower() in (
        "", "none", "self", "anyplayer", "anyself",
    )


def _effect_text(potion: dict[str, Any]) -> str:
    return str(potion.get("description") or "") + " " + str(potion.get("name") or "")


def _is_max_hp_gain(text: str) -> bool:
    return (
        any(marker in text for marker in MAX_HP_MARKERS)
        and not any(marker in text for marker in HEAL_MARKERS)
    )


def _is_heal(text: str) -> bool:
    return any(marker in text for marker in HEAL_MARKERS)


def potion_to_sip_now(
    state: dict[str, Any], *, at_rest_site: bool = False
) -> dict[str, Any] | None:
    """The held potion worth drinking between fights, if any.

    A maximum-HP wording is a permanent gain that cannot be wasted, so it is taken
    whenever it is legal.  A heal is spent only when HP is actually low, and not
    while standing at a rest site, where the same healing costs nothing.
    """
    player = state.get("player") or {}
    held = [p for p in (player.get("potions") or []) if isinstance(p, dict)]
    sippable = [p for p in held if has_out_of_combat_use(p)]
    if not sippable:
        return None
    for potion in sippable:
        if _is_max_hp_gain(_effect_text(potion)):
            return potion
    if at_rest_site:
        return None
    hp, max_hp = player.get("hp"), player.get("max_hp")
    if not isinstance(hp, (int, float)) or not isinstance(max_hp, (int, float)) or max_hp <= 0:
        return None
    if hp / max_hp >= HEAL_SIP_BELOW:
        return None
    for potion in sippable:
        if _is_heal(_effect_text(potion)):
            return potion
    return None


def potion_to_discard_for(
    state: dict[str, Any], offered: dict[str, Any]
) -> dict[str, Any] | None:
    """The held potion to give up for an offered one, or None to keep it.

    Only ever called when the shop decision already wants this purchase and the
    slots are full, so a discard is never a unilateral loss -- it is the cost of a
    pick the policy made on price and category.  Even then the swap needs a strict
    visible improvement: both sides must carry effect text, and the offered text has
    to score higher than the weakest held text.  Held potions are never re-ranked
    against a description the bridge did not publish.
    """
    player = state.get("player") or {}
    held = [
        p for p in (player.get("potions") or [])
        if isinstance(p, dict) and p.get("id")
    ]
    if not held:
        return None
    offered_text = str(
        (offered.get("potion_description") or "") + " "
        + (offered.get("potion_name") or "")
    )
    if not offered_text.strip():
        return None
    offered_score = score_option({"description": offered_text})
    weakest = min(
        held,
        key=lambda p: (
            score_option({"description": _effect_text(p)}),
            -int(p.get("slot", 0)),
        ),
    )
    if offered_score <= score_option({"description": _effect_text(weakest)}):
        return None
    return weakest
