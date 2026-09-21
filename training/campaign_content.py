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

import hashlib
import json
from collections.abc import Iterable

#: Bump when the campaign's *content* changes, so pre-change artifacts stay separable.
CAMPAIGN_ENVIRONMENT_VERSION = "sts2sim-campaign-fidelity-v2"

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

#: The result tiers that must never be quoted as one number.
RESULT_TIERS = {
    **RESULT_TIERS_AT_APPROX_V1,
    "simulator_three_act_pools_and_pair_boss": (
        "each stage draws the act the shipped campaign defines (Overgrowth -> Hive -> Glory), "
        "the final act's paired boss is a map row past the first, and no boss relic is "
        "invented; per-act Ancients, per-act map shape and the reward/upgrade "
        "distributions are still not real, so this tier is not content-verified"
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

#: The campaign published by the fidelity line: G6 + G1 + G3, nothing else.
CAMPAIGN_CONTENT_COVERAGE: dict[str, object] = {
    "verdict": "approximate",
    "result_tier": "simulator_three_act_pools_and_pair_boss",
    "environment_version": CAMPAIGN_ENVIRONMENT_VERSION,
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
    "result_tiers": RESULT_TIERS,
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
        "frozen_at_utc": "2026-09-22T00:40:00+00:00",
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
}

#: The declaration each frozen version was stamped with.  ``assert_content_declaration``
#: takes the coverage as an argument so a caller can prove an artifact's label against
#: the text it was labelled with; this is where that text lives once the live
#: declaration has moved on.
FROZEN_ENVIRONMENT_DECLARATIONS: dict[str, dict[str, object]] = {
    "sts2sim-campaign-approx-v1": CAMPAIGN_CONTENT_COVERAGE_APPROX_V1,
    "sts2sim-campaign-fidelity-v2": CAMPAIGN_CONTENT_COVERAGE,
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

