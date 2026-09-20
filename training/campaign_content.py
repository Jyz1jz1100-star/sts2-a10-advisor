"""What the emulator's campaign mode actually contains, in machine-readable form.

Every simulator number now carries this declaration, because the three-act campaign
is not the shipped game's three acts.  It walks real Act 1 content twice more: the
engine has no Hive or Glory pool, and no per-act Ancient.  Recording that next to
the number is what keeps an approximate environment from being read as a strength
result, and keeps the four tiers below from being averaged together.

The evidence column points at the locked build's own decompiled sources; the
emulator side points at the pools it actually reads.
"""
from __future__ import annotations

#: Bump when the campaign's *content* changes, so pre-change artifacts stay separable.
CAMPAIGN_ENVIRONMENT_VERSION = "sts2sim-campaign-approx-v1"

#: The four result tiers that must never be quoted as one number.
RESULT_TIERS = {
    "simulator_single_act": "one act per seed; every pre-2026-09-20 artifact",
    "simulator_three_act_approx": "three stages, Act 1 content reused for stages 2 and 3",
    "simulator_three_act_content_verified": (
        "requires Hive and Glory pools, a per-act Ancient, and a boss-to-boss exit; "
        "no artifact in this repository is at this tier yet"
    ),
    "live_full_run": "the real client, victory screen observed; not achieved",
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

CAMPAIGN_CONTENT_COVERAGE: dict[str, object] = {
    "verdict": "approximate",
    "result_tier": "simulator_three_act_approx",
    "environment_version": CAMPAIGN_ENVIRONMENT_VERSION,
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
    "result_tiers": RESULT_TIERS,
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
