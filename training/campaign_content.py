"""What the emulator's campaign mode actually contains, in machine-readable form.

Every simulator number carries one of the declarations below, because the emulator's
three-act campaign is an approximation of the shipped game's three acts and the
approximation has been edited.  ``sts2sim-campaign-approx-v1`` walked real Act 1
content twice more; ``sts2sim-campaign-fidelity-v2`` closes the encounter pools, the
paired boss and the invented boss relic, and still has no per-act Ancient, no per-act
map shape and no verified reward distributions.  Recording which of those is true next
to the number is what keeps a changed environment from being read as the same
environment, and keeps the result tiers below from being averaged together.

The evidence column points at the locked build's own decompiled sources; the
emulator side points at the pools it actually reads.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterable

#: Bump when the campaign's *content* changes, so pre-change artifacts stay separable.
#: v5 is v4's content with the engine's replayed-trace overrides removed: nothing was
#: added, and what the campaign contains is otherwise unchanged.
CAMPAIGN_ENVIRONMENT_VERSION = "sts2sim-campaign-fidelity-v5"

#: The base name the fidelity line publishes under.  approx-v1 is frozen below, and
#: results from the two must never be averaged, re-judged, or continued from each
#: other's checkpoints.
FIDELITY_ENVIRONMENT_VERSION_BASE = "sts2sim-campaign-fidelity"

#: The tiers as they stood when approx-v1 was frozen.  ``CAMPAIGN_CONTENT_COVERAGE_APPROX_V1``
#: carries this literal so its pinned digest cannot move when a new tier is added.
RESULT_TIERS_AT_APPROX_V1 = {
    "simulator_single_act": "one act per seed; every pre-2026-09-20 artifact",
    "simulator_three_act_approx": "three stages, Act 1 content reused for stages 2 and 3",
    "simulator_three_act_content_verified": (
        "requires Hive and Glory pools, a per-act Ancient, and a boss-to-boss exit; "
        "no artifact in this repository is at this tier yet"
    ),
    "live_full_run": "the real client, victory screen observed; not achieved",
}

#: The tier set as it read when fidelity-v2 was frozen, kept as a literal so v2's
#: pinned digest cannot move when v3 adds a tier.
RESULT_TIERS_AT_FIDELITY_V2 = {
    "simulator_single_act": "one act per seed; every pre-2026-09-20 artifact",
    "simulator_three_act_approx": "three stages, Act 1 content reused for stages 2 and 3",
    "simulator_three_act_content_verified": (
        "requires Hive and Glory pools, a per-act Ancient, and a boss-to-boss exit; "
        "no artifact in this repository is at this tier yet"
    ),
    "live_full_run": "the real client, victory screen observed; not achieved",
    "simulator_three_act_pools_and_pair_boss": (
        "each stage draws the act the shipped campaign defines (Overgrowth -> Hive -> Glory), "
        "the final act's paired boss is a map row past the first, and no boss relic is "
        "invented; per-act Ancients, per-act map shape and the reward/upgrade "
        "distributions are still not real, so this tier is not content-verified"
    ),
}

#: The result tiers that must never be quoted as one number.
RESULT_TIERS = {
    **RESULT_TIERS_AT_APPROX_V1,
    "simulator_three_act_pools_and_pair_boss": (
        "each stage draws the act the shipped campaign defines (Overgrowth -> Hive -> Glory), "
        "the final act's paired boss is a map row past the first, and no boss relic is "
        "invented; per-act Ancients, per-act map shape and the reward/upgrade "
        "distributions are still not real, so this tier is not content-verified"
    ),
    "simulator_three_act_pools_ancients_and_pair_boss": (
        "G1+G2+G3+G6 closed: per-act encounter pools, the act's own ancient met on entry "
        "with its real option pools, a travelled-to second boss, and no invented boss "
        "relic. Per-act map shape (G4) and the upgrade/reward distributions (G5) are "
        "still not real, so this tier is still not content-verified"
    ),
    "simulator_three_act_campaign_shape_and_rewards": (
        "all six hard gates closed: on top of fidelity-v3, each act generates its own map "
        "depth, room queues and event pool, rewards and card-upgrade odds follow the act and "
        "the room type, and the boss floors the geometry implies are the ones the real client "
        "measured (17/33/48). It is still *not* the tier above: no ascension model, no "
        "unknown-node re-roll, no TheArchitect exit, two weak encounter variants absent from "
        "the build's data, and room placement that can fall short of the queue -- so this tier is "
        "still not content-verified: a number from here says what the campaign contains, not that "
        "it is verified equal to the shipped game"
    ),
}

#: The progression the shipped build actually defines.
REAL_PROGRESSION = {
    "act_1": {"model": "Overgrowth", "alternate": "Underdocks", "ancients": ["Neow"]},
    "act_2": {
        "model": "Hive",
        "ancients": ["Orobas", "Pael", "Tezcatara"],
        "bosses": ["TheInsatiableBoss", "KnowledgeDemonBoss", "KaiserCrabBoss"],
    },
    "act_3": {
        "model": "Glory",
        "ancients": ["Nonupeipe", "Tanx", "Vakuu"],
        "bosses": ["QueenBoss", "TestSubjectBoss", "AeonglassBoss"],
        "second_boss_at_ascension": 10,
    },
    "evidence": {
        "progression": "ActModel.cs:510-515 GetDefaultList()",
        "underdocks_is_act_1": "ActModel.cs:498-507 replaces list[0] only",
        "act_contents": "Models.Acts/{Overgrowth,Hive,Glory,Underdocks}.cs",
        "second_boss": "RunManager.cs:685-691 (last act + AscensionLevel.DoubleBoss)",
        "second_boss_node": "StandardActMap.cs:88-91,231-234 SecondBossMapPoint",
        "victory": "RunManager.cs:1207-1246 -> EventRoom<TheArchitect>",
    },
}

#: The campaign as it stood on 2026-09-21, before the fidelity line.  Kept verbatim
#: and pinned by digest: an artifact stamped approx-v1 has to stay re-judgeable after
#: the engine changed underneath it, so nothing in here may track a live constant.
#: ``result_tiers`` is a literal copy for exactly that reason -- the live tiers have
#: since grown a member.
CAMPAIGN_CONTENT_COVERAGE_APPROX_V1: dict[str, object] = {
    "verdict": "approximate",
    "result_tier": "simulator_three_act_approx",
    "environment_version": "sts2sim-campaign-approx-v1",
    "summary": (
        "Stage 1 is Overgrowth content. Stages 2 and 3 both draw the Underdocks pools, so "
        "the campaign replays Act 1 material rather than walking Overgrowth -> Hive -> Glory. "
        "Reaching floor 50 therefore shows the state machine can traverse three stages; it "
        "does not show the real third act is represented."
    ),
    "stages": {
        "1": {
            "pools": "Overgrowth weak/normal/elite/boss",
            "matches_real_game_act": True,
            "evidence": "Core/Run/RunConstants.cs:104-112",
        },
        "2": {
            "pools": "Underdocks weak/normal/elite/boss",
            "matches_real_game_act": False,
            "real_act_at_this_position": "Hive",
            "note": "Underdocks is the build's alternate Act 1, never a later act",
            "evidence": "Core/Run/RunConstants.cs:112-120",
        },
        "3": {
            "pools": "Underdocks weak/normal/elite/boss (same tables as stage 2)",
            "matches_real_game_act": False,
            "real_act_at_this_position": "Glory",
            "evidence": "Core/Run/RunMapGenerator.cs:19 selects the Underdocks branch for any act != 1",
        },
    },
    "not_modelled": [
        "Hive encounters and events (real Act 2)",
        "Glory encounters and events (real Act 3)",
        "a per-act Ancient event; only the run-start Neow choice exists",
        "the boss-to-boss exit: the real game returns to the map and travels to a second "
        "boss node, the emulator starts the paired combat immediately",
        "any per-act or per-floor difficulty scaling",
    ],
    "implemented_but_unreferenced": {
        "note": (
            "These real act 2/3 encounters exist in the emulator's factory and enemy AI but "
            "no act pool names them, so no campaign run can ever meet them."
        ),
        "encounter_ids": [66, 70, 71, 73, 75, 76, 78, 80, 81],
        "evidence": "Core/Combat/CombatFactory.cs:676-737",
    },
    "real_progression": REAL_PROGRESSION,
    "result_tiers": RESULT_TIERS_AT_APPROX_V1,
    "may_be_quoted_as": [
        "reachability of the emulator's three-stage state machine",
        "a regression control for single-act artifacts",
    ],
    "must_not_be_quoted_as": [
        "real three-act coverage",
        "live A10 win rate",
        "evidence that a strategy can clear the shipped game",
    ],
}

#: G6 + G1 + G3, nothing else. Frozen on 2026-09-22 when fidelity-v3 took the live
#: version: kept verbatim, with its tier set copied rather than referenced, so an
#: artifact stamped v2 still means exactly what it meant the day it was written.
CAMPAIGN_CONTENT_COVERAGE_FIDELITY_V2: dict[str, object] = {
    "verdict": "approximate",
    "result_tier": "simulator_three_act_pools_and_pair_boss",
    "environment_version": "sts2sim-campaign-fidelity-v2",
    "summary": (
        "Each stage draws the act the shipped campaign defines: stage 1 Overgrowth, stage 2 "
        "Hive, stage 3 Glory, with no cross-act draw. The final act's paired boss is dealt at "
        "act generation from that act's own boss pool minus the first, and is reached by "
        "travelling to a map row past the boss, so the two fights are one continuous resource "
        "problem with an observable middle. The boss relic the engine invented is gone. That "
        "closes G6, G1 and G3. G2 (per-act Ancients), G4 (per-act map shape) and G5 (upgrade "
        "odds and boss reward distributions) are still open, so this environment is closer to "
        "the game without yet being content-verified."
    ),
    "gates_closed": ["G6_no_boss_relic_reward", "G1_act_pools", "G3_second_boss_structure"],
    "gates_open": [
        "G2_ancients",
        "G4_map_shape",
        "G5_reward_and_upgrade_distribution",
    ],
    "stages": {
        "1": {
            "encounter_pools": "Overgrowth weak/normal/elite/boss",
            "event_pools": "Overgrowth events",
            "encounter_pools_match_real_game_act": True,
            "event_pools_match_real_game_act": True,
            "evidence": "Core/Run/RunConstants.cs:111-120",
        },
        "2": {
            "encounter_pools": "Hive weak/normal/elite/boss",
            "event_pools": "Underdocks events (G2/G4 territory, unchanged here)",
            "encounter_pools_match_real_game_act": True,
            "event_pools_match_real_game_act": False,
            "evidence": "Core/Run/RunConstants.cs:132-136 from Models.Acts/Hive.cs:84-103",
        },
        "3": {
            "encounter_pools": "Glory weak/normal/elite/boss, twice over at the boss row",
            "event_pools": "Underdocks events (G2/G4 territory, unchanged here)",
            "encounter_pools_match_real_game_act": True,
            "event_pools_match_real_game_act": False,
            "second_boss": {
                "dealt_at": "act generation, from GloryBossEncounters minus the first boss",
                "reached_by": "a map node one row past the boss, travelled to through the "
                              "normal map path with the run's own HP, potions, deck, relics "
                              "and gold",
                "evidence": "Core/Run/RunMapGenerator.cs GenerateSecondBoss()/OpenSecondBossRow() "
                            "for RunManager.cs:685-690 and StandardActMap.cs:88-91,231-234",
            },
            "evidence": "Core/Run/RunConstants.cs:138-142 from Models.Acts/Glory.cs:80-97",
        },
    },
    "rewards": {
        "boss": "gold + a potion roll + cards, no relic -- RewardsSet.cs:245-261",
        "elite": "the only combat clear that grants a relic",
        "final_act_boss": "the shipped build gives the final act's boss an empty RewardsSet "
                          "(RewardsSet.cs:65-74); the engine still hands out its standard "
                          "post-combat screen, which is a G5 residual, not a closure",
    },
    "not_modelled": [
        "a per-act Ancient event; only the run-start Neow choice exists (G2 open: the real "
        "Act 2 draws Orobas/Pael/Tezcatara and Act 3 Nonupeipe/Tanx/Vakuu)",
        "per-act map shape: all three stages use the same 16-row map with the boss at row 16, "
        "where the build defines 15/14/13 rooms with the boss at 16/15/14 (G4 open)",
        "per-act room counts and encounter-fill counts (real weak 3/2/2, normal 12/12/11)",
        "Hive and Glory event pools: acts 2 and 3 still draw the Underdocks event list",
        "card upgrade odds by act and the composition of boss rewards (G5 open)",
        "AscensionLevel: the double boss is unconditional here, which is A10 and nothing else",
        "any per-act or per-floor difficulty scaling",
        "TheArchitect: the real run ends in an EventRoom after the second boss "
        "(RunManager.cs:1207-1246); the engine ends it with a cleared flag",
        "ExoskeletonsWeak and DevotedSculptorWeak: the emulator has no separate weak variant, "
        "so those two weak slots draw the normal encounter",
    ],
    "real_progression": REAL_PROGRESSION,
    "result_tiers": RESULT_TIERS_AT_FIDELITY_V2,
    "may_be_quoted_as": [
        "a three-act campaign whose stages draw the shipped game's own acts",
        "a paired-boss final act with a real route decision between the two fights",
        "a regression control against approx-v1 artifacts",
    ],
    "must_not_be_quoted_as": [
        "content-verified three-act coverage",
        "live A10 win rate",
        "evidence that a strategy can clear the shipped game",
        "a per-act Ancient or a real Act 2/Act 3 event pool",
    ],
}

#: The campaign published by the fidelity line as of 2026-09-22: G6 + G1 + G3 + G2.
#: The campaign published as fidelity-v3 on 2026-09-22 (G6+G1+G3+G2), copied verbatim from the
#: live declaration of that date and pinned by digest below: v3 stamped artifacts walked a map
#: whose three acts shared one 16-row shape and an Underdocks event list, which v4 no longer
#: does, so a v3 artifact must keep saying what v3 was.
CAMPAIGN_CONTENT_COVERAGE_FIDELITY_V3: dict[str, object] = {
    "verdict": "approximate",
    "result_tier": "simulator_three_act_pools_ancients_and_pair_boss",
    "environment_version": "sts2sim-campaign-fidelity-v3",
    "summary": (
        "Each stage draws the act the shipped campaign defines (Overgrowth -> Hive -> Glory) and "
        "stands the run in front of that act's own ancient before its map is walkable, offering "
        "the three candidates the ancient's real option pools produce. The final act's paired "
        "boss is dealt at act generation from its own pool minus the first and reached by "
        "travelling to a row past the boss, and the boss relic the engine invented is gone. That "
        "closes G6, G1, G3 and G2. G4 (per-act map shape) and G5 (upgrade odds and boss reward "
        "distributions) are still open, so this environment is closer to the game without yet "
        "being content-verified."
    ),
    "gates_closed": [
        "G6_no_boss_relic_reward",
        "G1_act_pools",
        "G3_second_boss_structure",
        "G2_ancients",
    ],
    "gates_open": ["G4_map_shape", "G5_reward_and_upgrade_distribution"],
    "stages": {
        "1": {
            "encounter_pools": "Overgrowth weak/normal/elite/boss",
            "event_pools": "Overgrowth events",
            "ancient": "Neow, on the run-start screen",
            "encounter_pools_match_real_game_act": True,
            "event_pools_match_real_game_act": True,
            "ancient_matches_real_game_act": True,
            "evidence": "Core/Run/RunConstants.cs Overgrowth* tables; "
                        "Models.Acts/Overgrowth.cs:29-32",
        },
        "2": {
            "encounter_pools": "Hive weak/normal/elite/boss",
            "event_pools": "Underdocks events (G4/G5 territory, unchanged here)",
            "ancient": "Pael or Tezcatara, met on entering the act",
            "encounter_pools_match_real_game_act": True,
            "event_pools_match_real_game_act": False,
            "ancient_matches_real_game_act": "partial",
            "ancient_note": "Orobas is excluded: Hive.cs:110-115 hides it until its epoch is "
                            "revealed and the emulator has no unlock progression, so drawing it "
                            "would claim a progression the engine does not model",
            "evidence": "Core/Run/RunConstants.cs Hive* + Pael/Tezcatara option pools, from "
                        "Models.Acts/Hive.cs:27-33,110-116 and Events/{Pael,Tezcatara}.cs",
        },
        "3": {
            "encounter_pools": "Glory weak/normal/elite/boss, twice over at the boss row",
            "event_pools": "Underdocks events (G4/G5 territory, unchanged here)",
            "ancient": "Nonupeipe, Tanx or Vakuu, met on entering the act",
            "encounter_pools_match_real_game_act": True,
            "event_pools_match_real_game_act": False,
            "ancient_matches_real_game_act": True,
            "second_boss": {
                "dealt_at": "act generation, from GloryBossEncounters minus the first boss",
                "reached_by": "a map node one row past the boss, travelled to through the "
                              "normal map path with the run's own HP, potions, deck, relics "
                              "and gold",
                "evidence": "Core/Run/RunMapGenerator.cs GenerateSecondBoss()/OpenSecondBossRow() "
                            "for RunManager.cs:685-690 and StandardActMap.cs:88-91,231-234",
            },
            "evidence": "Core/Run/RunConstants.cs Glory* + Nonupeipe/Tanx/Vakuu option pools, "
                        "from Models.Acts/Glory.cs:26-32 and Events/{Nonupeipe,Tanx,Vakuu}.cs",
        },
    },
    "rewards": {
        "boss": "gold + a potion roll + cards, no relic -- RewardsSet.cs:245-261",
        "elite": "the only combat clear that grants a relic",
        "ancient": "three relic candidates drawn one per option pool, exactly as each "
                   "ancient's GenerateInitialOptions does; DistinguishedCape carries its "
                   "ThatDecreasesMaxHp(9m) price",
        "final_act_boss": "the shipped build gives the final act's boss an empty RewardsSet "
                          "(RewardsSet.cs:65-74); the engine still hands out its standard "
                          "post-combat screen, which is a G5 residual, not a closure",
    },
    "not_modelled": [
        "per-act map shape: all three stages use the same 16-row map with the boss at row 16, "
        "where the build defines 15/14/13 rooms with the boss at 16/15/14 (G4 open)",
        "per-act room counts and encounter-fill counts (real weak 3/2/2, normal 12/12/11)",
        "Hive and Glory event pools: acts 2 and 3 still draw the Underdocks event list",
        "card upgrade odds by act and the composition of boss rewards (G5 open)",
        "Orobas as an act 2 ancient: hidden behind OrobasEpoch in the build, and the emulator "
        "models no unlock state, so it is excluded rather than dealt unconditionally",
        "the deck-conditional ancient options that need counts this engine does not track: "
        "Pael's claw/tooth (Goopy-enchanted, removable cards), Nonupeipe's BeautifulBracelet "
        "and Tanx's TriBoomerang (Swift-/Instinct-enchanted cards). Their pools without the "
        "extra are offered, so the distribution of those three ancients is narrower than the "
        "game's, not merely different",
        "SeaGlass's other-character binding: Orobas offers it as itself because the emulator "
        "has one playable character",
        "Hook.ShouldAllowAncient: when the build's hook disallows an ancient the screen "
        "collapses to a single PROCEED, which the emulator never does",
        "an out-of-combat death: an ancient's max-health price can take a real run to zero, "
        "and this engine floors it at 1 health instead of ending the run",
        "AscensionLevel: the double boss is unconditional here, which is A10 and nothing else",
        "any per-act or per-floor difficulty scaling",
        "TheArchitect: the real run ends in an EventRoom after the second boss "
        "(RunManager.cs:1207-1246); the engine ends it with a cleared flag",
        "ExoskeletonsWeak and DevotedSculptorWeak: the emulator has no separate weak variant, "
        "so those two weak slots draw the normal encounter",
    ],
    "real_progression": REAL_PROGRESSION,
    "result_tiers": {
        'simulator_single_act': 'one act per seed; every pre-2026-09-20 artifact',
        'simulator_three_act_approx': 'three stages, Act 1 content reused for stages 2 and 3',
        'simulator_three_act_content_verified': 'requires Hive and Glory pools, a per-act Ancient, and a boss-to-boss exit; no artifact in this repository is at this tier yet',
        'live_full_run': 'the real client, victory screen observed; not achieved',
        'simulator_three_act_pools_and_pair_boss': "each stage draws the act the shipped campaign defines (Overgrowth -> Hive -> Glory), the final act's paired boss is a map row past the first, and no boss relic is invented; per-act Ancients, per-act map shape and the reward/upgrade distributions are still not real, so this tier is not content-verified",
        'simulator_three_act_pools_ancients_and_pair_boss': "G1+G2+G3+G6 closed: per-act encounter pools, the act's own ancient met on entry with its real option pools, a travelled-to second boss, and no invented boss relic. Per-act map shape (G4) and the upgrade/reward distributions (G5) are still not real, so this tier is still not content-verified",
    },
    "may_be_quoted_as": [
        "a three-act campaign whose stages draw the shipped game's own acts",
        "a paired-boss final act with a real route decision between the two fights",
        "each act's own ancient, with its real relic candidates, on entry",
        "a regression control against approx-v1 and fidelity-v2 artifacts",
    ],
    "must_not_be_quoted_as": [
        "content-verified three-act coverage",
        "live A10 win rate",
        "evidence that a strategy can clear the shipped game",
        "the real Act 2/Act 3 event pools or map shape",
    ],
}

#: Frozen: all six gates, per-act maps and act-scaled rewards, with the engine still
#: carrying a subsystem that replayed one recorded run.  Its digest is pinned below, so
#: this text and that digest may not move together with the live declaration.
CAMPAIGN_CONTENT_COVERAGE_FIDELITY_V4: dict[str, object] = {
    "verdict": "approximate",
    "result_tier": "simulator_three_act_campaign_shape_and_rewards",
    "environment_version": "sts2sim-campaign-fidelity-v4",
    "summary": (
        "Each act generates its own map: 15/14/13 rooms with the boss one row below the last "
        "room row (16/15/14), its own weak/normal fill counts, 5 elites and 3 shops, its own "
        "rest and unknown queues, and its own event pool (the act's events plus the shared "
        "eighteen). The boss floors that implies -- 17/33/48 -- are the floors the real client "
        "was measured at. Card upgrades roll at the act's own odds (0% / 12.5% / 25% at A10, "
        "never for Rare cards, one draw per offered card taken before the check), and the final "
        "act's boss deals an empty RewardsSet exactly as the build does, resolving its node with "
        "no screen at all. That closes G4 and G5, so all six hard gates pass. The tier stays "
        "below content-verified: no ascension model, no unknown-node re-roll, no TheArchitect "
        "exit, two weak encounter variants the build does not have here, and placement that can "
        "fall short of the queued room count in the shorter acts."
    ),
    "gates_closed": [
        "G6_no_boss_relic_reward",
        "G1_act_pools",
        "G3_second_boss_structure",
        "G2_ancients",
        "G4_map_shape",
        "G5_reward_and_upgrade_distribution",
    ],
    "gates_open": [],
    "stages": {
        "1": {
            "encounter_pools": "Overgrowth weak/normal/elite/boss",
            "event_pools": "Overgrowth events + the shared 18 (four of which Act 1 was "
                           "missing before)",
            "ancient": "Neow, on the run-start screen",
            "map_shape": "15 rooms, boss on row 16, treasure row 9, forced final rest row 15",
            "room_queues": "3 weak, 12 normal, 5 elite, 3 shop, N(7,1)[6,7] rests, "
                           "10-14 unknown",
            "encounter_pools_match_real_game_act": True,
            "event_pools_match_real_game_act": True,
            "ancient_matches_real_game_act": True,
            "evidence": "Core/Run/RunConstants.cs Overgrowth* tables; "
                        "Models.Acts/Overgrowth.cs:29-32",
        },
        "2": {
            "encounter_pools": "Hive weak/normal/elite/boss",
            "event_pools": "Hive events + the shared 18, minus the epoch-gated "
                           "ColorfulPhilosophers",
            "ancient": "Pael or Tezcatara, met on entering the act",
            "map_shape": "14 rooms, boss on row 15, treasure row 8, forced final rest row 14",
            "room_queues": "2 weak, 12 normal, 5 elite, 3 shop, N(6,1)[6,7] rests, 9-13 unknown",
            "encounter_pools_match_real_game_act": True,
            "event_pools_match_real_game_act": True,
            "ancient_matches_real_game_act": "partial",
            "ancient_note": "Orobas is excluded: Hive.cs:110-115 hides it until its epoch is "
                            "revealed and the emulator has no unlock progression, so drawing it "
                            "would claim a progression the engine does not model",
            "evidence": "Core/Run/RunConstants.cs Hive* + Pael/Tezcatara option pools, from "
                        "Models.Acts/Hive.cs:27-33,110-116 and Events/{Pael,Tezcatara}.cs",
        },
        "3": {
            "encounter_pools": "Glory weak/normal/elite/boss, twice over at the boss row",
            "event_pools": "Glory events + the shared 18, minus the epoch-gated Reflections",
            "ancient": "Nonupeipe, Tanx or Vakuu, met on entering the act",
            "map_shape": "13 rooms, boss on row 14, paired boss on row 15, treasure row 7, "
                          "forced final rest row 13",
            "room_queues": "2 weak, 11 normal, 5 elite, 3 shop, {5,6} rests, 9-13 unknown",
            "encounter_pools_match_real_game_act": True,
            "event_pools_match_real_game_act": True,
            "ancient_matches_real_game_act": True,
            "second_boss": {
                "dealt_at": "act generation, from GloryBossEncounters minus the first boss",
                "reached_by": "a map node one row past the boss, travelled to through the "
                              "normal map path with the run's own HP, potions, deck, relics "
                              "and gold",
                "evidence": "Core/Run/RunMapGenerator.cs GenerateSecondBoss()/OpenSecondBossRow() "
                            "for RunManager.cs:685-690 and StandardActMap.cs:88-91,231-234",
            },
            "evidence": "Core/Run/RunConstants.cs Glory* + Nonupeipe/Tanx/Vakuu option pools, "
                        "from Models.Acts/Glory.cs:26-32 and Events/{Nonupeipe,Tanx,Vakuu}.cs",
        },
    },
    "rewards": {
        "boss": "gold + a potion roll + cards, no relic -- RewardsSet.cs:245-261",
        "elite": "the only combat clear that grants a relic",
        "ancient": "three relic candidates drawn one per option pool, exactly as each "
                   "ancient's GenerateInitialOptions does; DistinguishedCape carries its "
                   "ThatDecreasesMaxHp(9m) price",
        "final_act_boss": "no rewards at all, and not even the potion draw: the engine returns "
                          "before generating anything, as RewardsSet.cs:68-74 does, and the "
                          "node resolves on the win itself",
        "gold": "Monster 7-15, Elite 26-33, Boss 75 -- EncounterModel.cs:44-80's 10-20 / "
                "35-45 / 100 times Poverty's 0.75 (AscensionHelper.cs:12), which is what A10 "
                "means; the boss used to pay the un-scaled 100",
        "upgrade_odds": "0% / 12.5% / 25% by act at A10 (CardFactory.cs:23-24,395-396), drawn "
                        "per offered card before the check is made (:389), never for Rare cards "
                        "(:393); shop cards pass a base chance so low they can never upgrade "
                        "(:81,101)",
        "rarity": "regular .0149 rare / .37 uncommon, elite .05 / .4, boss all-rare "
                  "(CardRarityOdds.cs:111-151) with the drift offset the build carries",
    },
    "not_modelled": [
        "placement is not always the queue: the prune/repair pass tops up only from modifiable "
        "Monster nodes, so over 24 seeds a Glory map came out with 4 elites and a Hive map with "
        "5 rests. The shipped generator runs the same pass; the equality is measured here rather "
        "than assumed from the build",
        "the unknown node's re-roll on entry (RunManager.cs:935 with UnknownMapPointOdds): an "
        "Unknown that becomes a monster, treasure, shop or elite is not modelled, so the event "
        "distribution counted here is narrower than the game's",
        "the shipped generator's map shape node for node: seven paths, crossover rejection, "
        "three assignment passes and the column shift are ported, but G4 is judged on rows, "
        "room counts and the measured boss floors, not on comparing maps to the client's",
        "Orobas as an act 2 ancient: hidden behind OrobasEpoch in the build, and the emulator "
        "models no unlock state, so it is excluded rather than dealt unconditionally",
        "the deck-conditional ancient options that need counts this engine does not track: "
        "Pael's claw/tooth (Goopy-enchanted, removable cards), Nonupeipe's BeautifulBracelet "
        "and Tanx's TriBoomerang (Swift-/Instinct-enchanted cards). Their pools without the "
        "extra are offered, so the distribution of those three ancients is narrower than the "
        "game's, not merely different",
        "SeaGlass's other-character binding: Orobas offers it as itself because the emulator "
        "has one playable character",
        "Hook.ShouldAllowAncient: when the build's hook disallows an ancient the screen "
        "collapses to a single PROCEED, which the emulator never does",
        "an out-of-combat death: an ancient's max-health price can take a real run to zero, "
        "and this engine floors it at 1 health instead of ending the run",
        "AscensionLevel: the double boss is unconditional here, which is A10 and nothing else",
        "any per-act or per-floor difficulty scaling, and AscensionLevel.SwarmingElites (A2), "
        "which queues 8 elites instead of 5 (MapPointTypeCounts.cs:15-19)",
        "TheArchitect: the real run ends in an EventRoom after the second boss "
        "(RunManager.cs:1207-1246); the engine ends it with a cleared flag",
        "ExoskeletonsWeak and DevotedSculptorWeak: the emulator has no separate weak variant, "
        "so those two weak slots draw the normal encounter",
    ],
    "real_progression": REAL_PROGRESSION,
    "result_tiers": RESULT_TIERS,
    "may_be_quoted_as": [
        "a three-act campaign whose stages draw the shipped game's own acts",
        "a paired-boss final act with a real route decision between the two fights",
        "each act's own ancient, with its real relic candidates, on entry",
        "a regression control against approx-v1, fidelity-v2 and fidelity-v3 artifacts",
    ],
    "must_not_be_quoted_as": [
        "content-verified three-act coverage",
        "live A10 win rate",
        "evidence that a strategy can clear the shipped game",
        "the shipped generator's per-node map shapes",
        "a content-verified three-act campaign -- the tier above this one is where that claim "
        "would live, and it is still unattained",
    ],
}


V5_SUMMARY_LEAD = (
    "v4's content with one thing taken out of the engine rather than added to it. The "
    "run step used to carry a retained-trace subsystem that replayed a single recorded "
    "playthrough: it matched live state against that run's exact floor, HP and gold and "
    "then overwrote combat results, rewards, map routing and event choice. 54 of those "
    "branches guarded nothing, because the environment passes str(int_seed) and a branch "
    "comparing StringSeed to a golden run code cannot fire for a training seed; one of "
    "them settled the Act-1 floor-6 Punch Construct as a won combat, at a fixed 10 HP and "
    "132 gold, for any targeted play of hand slot 4, which is a reward an agent can look "
    "up rather than earn. Removing the lot moved 4 of 1500 campaign episodes' final floor "
    "and no wins, so every v4 strength number stays usable and is now produced by rules "
    "that apply to every seed. "
)

#: Derived from the frozen v4 declaration rather than copied, so a correction that belongs
#: to both is made once; v4's pinned digest is what stops v4's text drifting underneath.
CAMPAIGN_CONTENT_COVERAGE: dict[str, object] = {
    **copy.deepcopy(CAMPAIGN_CONTENT_COVERAGE_FIDELITY_V4),
    "environment_version": CAMPAIGN_ENVIRONMENT_VERSION,
    "summary": V5_SUMMARY_LEAD + str(
        CAMPAIGN_CONTENT_COVERAGE_FIDELITY_V4["summary"]
    ),
    "may_be_quoted_as": [
        "a campaign whose outcomes come from its own rules: no branch overwrites a combat "
        "result, a reward, a route or an event because the state resembles a recorded run",
        *CAMPAIGN_CONTENT_COVERAGE_FIDELITY_V4["may_be_quoted_as"],
    ],
}


class EnvironmentVersionError(RuntimeError):
    """A version name is being reused for content it never declared."""


def _canonical_digest(coverage: dict[str, object]) -> str:
    canonical = json.dumps(
        coverage, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


#: Frozen environments, keyed by the version string that will appear in an
#: artifact.  The digest is pinned as a literal on purpose: recomputing it from
#: the dictionary would let a content edit "verify" itself, which is exactly the
#: failure being prevented -- an approximate result must not be able to become a
#: fidelity result without saying so in the version.
#: Pinned from the declaration below, the same way approx-v1's is: editing what v2
#: contains without bumping the version trips the digest, not a comment.
_FIDELITY_V2_DIGEST = "512b6157e70bd501f88c0d8862d8bcd83873f8733d5bdae629ffadda66aa80c5"

#: Pinned the same way, from the declaration below.
_FIDELITY_V3_DIGEST = "6e84c9e77499fd8253be9b97fd8d893c4a4602b6853779b315883159bd0a47d4"

#: Pinned from the declaration below, which is the live one until the next version bumps it.
_FIDELITY_V4_DIGEST = "d7c9dece05bdde79d626fe05f7890bf10597a91cc95e4b7dafecb8abda072fa3"

#: Pinned from the live declaration below, which is v5: v4's content with the engine's
#: replayed-trace overrides removed.
_FIDELITY_V5_DIGEST = "7eaf8e47e390c3678db459bec697c045b46490c6149be5be361a93b594f8d918"

FROZEN_ENVIRONMENT_VERSIONS: dict[str, dict[str, object]] = {
    "sts2sim-campaign-approx-v1": {
        "frozen_at_utc": "2026-09-21T14:30:00+00:00",
        "content_sha256": "13cbd87703407fe6f7c7c6621638653313c3dd015d26c4ad311cb0bce5e4933f",
        "verdict": "approximate",
        "result_tier": "simulator_three_act_approx",
        "gates_closed": [],
        "meaning": (
            "Stage 1 is Overgrowth; stages 2 and 3 draw the Underdocks pools. Every "
            "artifact carrying this name -- smoke, checkpoints, metrics, rolled "
            "windows -- keeps that meaning permanently, including after G1-G6 land."
        ),
        "checkpoint_rule": (
            "a checkpoint trained under this version may not be continued into a "
            "differently-versioned environment without restating its provenance"
        ),
    },

    "sts2sim-campaign-fidelity-v2": {
        "frozen_at_utc": "2026-09-22T06:30:00+00:00",
        "content_sha256": _FIDELITY_V2_DIGEST,
        "verdict": "approximate",
        "result_tier": "simulator_three_act_pools_and_pair_boss",
        "gates_closed": ["G6_no_boss_relic_reward", "G1_act_pools", "G3_second_boss_structure"],
        "meaning": (
            "G6, G1 and G3 are closed and nothing else is: Overgrowth -> Hive -> Glory "
            "encounter pools, a dealt-and-travelled-to second boss, no invented boss relic. "
            "No per-act Ancient, no per-act map shape, no verified reward distributions, and "
            "acts 2/3 still draw the Underdocks event list."
        ),
        "checkpoint_rule": (
            "an approx-v1 checkpoint may not be resumed here; the boss-reward screen its "
            "policy was trained on no longer exists in this environment"
        ),
    },
    "sts2sim-campaign-fidelity-v3": {
        "frozen_at_utc": "2026-09-22T06:30:00+00:00",
        "content_sha256": _FIDELITY_V3_DIGEST,
        "verdict": "approximate",
        "result_tier": "simulator_three_act_pools_ancients_and_pair_boss",
        "gates_closed": [
            "G6_no_boss_relic_reward",
            "G1_act_pools",
            "G3_second_boss_structure",
            "G2_ancients",
        ],
        "meaning": (
            "Four of six hard gates: acts draw their own encounter pools, each act's own "
            "ancient is met on entry with its real relic candidates, the paired boss is a "
            "travelled-to map row, and no boss relic is invented. G4 (map shape, event "
            "pools) and G5 (upgrade odds, boss reward composition) are open, and several "
            "deck-conditional ancient options are not dealt at all."
        ),
        "checkpoint_rule": (
            "a fidelity-v2 checkpoint may be continued only with its own metrics kept "
            "separate: v2 never offered an act ancient, so an act-2/3 resource policy "
            "trained there has not seen a decision that exists here"
        ),
    },
    "sts2sim-campaign-fidelity-v4": {
        "frozen_at_utc": "2026-09-22T09:58:00+00:00",
        "content_sha256": _FIDELITY_V4_DIGEST,
        "verdict": "approximate",
        "result_tier": "simulator_three_act_campaign_shape_and_rewards",
        "gates_closed": [
            "G6_no_boss_relic_reward",
            "G1_act_pools",
            "G3_second_boss_structure",
            "G2_ancients",
            "G4_map_shape",
            "G5_reward_and_upgrade_distribution",
        ],
        "meaning": (
            "All six hard gates close: each act also generates its own map depth, room queues "
            "and event pool, rewards and upgrade odds follow the act and the room type, and the "
            "boss floors the geometry implies are the ones the real client measured (17/33/48). "
            "It is still not the tier above: no ascension model, no unknown-node re-roll, no "
            "TheArchitect exit, two weak variants absent, and placement can fall short of the "
            "queue in the shorter acts."
        ),
        "checkpoint_rule": (
            "a fidelity-v3 checkpoint enters a different map here -- acts 2 and 3 are one and two "
            "rows shallower and their event pools are their own -- so its act-2/3 state visitation "
            "may not be read as coverage of this environment; v3 rewards were also richer, because "
            "the final-act boss dealt a screen this build does not"
        ),
    },
    "sts2sim-campaign-fidelity-v5": {
        "frozen_at_utc": "2026-09-23T02:20:00+00:00",
        "content_sha256": _FIDELITY_V5_DIGEST,
        "verdict": "approximate",
        "result_tier": "simulator_three_act_campaign_shape_and_rewards",
        "gates_closed": [
            "G6_no_boss_relic_reward",
            "G1_act_pools",
            "G3_second_boss_structure",
            "G2_ancients",
            "G4_map_shape",
            "G5_reward_and_upgrade_distribution",
        ],
        "meaning": (
            "Nothing new was added: this is fidelity-v4 with the engine's retained-trace "
            "subsystem deleted, so the same maps, pools, ancients, rewards and boss floors are "
            "now produced only by rules that apply to every seed. What it removes is a confound, "
            "not a capability -- no combat result, reward, route or event is chosen because the "
            "live state resembles one recorded run."
        ),
        "checkpoint_rule": (
            "a fidelity-v4 checkpoint may be resumed here, and this is the one version bump where "
            "that is defensible rather than merely convenient: over the same 1500 campaign seeds "
            "the removal moved 3 of 1500 episodes' final floor and no win or clear flag, so a v4 policy arrives in an "
            "environment that behaves as it did. Its v4 metrics must still be reported as v4 -- "
            "measured equivalence on one population is not identity, and the branches removed were "
            "reachable in the tail of depth this population almost never walks into"
        ),
    },
}

#: The declaration each frozen version was stamped with.  ``assert_content_declaration``
#: takes the coverage as an argument so a caller can prove an artifact's label against
#: the text it was labelled with; this is where that text lives once the live
#: declaration has moved on.
FROZEN_ENVIRONMENT_DECLARATIONS: dict[str, dict[str, object]] = {
    "sts2sim-campaign-approx-v1": CAMPAIGN_CONTENT_COVERAGE_APPROX_V1,
    "sts2sim-campaign-fidelity-v2": CAMPAIGN_CONTENT_COVERAGE_FIDELITY_V2,
    "sts2sim-campaign-fidelity-v3": CAMPAIGN_CONTENT_COVERAGE_FIDELITY_V3,
    "sts2sim-campaign-fidelity-v4": CAMPAIGN_CONTENT_COVERAGE_FIDELITY_V4,
    "sts2sim-campaign-fidelity-v5": CAMPAIGN_CONTENT_COVERAGE,
}


def assert_content_declaration(
    version: str, coverage: dict[str, object]
) -> str:
    """Return the coverage digest, refusing a frozen name whose content moved.

    Called by anything that stamps an artifact, so the mistake this catches is
    the expensive one: editing what the campaign contains while leaving the old
    version string on the file, after which nothing on disk can tell the two
    environments apart.
    """
    frozen = FROZEN_ENVIRONMENT_VERSIONS.get(version)
    digest = _canonical_digest(coverage)
    if frozen is None:
        return digest
    if str(coverage.get("verdict")) != str(frozen["verdict"]):
        raise EnvironmentVersionError(
            f"{version} is frozen with verdict {frozen['verdict']!r}; "
            f"a declaration of {coverage.get('verdict')!r} is a different "
            "environment and must carry a new environment_version"
        )
    if digest != str(frozen["content_sha256"]):
        raise EnvironmentVersionError(
            f"{version} is frozen at content_sha256 "
            f"{frozen['content_sha256'][:16]}... but the declaration now reads "
            f"{digest[:16]}... -- publish a new environment_version instead of "
            "rewriting a frozen one"
        )
    return digest


def assert_single_environment(versions: Iterable[str | None]) -> str:
    """One version per comparison, or a named refusal.

    Rollups, gate re-judgements and merge steps all take a list of artifacts, and
    a set of two versions in that list is the silent event that makes an
    approximate result and a fidelity result into one number.
    """
    named = {version if version is not None else "unlabelled" for version in versions}
    if not named:
        raise EnvironmentVersionError("no artifacts to compare")
    if len(named) > 1:
        raise EnvironmentVersionError(
            "refusing to combine artifacts across environments: "
            + ", ".join(sorted(named))
        )
    return next(iter(named))

