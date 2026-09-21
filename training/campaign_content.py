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

import hashlib
import json
from collections.abc import Iterable

#: Bump when the campaign's *content* changes, so pre-change artifacts stay separable.
CAMPAIGN_ENVIRONMENT_VERSION = "sts2sim-campaign-approx-v1"

#: The name the Act 2 / Act 3 fidelity work must publish under.  It is reserved
#: here so the first fidelity change lands on a new version rather than on this
#: one -- approx-v1 is frozen below, and results from the two must never be
#: averaged, re-judged, or continued from each other's checkpoints.
FIDELITY_ENVIRONMENT_VERSION_BASE = "sts2sim-campaign-fidelity"

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
FROZEN_ENVIRONMENT_VERSIONS: dict[str, dict[str, object]] = {
    "sts2sim-campaign-approx-v1": {
        "frozen_at_utc": "2026-09-21T14:30:00+00:00",
        "content_sha256": "13cbd87703407fe6f7c7c6621638653313c3dd015d26c4ad311cb0bce5e4933f",
        "verdict": "approximate",
        "result_tier": "simulator_three_act_approx",
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

