"""Expanded, deterministic V2 observation vector for the run contract.

The raw simulator observation (``RUN_OBS_SIZE`` ints) omits decision-relevant
visible information the V2 training contract requires: the full deck with
upgrade status, relic identities, the complete shop inventory with every
price, map candidate coordinates, and Neow/reward lists.  This module builds
one fixed-width integer vector from the raw observation plus the already
public ``Sts2Run_GetStateList`` data, with no emulator modification.

The layout is versioned and stable: block names, order, and widths are
derived once from ``advisor_core.card_targeting_v2`` tables, and
``observation_contract()`` records them for dataset/checkpoint provenance.
A policy may only ever read this vector (never hidden piles or futures); all
blocks are player-visible.

Known visibility gaps, recorded so metrics can report them (never silently
swallowed by a hash collision):

* relic counters and enemy definition identities are not exposed by any
  native API in this emulator build; the vector therefore carries relic
  presence (not counters) and enemy HP/intent only;
* unknown object ids (vocabulary drift after an emulator update) are skipped
  and counted in the trailing ``unknown_id_events`` slot.
"""

from __future__ import annotations

import numpy as np

from advisor_core.card_targeting_v2 import (
    CARD_VOCAB,
    POTION_VOCAB,
    RELIC_VOCAB,
    dense_index,
)

from .v2_constants import (
    COMBAT_OBS_SIZE,
    MAX_ENEMIES,
    MAX_HAND,
    NODE_NONE,
    PHASE_NAMES,
    PHASE_NONE_COUNT,
    RUN_EXTRA_OBS_SIZE,
    RUN_OBS_SIZE,
    SHOP_CARD_COUNT,
)

# ---------------------------------------------------------------------------
# Block layout (fixed order; the contract hash below pins it).
# ---------------------------------------------------------------------------

#: Raw combat block passthrough: 164 native ints, exactly as the simulator
#: emits.  Contains HP/block/energy, hand (def id + upgraded), potions, player
#: buffs, per-enemy HP/max HP/block/intent/buffs, secondary intents, gold.
_COMBAT_BLOCK = COMBAT_OBS_SIZE
_RUN_BLOCK = RUN_EXTRA_OBS_SIZE

_BLOCKS: list[tuple[str, int]] = [
    ("combat_native_passthrough", _COMBAT_BLOCK),
    ("run_native_passthrough", _RUN_BLOCK),
    ("deck_presence", len(CARD_VOCAB)),
    ("deck_upgraded_presence", len(CARD_VOCAB)),
    ("relic_presence", len(RELIC_VOCAB)),
    ("potion_slot_presence", len(POTION_VOCAB)),
    ("neow_options", 3),
    ("card_reward_upgraded_flags", 3),
    ("pending_rewards", 4),
    ("shop_cards_4_to_7", SHOP_CARD_COUNT - 3),
    ("shop_costs", 14),
    ("map_option_coords", 8),
    ("alive_enemy_count", 1),
    ("hand_single_target_flags", MAX_HAND),
    ("phase_onehot", PHASE_NONE_COUNT),
    ("current_node_type_onehot", 8),
    ("unknown_id_events", 1),
]

BLOCK_SIZES: tuple[tuple[str, int], ...] = tuple(_BLOCKS)
OBS_SIZE: int = sum(width for _name, width in _BLOCKS)

_OFFSETS: dict[str, int] = {}
_cursor = 0
for _name, _width in _BLOCKS:
    _OFFSETS[_name] = _cursor
    _cursor += _width
BLOCK_OFFSETS = dict(_OFFSETS)


def observation_contract() -> dict[str, object]:
    """Provenance payload describing this exact observation layout."""

    return {
        "v2_observation_schema": 1,
        "size": OBS_SIZE,
        "blocks": [
            {"name": name, "width": width, "offset": BLOCK_OFFSETS[name]}
            for name, width in BLOCK_SIZES
        ],
        "card_vocab_size": len(CARD_VOCAB),
        "relic_vocab_size": len(RELIC_VOCAB),
        "potion_vocab_size": len(POTION_VOCAB),
        "known_gaps": [
            "relic counters are not exposed by the native API",
            "enemy definition identities are not exposed by the native API",
        ],
    }


def _set_slot(vector: np.ndarray, base: int, vocab: tuple[int, ...], object_id: int,
              value: int, counter: list[int]) -> None:
    index = dense_index(vocab, object_id)
    if index < 0:
        counter[0] += 1
        return
    vector[base + index] = value


def build_observation(
    raw_obs: np.ndarray,
    *,
    deck: tuple[int, ...],
    relics: tuple[int, ...],
    potions: tuple[int, ...],
    neow_options: tuple[int, ...],
    reward_upgraded: tuple[int, ...],
    pending_rewards: tuple[int, ...],
    map_coords: tuple[int, ...],
    shop_cards: tuple[int, ...],
    shop_costs: tuple[int, ...],
) -> np.ndarray:
    """Assemble the fixed-width V2 observation from simulator-visible data.

    ``raw_obs`` is the untouched ``RUN_OBS_SIZE`` int32 array from the
    emulator.  The list arguments are the outputs of the existing public
    ``run_state_list`` calls (deck entries use the native signed encoding:
    negative id means upgraded).
    """

    raw = np.asarray(raw_obs, dtype=np.int32)
    if raw.shape != (RUN_OBS_SIZE,):
        raise ValueError(f"raw observation must have shape ({RUN_OBS_SIZE},), got {raw.shape}")

    vector = np.zeros(OBS_SIZE, dtype=np.int32)
    unknown: list[int] = [0]

    vector[:_COMBAT_BLOCK] = raw[:_COMBAT_BLOCK]
    vector[_COMBAT_BLOCK : _COMBAT_BLOCK + _RUN_BLOCK] = raw[_COMBAT_BLOCK:RUN_OBS_SIZE]

    deck_base = BLOCK_OFFSETS["deck_presence"]
    deck_up_base = BLOCK_OFFSETS["deck_upgraded_presence"]
    for entry in deck:
        entry = int(entry)
        upgraded = entry < 0
        _set_slot(vector, deck_base, CARD_VOCAB, abs(entry), 1, unknown)
        if upgraded:
            _set_slot(vector, deck_up_base, CARD_VOCAB, abs(entry), 1, unknown)

    relic_base = BLOCK_OFFSETS["relic_presence"]
    for relic_id in relics:
        _set_slot(vector, relic_base, RELIC_VOCAB, int(relic_id), 1, unknown)

    potion_base = BLOCK_OFFSETS["potion_slot_presence"]
    for potion_id in potions:
        if int(potion_id) != 0:
            _set_slot(vector, potion_base, POTION_VOCAB, int(potion_id), 1, unknown)

    def _copy(name: str, values: tuple[int, ...], count: int) -> None:
        base = BLOCK_OFFSETS[name]
        for position in range(min(count, len(values))):
            vector[base + position] = int(values[position])

    _copy("neow_options", neow_options, 3)
    _copy("card_reward_upgraded_flags", reward_upgraded, 3)
    _copy("pending_rewards", pending_rewards, 4)
    _copy("shop_cards_4_to_7", tuple(shop_cards[3:SHOP_CARD_COUNT]), SHOP_CARD_COUNT - 3)
    _copy("shop_costs", shop_costs, 14)
    _copy("map_option_coords", map_coords, 8)

    phase = int(raw[_COMBAT_BLOCK + 0])
    node_type = int(raw[_COMBAT_BLOCK + 8])
    alive_enemies = 0
    enemy_base = 54
    for slot in range(MAX_ENEMIES):
        max_hp = int(raw[enemy_base + slot * 15 + 1])
        hp = int(raw[enemy_base + slot * 15])
        if max_hp > 0 and hp > 0:
            alive_enemies += 1
    vector[BLOCK_OFFSETS["alive_enemy_count"]] = alive_enemies

    hand_base = BLOCK_OFFSETS["hand_single_target_flags"]
    from advisor_core.card_targeting_v2 import is_single_target

    for index in range(MAX_HAND):
        def_id = int(raw[8 + index * 2])
        if def_id != 0 and is_single_target(def_id):
            vector[hand_base + index] = 1

    if 0 <= phase < PHASE_NONE_COUNT:
        vector[BLOCK_OFFSETS["phase_onehot"] + phase] = 1
    if 0 <= node_type < 8:
        vector[BLOCK_OFFSETS["current_node_type_onehot"] + node_type] = 1

    vector[BLOCK_OFFSETS["unknown_id_events"]] = unknown[0]
    return vector


__all__ = [
    "BLOCK_OFFSETS",
    "BLOCK_SIZES",
    "OBS_SIZE",
    "PHASE_NAMES",
    "NODE_NONE",
    "build_observation",
    "observation_contract",
]
