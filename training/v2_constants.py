"""V2 simulator protocol constants (mirrors, not imports, of sts2_gym).

These describe the *native* contract of the locked emulator build.  They are
deliberately declared here so the V2 training stack can unit-test its
contract logic without loading the native DLL, and so a version drift becomes
an explicit, testable mismatch instead of an implicit third-party import.
"""

from __future__ import annotations

#: Versioned identity of the Python-side V2 contract this file describes.
V2_CONTRACT_VERSION = "2026.09.01-r1"

# Native sizes for the locked emulator build (verified against
# Sts2Run_NativeApiVersion == 8).
COMBAT_OBS_SIZE = 164
RUN_EXTRA_OBS_SIZE = 35
RUN_OBS_SIZE = COMBAT_OBS_SIZE + RUN_EXTRA_OBS_SIZE  # 199
RUN_MAX_ACTIONS = 32
RUN_INFO_SIZE = 11
MAP_CHOICES = 4
MAX_HAND = 10
MAX_ENEMIES = 6
MAX_PLAYER_BUFFS = 10
MAX_ENEMY_BUFFS = 5
SHOP_CARD_COUNT = 7

# Run phases (RunPhase).
PHASE_COMBAT = 0
PHASE_CARD_REWARD = 1
PHASE_MAP = 2
PHASE_REST = 3
PHASE_SHOP = 4
PHASE_RELIC_REWARD = 5
PHASE_COMPLETE = 6
PHASE_EVENT = 7
PHASE_ANCIENT = 8
PHASE_TRANSFORM_SELECT = 9
PHASE_TREASURE = 10
PHASE_NAMES = (
    "combat",
    "card_reward",
    "map",
    "rest",
    "shop",
    "relic_reward",
    "complete",
    "event",
    "ancient",
    "transform_select",
    "treasure",
)
#: One-hot width: every phase plus one catch-all bucket for future values.
PHASE_NONE_COUNT = 11

# Node types (RunConstants.Node*).
NODE_NONE = 0
NODE_NORMAL = 1
NODE_ELITE = 2
NODE_REST = 3
NODE_SHOP = 4
NODE_RELIC = 5
NODE_BOSS = 6
NODE_EVENT = 7

# Well-known action anchors.
REWARD_SKIP_ACTION = 3
SHOP_REMOVE_ACTION = 13
SHOP_SKIP_ACTION = 14
EVENT_SKIP_ACTION = 3
REST_HEAL_ACTION = 0
REST_UPGRADE_ACTION = 1

#: state-list ids accepted by the existing native Sts2Run_GetStateList.
STATE_LIST_DECK = 0
STATE_LIST_RELICS = 1
STATE_LIST_POTIONS = 2
STATE_LIST_NEOW = 3
STATE_LIST_SHOP_COSTS = 4
STATE_LIST_REWARD_UPGRADED = 5
STATE_LIST_PENDING_REWARDS = 6
STATE_LIST_MAP_COORDS = 7
STATE_LIST_SHOP_CARDS = 8
STATE_LIST_SHOP_RELICS = 9
STATE_LIST_SHOP_POTIONS = 10

__all__ = [
    "COMBAT_OBS_SIZE",
    "EVENT_SKIP_ACTION",
    "MAP_CHOICES",
    "MAX_ENEMIES",
    "MAX_ENEMY_BUFFS",
    "MAX_HAND",
    "MAX_PLAYER_BUFFS",
    "NODE_BOSS",
    "NODE_ELITE",
    "NODE_EVENT",
    "NODE_NONE",
    "NODE_NORMAL",
    "NODE_RELIC",
    "NODE_REST",
    "NODE_SHOP",
    "PHASE_ANCIENT",
    "PHASE_CARD_REWARD",
    "PHASE_COMBAT",
    "PHASE_COMPLETE",
    "PHASE_EVENT",
    "PHASE_MAP",
    "PHASE_NAMES",
    "PHASE_NONE_COUNT",
    "PHASE_RELIC_REWARD",
    "PHASE_REST",
    "PHASE_SHOP",
    "PHASE_TREASURE",
    "PHASE_TRANSFORM_SELECT",
    "REWARD_SKIP_ACTION",
    "REST_HEAL_ACTION",
    "REST_UPGRADE_ACTION",
    "RUN_EXTRA_OBS_SIZE",
    "RUN_INFO_SIZE",
    "RUN_MAX_ACTIONS",
    "RUN_OBS_SIZE",
    "SHOP_CARD_COUNT",
    "SHOP_REMOVE_ACTION",
    "SHOP_SKIP_ACTION",
    "STATE_LIST_DECK",
    "STATE_LIST_MAP_COORDS",
    "STATE_LIST_NEOW",
    "STATE_LIST_PENDING_REWARDS",
    "STATE_LIST_POTIONS",
    "STATE_LIST_RELICS",
    "STATE_LIST_REWARD_UPGRADED",
    "STATE_LIST_SHOP_CARDS",
    "STATE_LIST_SHOP_COSTS",
    "STATE_LIST_SHOP_POTIONS",
    "STATE_LIST_SHOP_RELICS",
    "V2_CONTRACT_VERSION",
]
