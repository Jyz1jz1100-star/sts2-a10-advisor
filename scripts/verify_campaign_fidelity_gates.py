"""Measure the simulator's campaign against the locked build, gate by gate.

Six gates (G1-G6) decide whether a policy trained here can transfer.  This tool
does not read the fidelity document and agree with it; it reads the engine's own
C# tables, walks the environment with a reproducible checkpoint, and says for each
gate what the real build does, what this build does, and what still differs.

    <training python> scripts/verify_campaign_fidelity_gates.py --seeds 6 --out runtime/fidelity_gates.json

The two gates that describe a transition (G3, G6) additionally run the engine's own
scenario tests, because a source regex can show that code was written and not that the
state machine walks it.

Exit code is 0 only when every hard gate passes, i.e. only when the environment
may be called content-verified.  A gate that could not be measured reports as
``not_measured`` and counts as not passed -- an unavailable probe must never read
as a pass.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EMULATOR = ROOT.parent / "third_party" / "slay-the-spire-2-emulator-main"
sys.path.insert(0, str(ROOT))
# The gym package lives in the emulator's own src tree, not in any wheel: without
# this the dynamic sample degrades to "import failed" and the pool gate is judged
# on static tables alone.
sys.path.insert(0, str(EMULATOR / "src"))

RUN_CONSTANTS = EMULATOR / "src" / "Sts2Emulator" / "Core" / "Run" / "RunConstants.cs"
MAP_GENERATOR = EMULATOR / "src" / "Sts2Emulator" / "Core" / "Run" / "RunMapGenerator.cs"
REWARD_GENERATOR = EMULATOR / "src" / "Sts2Emulator" / "Core" / "Run" / "RunRewardGenerator.cs"
RUN_ENGINE = EMULATOR / "src" / "Sts2Emulator" / "Core" / "Run" / "RunEngine.cs"
ENCOUNTER_TABLE = EMULATOR / "src" / "sts2_gym" / "env.py"
ANCIENT_CHOICES = EMULATOR / "src" / "Sts2Emulator" / "Core" / "Run" / "RunAncientChoices.cs"

#: Encounter ids the emulator actually assigns, resolved by name so the numbers
#: are never copied by hand.
def emulator_encounter_ids() -> dict[str, int]:
    text = ENCOUNTER_TABLE.read_text(encoding="utf-8", errors="replace")
    block = text.split("ENCOUNTER_NAMES = {", 1)[1].split("}", 1)[0]
    return {name: int(encounter_id) for encounter_id, name in
            re.findall(r"(\d+):\s*\"([^\"]+)\"", block)}

#: The locked build's per-act pools, transcribed from the decompiled sources and
#: expressed in the emulator's own encounter names (Models.Acts/*.cs lines are in
#: ``ACT_SOURCES``).  Where the emulator has no separate weak variant, the same id
#: appears in both lists and the harness reports that as a residual difference.
REAL_POOLS: dict[str, dict[str, list[str]]] = {
    "act_1_overgrowth": {
        "source": "decompiled/MegaCrit.Sts2.Core.Models.Acts/Overgrowth.cs:84-105",
        "weak": ["fuzzy-wurm-crawler", "nibbit", "shrinker-beetle", "slimes"],
        "normal": ["cubex-construct", "slime-and-flyconid", "fogmog", "inklets",
                   "mawler", "nibbits", "shrinker-and-fuzzy", "ruby-raiders",
                   "large-slimes", "slithering-strangler", "jaxfruit-and-flyconid",
                   "vine-shambler"],
        "elite": ["byrdonis", "phrog-parasite", "bygone-effigy"],
        "boss": ["vantom", "ceremonial-beast", "kin"],
    },
    "act_2_hive": {
        "source": "decompiled/MegaCrit.Sts2.Core.Models.Acts/Hive.cs:84-103",
        "weak": ["bowlbugs-weak", "exoskeletons", "thieving-hopper", "tunneler"],
        "normal": ["bowlbugs", "chompers", "exoskeletons", "hunter-killer",
                   "louse-progenitor", "mytes", "ovicopter", "slumbering-beetle",
                   "spiny-toad", "obscura"],
        "elite": ["decimillipede", "entomancer", "infested-prisms"],
        "boss": ["kaiser-crab", "knowledge-demon", "insatiable"],
    },
    "act_3_glory": {
        "source": "decompiled/MegaCrit.Sts2.Core.Models.Acts/Glory.cs:80-97",
        "weak": ["devoted-sculptor", "scrolls-weak", "turret-operator"],
        "normal": ["axebot", "construct-menagerie", "fabricator", "frog-knight",
                   "globe-head", "owl-magistrate", "scrolls", "slimed-berserker",
                   "lost-and-forgotten"],
        "elite": ["knights", "mecha-knight", "soul-nexus"],
        "boss": ["aeonglass", "queen", "test-subject"],
    },
}

#: Per-act map shape as the build computes it, with the rule that produces it.
REAL_SHAPE = {
    "source": "ActModel.cs:309-343, StandardActMap.cs:81-91,154-159 / "
              "Hive.cs:54,56,120-125 / Glory.cs:50,52,111-112",
    "act_1": {"rooms": 15, "boss_row": 16, "rests": "NextGaussianInt(7,1,6,7)",
              "elites_on_map": 5, "shops": 3},
    "act_2": {"rooms": 14, "boss_row": 15, "rests": "NextGaussianInt(6,1,6,7)",
              "elites_on_map": 5, "shops": 3},
    "act_3": {"rooms": 13, "boss_row": 14, "rests": "NextInt(5, 7) -> {5, 6}",
              "elites_on_map": 5, "shops": 3},
    "second_boss_row": "act_3 only: StandardActMap.cs:88-91 puts SecondBossMapPoint "
                       "at GetRowCount()+1, and RunManager.cs:685-690 fills it only "
                       "at AscensionLevel.DoubleBoss",
}

REAL_ANCIENTS = {
    "source": "Overgrowth.cs:29-32,110-118 / Hive.cs:27-35,108-116 / "
              "Glory.cs:26-34,102-105 / ActModel.cs:345-348 / ModelDb.cs:152-153",
    "act_1": ["Neow"],
    "act_2": ["Orobas", "Pael", "Tezcatara"],
    "act_3": ["Nonupeipe", "Tanx", "Vakuu"],
    "shared": "Darv may be drawn by acts 2 and 3 only (RunManager.cs:669-676 skips "
              "the first act), never by act 1",
}

UPGRADE_ODDS_RULE = (
    "CardFactory.cs:387-409: non-Rare cards add CurrentActIndex * "
    "UpgradedCardOddScaling (0.25, or 0.125 at ascension Scarcity) to the base "
    "chance, so 0% / 25% / 50% by act; Rare keeps the base chance"
)
BOSS_REWARD_RULE = (
    "RewardsSet.cs:245-261: boss rewards are gold + a potion roll + three rare "
    "cards and no relic; RewardsSet.cs:65-74: the final act's boss gives an empty "
    "RewardsSet; BossRelicReward appears nowhere in the build"
)


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def pool_tables() -> dict[str, list[int]]:
    text = _text(RUN_CONSTANTS)
    tables: dict[str, list[int]] = {}
    for match in re.finditer(
        r"ReadOnlySpan<int>\s+(\w+Encounters)\s*(?:=>|=\>)\s*(?:\[[^\]]*\]|"
        r"\s*\n?\s*\[[^\]]*\])", text
    ):
        tables[match.group(1)] = [
            int(number) for number in re.findall(r"\d+", match.group(0).split("[", 1)[1])
        ]
    return tables


def measure_gates(
    ids: dict[str, int],
    tables: dict[str, list[int]],
    scenario: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Static comparison against the engine's own tables (G1, G2, G4, G5, G6).

    ``scenario`` carries the engine-side structural evidence produced by
    :func:`engine_scenario_tests`; the two gates that describe a *transition* (G3, G6)
    refuse to close on source text alone, because a regex can only show that code was
    written, not that the state machine walks it.
    """
    gates: dict[str, Any] = {}

    def resolve(act: str) -> dict[str, list[int]]:
        pool = REAL_POOLS[act]
        return {
            tier: [ids[name] for name in pool[tier] if name in ids]
            for tier in ("weak", "normal", "elite", "boss")
        }

    missing_names = sorted({
        name
        for act in REAL_POOLS
        for tier in ("weak", "normal", "elite", "boss")
        for name in REAL_POOLS[act][tier]
        if name not in ids
    })

    hive = resolve("act_2_hive")
    glory = resolve("act_3_glory")
    referenced = {
        encounter_id
        for table in tables.values()
        for encounter_id in table
    }
    hive_drawable = [i for i in hive["normal"] + hive["elite"] + hive["boss"]
                     if i in referenced]
    glory_drawable = [i for i in glory["normal"] + glory["elite"] + glory["boss"]
                      if i in referenced]
    gates["G1_act_pools"] = {
        "engine_tables": {k: v for k, v in sorted(tables.items())},
        "stage_selector": "RunMapGenerator.cs `hive ? Hive : glory ? Glory : underdocks ? "
                          "Underdocks : Overgrowth` per tier, with hive/glory from "
                          "RunConstants.IsHiveAct/IsGloryAct (campaign act 2 / act 3)",
        "hive_ids_referenced_by_any_pool": hive_drawable,
        "glory_ids_referenced_by_any_pool": glory_drawable,
        "ids_the_emulator_cannot_name": missing_names,
        # Naming a table is not drawing from it: the gate is decided by what a campaign
        # walk actually meets per act, so the static view alone never passes and a run
        # without the dynamic sample reports not_measured.
        #
        # Two legs, both required. The walk can only cover acts a policy survives to,
        # and the policy on disk is an Act-1 one, so the generator leg supplies act 3
        # while saying plainly that no played episode reached it.
        "pool_leg": bool((scenario or {}).get("G1_act_pools")),
        "scenario": scenario_note(scenario, "G1_act_pools"),
        "passed": False,
        "passed_because": "static tables alone cannot satisfy this gate; it needs the "
                          "dynamic per-act sample (--seeds) and the engine's own "
                          "per-act pool scenario",
    }

    map_text = _text(MAP_GENERATOR)
    ancient_dealt = re.search(
        r"private static void GenerateActAncient\(RunState state\)\s*\{"
        r"(?P<body>.*?)\n    \}",
        map_text,
        re.S,
    )
    ancient_body = (
        re.sub(r"\s+", " ", ancient_dealt.group("body")).strip() if ancient_dealt else ""
    )
    choices = _text(ANCIENT_CHOICES)
    offers = re.search(
        r"public static int\[\] Offer\(RunState state, GameRng rng\)\s*\{"
        r"(?P<body>.*?)\n    \}",
        choices,
        re.S,
    )
    offers_body = re.sub(r"\s+", " ", offers.group("body")).strip() if offers else ""
    # Each of the three acts the campaign can enter must be dispatched to its own
    # ancient, and the act's pool must be the real one: a screen that exists but only
    # ever shows Neow is the approx-v1 situation wearing a new name.
    acts_dispatched = all(
        name in offers_body
        for name in ("AncientNonupeipe", "AncientTanx", "AncientVakuu",
                      "AncientPael", "AncientTezcatara")
    ) and "AncientOrobas" not in ancient_body
    pools_are_the_builds = all(
        name in choices
        for name in (
            "OrobasOptionPool1", "PaelOptionPool1", "TezcataraOptionPool1",
            "NonupeipeOptionPool", "TanxBaseOptionPool", "VakuuPool1",
        )
    )
    presents_on_entry = "EnterActAncient()" in _text(RUN_ENGINE)
    static_shape = bool(
        ancient_dealt and acts_dispatched and pools_are_the_builds and presents_on_entry
    )
    gates["G2_ancients"] = {
        "real": REAL_ANCIENTS,
        "engine": {
            "generate_act_ancient_body": ancient_body or "not found",
            "offer_dispatch_body": offers_body or "not found",
            "ancient_dealt_per_act": bool(ancient_dealt),
            "every_hive_and_glory_ancient_dispatched": acts_dispatched,
            "pools_are_the_builds": pools_are_the_builds,
            "entry_presents_it_before_the_map": presents_on_entry,
        },
        # G2 is a claim about a transition (enter an act -> meet its ancient -> choose
        # from its pools), so the engine's own scenario has to have walked it; see
        # SCENARIO_TESTS.
        "passed": static_shape and bool((scenario or {}).get("G2_ancients")),
        "scenario": scenario_note(scenario, "G2_ancients"),
    }

    shape_text = _text(RUN_CONSTANTS)
    boss_row = re.search(r"MapBossRow\s*=\s*(\d+)", shape_text)
    rest_rule = re.search(r"NextGaussianInt\(([^)]*)\)", _text(MAP_GENERATOR))
    gates["G4_map_shape"] = {
        "real": REAL_SHAPE,
        "engine": {
            "MapBossRow": boss_row.group(1) if boss_row else None,
            "rest_rule": f"NextGaussianInt({rest_rule.group(1)})" if rest_rule else None,
            "per_act_differences": "none: every value is a single const shared by the "
                                   "three stages (RunConstants.cs:12-18)",
        },
        "passed": False,
    }

    reward_text = _text(REWARD_GENERATOR)
    # Anchor on the *definition*: matching `RollCardUpgrade(` alone lands on the
    # call site first, whose braces then swallow the next method -- which is how
    # this probe once read a hardcoded seed override as the roll and reported the
    # gate green.
    upgrade_stub = re.search(
        r"private static bool RollCardUpgrade\(\s*[^)]*\)\s*\{"
        r"(?P<body>.*?)\n    \}",
        reward_text,
        re.S,
    )
    upgrade_body = re.sub(r"\s+", " ", upgrade_stub.group("body")).strip() if upgrade_stub else ""
    upgrade_returns_false = "return false" in upgrade_body
    act_scaling_present = bool(
        upgrade_stub
        and re.search(r"CurrentAct|state\.Act", upgrade_body)
    )
    gates["G5_reward_and_upgrade_distribution"] = {
        "real": UPGRADE_ODDS_RULE,
        "engine": {
            "RollCardUpgrade_definition_found": bool(upgrade_stub),
            "RollCardUpgrade_body": upgrade_body or "not found",
            "act_index_scaling": act_scaling_present,
            "site": "RunRewardGenerator.cs:1127-1131",
        },
        "passed": bool(upgrade_stub) and not upgrade_returns_false and act_scaling_present,
    }

    combat_rewards = re.search(
        r"public static void GenerateCombatRewards\(\s*RunState state\s*\)\s*\{"
        r"(?P<body>.*?)\n    \}",
        reward_text,
        re.S,
    )
    rewards_body = combat_rewards.group("body") if combat_rewards else ""
    # Anchored on the post-combat definition on purpose: `PendingRelicReward = true`
    # also appears in two event handlers, and matching anywhere in the file read those
    # as a boss grant forever -- the mirror image of the same probe reading the *absence*
    # of a match as a pass.
    relic_grant = re.search(r"PendingRelicReward\s*=\s*(?P<expr>[^;]+);", rewards_body)
    relic_grant_expr = (
        re.sub(r"\s+", " ", relic_grant.group("expr")).strip() if relic_grant else ""
    )
    boss_grants_relic = bool(relic_grant) and "NodeBoss" in relic_grant_expr
    engine_text = _text(RUN_ENGINE)
    dealt = re.search(
        r"private static void GenerateSecondBoss\([^)]*\)\s*\{(?P<body>.*?)\n    \}",
        _text(MAP_GENERATOR),
        re.S,
    )
    dealt_body = re.sub(r"\s+", " ", dealt.group("body")).strip() if dealt else ""
    row = re.search(
        r"public static bool OpenSecondBossRow\([^)]*\)\s*\{(?P<body>.*?)\n    \}",
        _text(MAP_GENERATOR),
        re.S,
    )
    row_body = re.sub(r"\s+", " ", row.group("body")).strip() if row else ""
    deals_at_generation = bool(
        dealt
        and "IsGloryAct" in dealt_body
        and "state.BossEncounterId" in dealt_body
        and "NextItem" in dealt_body
        and "MapBossRow + 1" in dealt_body
    )
    row_past_boss = bool(
        row and "SecondBossCoord" in row_body and "AddEdge" in row_body
    )
    starts_on_the_spot = "StartFinalActBoss" in engine_text
    gates["G3_second_boss_structure"] = {
        "real": "ActMap.cs:13 / StandardActMap.cs:88-91 add SecondBossMapPoint one row "
                "past the boss; RunManager.cs:685-690 fills it with a boss from "
                "AllBossEncounters excluding the first, only at AscensionLevel.DoubleBoss; "
                "RoomSet.cs:72-82 returns it as the next boss after one has been visited; "
                "RewardsSet.cs:65-74 gives the final act's bosses an empty reward set, and "
                "RunManager.cs:1207-1246 ends the run in EventRoom<TheArchitect>",
        "engine": {
            "generate_second_boss_body": dealt_body or "not found",
            "open_second_boss_row_body": row_body or "not found",
            "dealt_at_act_generation_excluding_the_first": deals_at_generation,
            "reached_through_a_map_row_past_the_boss": row_past_boss,
            "paired_combat_still_started_on_the_spot": starts_on_the_spot,
        },
        # A resource decision cannot be trained on a structure that is only *claimed*
        # to exist: the shape here is necessary, and what closes the gate is a walk of
        # boss #1 -> map row -> travel -> boss #2 -> cleared, with the resources carried.
        "passed": bool(deals_at_generation and row_past_boss and not starts_on_the_spot)
                  and bool((scenario or {}).get("G3_second_boss_structure")),
        "scenario": scenario_note(scenario, "G3_second_boss_structure"),
    }

    gates["G6_no_boss_relic_reward"] = {
        "real": BOSS_REWARD_RULE,
        "engine": {
            "post_combat_relic_grant": relic_grant_expr or "not found",
            "definition_found": bool(combat_rewards),
            "boss_grants_a_relic": boss_grants_relic,
            "site": "RunRewardGenerator.cs GenerateCombatRewards(), and the two retained "
                    "trace fixtures that keep their historical grants verbatim",
        },
        "passed": bool(combat_rewards)
                  and not boss_grants_relic
                  and bool((scenario or {}).get("G6_no_boss_relic_reward")),
        "scenario": scenario_note(scenario, "G6_no_boss_relic_reward"),
    }
    return gates


def scenario_note(scenario: dict[str, Any] | None, gate: str) -> dict[str, Any]:
    """Say which evidence decided a structural gate, in the words that own it."""
    if not scenario:
        return {"measured": False, "reason": "no engine scenario evidence supplied"}
    return {
        "measured": bool(scenario.get("measured")),
        "evidence": scenario.get("evidence"),
        gate: bool(scenario.get(gate)),
    }


#: Which engine scenario each structural gate is allowed to lean on.  These are tests
#: of the compiled state machine, run here rather than re-derived from source text:
#: the judgement belongs to the code that owns it.
SCENARIO_TESTS: dict[str, list[str]] = {
    "G1_act_pools": [
        "EveryCampaignActDrawsFromItsOwnPools",
    ],
    "G2_ancients": [
        "ClearingAnActBossMeetsTheNextActsAncientBeforeItsMap",
        "GloryActsAncientOffersThreeOfItsOwnCandidates",
        "DistinguishedCape_ChargesNineMaxHealth_WhenAnAncientOffersIt",
    ],
    "G3_second_boss_structure": [
        "FinalActDealsItsPairedBoss_AtActGeneration_ExcludingTheFirst",
        "EarlierCampaignActs_DealNoPairedBoss",
        "FinalActNeedsBothBosses_AndOnlyThenReportsTheRunCleared",
    ],
    "G6_no_boss_relic_reward": [
        "OnlyAnEliteClearGrantsARelic_BossesNeverDo",
        "CampaignActTransition_NeedsNoRelicScreenToHappen",
    ],
}


def engine_scenario_tests(timeout: int = 900) -> dict[str, Any]:
    """Run the engine's own scenario tests and report per gate."""
    dotnet = ROOT / ".tools" / "dotnet" / "dotnet.exe"
    project = EMULATOR / "src" / "Sts2Emulator.Tests" / "Sts2Emulator.Tests.csproj"
    if not dotnet.is_file() or not project.is_file():
        return {
            "measured": False,
            "reason": f"missing {dotnet.name if not dotnet.is_file() else project.name}",
        }

    import os
    import subprocess

    env = dict(os.environ)
    env.setdefault("NUGET_PACKAGES", str(ROOT / ".cache" / "nuget"))
    env["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"

    # A --filter that matches no test at all exits 0, so an unverified filter would
    # turn a renamed scenario into a green gate.  Ask the runner which tests exist.
    try:
        catalog = subprocess.run(
            [str(dotnet), "test", str(project), "-c", "Release", "--list-tests"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env, check=False,
        )
    except Exception as exc:  # noqa: BLE001 - an unavailable probe must read as unmeasured
        return {"measured": False, "reason": f"dotnet --list-tests failed: {exc}"}
    absent = [
        name
        for names in SCENARIO_TESTS.values()
        for name in names
        if name not in (catalog.stdout or "")
    ]
    if absent:
        return {
            "measured": False,
            "reason": "the engine suite no longer contains " + ", ".join(absent),
        }

    evidence: dict[str, Any] = {}
    verdicts: dict[str, Any] = {}
    for gate, tests in SCENARIO_TESTS.items():
        filter_expression = "|".join(f"FullyQualifiedName~{name}" for name in tests)
        try:
            proc = subprocess.run(
                [str(dotnet), "test", str(project), "-c", "Release",
                 "--filter", filter_expression],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, env=env, check=False,
            )
            tail = re.sub(r"\s+", " ", (proc.stdout or "") + (proc.stderr or ""))[-400:]
            evidence[gate] = {"tests": tests, "exit_code": proc.returncode, "tail": tail}
            verdicts[gate] = proc.returncode == 0
        except Exception as exc:  # noqa: BLE001 - an unavailable probe must read as unmeasured
            evidence[gate] = {"tests": tests, "error": str(exc)}
            verdicts[gate] = False
    return {"measured": True, "evidence": evidence, **verdicts}


_WALK: dict[str, Any] = {}


def _import_walk_stack() -> None:
    import sts2_gym  # noqa: F401
    from sb3_contrib import MaskablePPO  # noqa: F401
    from training.v2_config import load_v2_training_config  # noqa: F401
    from training.v2_curriculum import _environment_factory  # noqa: F401


def _init_walk_worker(config_path: str, stage_name: str, checkpoint: str,
                      max_steps: int) -> None:
    """One factory and one policy per worker; a walk is only worth its checkpoint."""
    import sts2_gym
    from sb3_contrib import MaskablePPO
    from training.v2_config import load_v2_training_config
    from training.v2_curriculum import _environment_factory

    config = load_v2_training_config(Path(config_path))
    stage = next(s for s in config.stages if s.name == stage_name)
    _WALK["factory"] = _environment_factory(
        config, stage, sts2_gym, max_episode_steps=max_steps
    )
    _WALK["model"] = MaskablePPO.load(str(checkpoint), device="cpu")
    _WALK["max_steps"] = max_steps


def _walk_one(seed: int) -> dict[str, Any]:
    """One campaign episode, with the native run slot always given back."""
    env = _WALK["factory"](seed)
    try:
        return _episode(env, seed)
    finally:
        close = getattr(env, "close", None)
        if callable(close):
            close()


def _episode(env: Any, seed: int) -> dict[str, Any]:
    """Which encounters each act put on screen, and how far the walk got."""
    model = _WALK["model"]
    max_steps = _WALK["max_steps"]
    obs, info = env.reset(seed=seed, options={"campaign": True})
    drawn: dict[str, list[int]] = {"act_1": [], "act_2": [], "act_3": []}
    bosses: dict[str, list[int]] = {"act_1": [], "act_2": [], "act_3": []}
    nodes: dict[str, list[int]] = {"act_1": [], "act_2": [], "act_3": []}
    illegal = 0
    deepest_act = 0
    deepest_floor = 0
    dead_end = ""
    for _ in range(max_steps):
        mask = env.action_masks()
        if not any(bool(value) for value in mask):
            # An empty mask is an implementation defect, not a shallow walk: say so
            # per seed instead of letting it read as "this seed did not reach act 3".
            dead_end = str(info.get("simulator_dead_end") or "empty_action_mask")
            break
        action, _ = model.predict(obs, action_masks=mask, deterministic=True)
        obs, _reward, term, trunc, info = env.step(int(action))
        illegal += int(info.get("illegal_actions") or 0)
        deepest_floor = max(deepest_floor, int(info.get("floor") or 0))
        # The run's act is published as `act` by the V2 wrapper and as
        # `act_index` by the flat one; reading only either silently measures
        # nothing, which is why both are tried and an empty result is reported
        # as a measurement failure rather than as a clean act.
        act_index = int(info.get("act") or info.get("act_index") or 0)
        encounter_id = info.get("encounter_id")
        phase = info.get("phase")
        deepest_act = max(deepest_act, act_index if act_index in (1, 2, 3) else 0)
        # Only a frame that is *inside* a fight answers "can this act draw this
        # encounter". A map or transition frame still carries the previous node's
        # id in the info buffer, and counting those read act 1's boss as an act 2
        # draw off the first time the campaign crossed.  `phase` is 0 in combat,
        # so it is tested against None rather than through `or`.
        if (
            act_index in (1, 2, 3)
            and phase is not None
            and int(phase) == 0
            and encounter_id is not None
            and int(encounter_id) >= 0
        ):
            key = f"act_{act_index}"
            drawn[key].append(int(encounter_id))
            nodes[key].append(int(info.get("current_node_type") or 0))
            if int(info.get("current_node_type") or 0) == 6:
                bosses[key].append(int(encounter_id))
        if term or trunc:
            break
    return {
        "seed": seed,
        "drawn": drawn,
        "bosses": bosses,
        "nodes": nodes,
        "illegal": illegal,
        "deepest_act": deepest_act,
        "deepest_floor": deepest_floor,
        "cleared": bool(info.get("run_cleared") or info.get("run_won")),
        "dead_end": dead_end,
        "outcome": str(info.get("run_outcome") or ""),
    }


def sample_walks(
    seeds: list[int],
    max_steps: int,
    checkpoint: Path | None = None,
    *,
    workers: int = 1,
    per_act_target: int = 0,
    stage_name: str = "act1",
) -> dict[str, Any]:
    """Draw what the campaign actually meets, per act, with a reproducible policy.

    ``per_act_target`` is how many *episodes that fought in that act* are wanted before
    the sweep stops, and ``workers`` fans the seed list out.  Both exist because the
    policy on disk is an Act-1 policy: it reaches Act 2 often and Act 3 rarely, so a
    fixed seed count would report act 3 as empty for a reason that has nothing to do
    with which pool act 3 draws.
    """
    try:
        _import_walk_stack()
    except ImportError as exc:  # pragma: no cover - needs the training venv
        return {"measured": False, "reason": f"import failed: {exc}"}

    config_path = ROOT / "runtime/fanout/b_terminal-1.toml"
    default_checkpoint = ROOT / (
        "runtime/fanout/b_terminal-1/v2curriculum-20260918T182830Z/act1/"
        "checkpoints/step_000004000032.zip"
    )
    if checkpoint is None:
        checkpoint = default_checkpoint
    if not config_path.is_file() or not checkpoint.is_file():
        return {"measured": False,
                "reason": f"missing {config_path.name if not config_path.is_file() else checkpoint}"}

    drawn: dict[str, list[int]] = {"act_1": [], "act_2": [], "act_3": []}
    bosses: dict[str, list[int]] = {"act_1": [], "act_2": [], "act_3": []}
    nodes: dict[str, list[int]] = {"act_1": [], "act_2": [], "act_3": []}
    illegal_total = 0
    deepest_floor = 0
    episodes: list[dict[str, Any]] = []
    init = (str(config_path), stage_name, str(checkpoint), max_steps)
    if workers > 1:
        import multiprocessing

        pool_executor: Any = multiprocessing.Pool(
            processes=workers, initializer=_init_walk_worker, initargs=init
        )
        results = pool_executor.imap_unordered(_walk_one, seeds)
    else:
        _init_walk_worker(*init)
        results = (_walk_one(seed) for seed in seeds)
    try:
        for record in results:
            episodes.append(record)
            for act, ids in record["drawn"].items():
                drawn[act].extend(ids)
                nodes[act].extend(record["nodes"][act])
            for act, ids in record["bosses"].items():
                bosses[act].extend(ids)
            illegal_total += record["illegal"]
            deepest_floor = max(deepest_floor, record["deepest_floor"])
            if per_act_target and all(
                sum(1 for e in episodes if e["drawn"][act]) >= per_act_target
                for act in ("act_1", "act_2", "act_3")
            ):
                break
    finally:
        if workers > 1:
            pool_executor.terminate()
    reached = {
        f"episodes_that_fought_in_act_{act}": sum(
            1 for e in episodes if e["drawn"][f"act_{act}"]
        )
        for act in (1, 2, 3)
    }
    # Per-episode rows stay small: the encounter *sets* are what a reader checks, and
    # a 5,000-row frame dump would make this artifact unreadable.
    slim = [
        {
            "seed": e["seed"],
            "deepest_act": e["deepest_act"],
            "deepest_floor": e["deepest_floor"],
            "outcome": e["outcome"],
            "cleared": e["cleared"],
            "dead_end": e["dead_end"],
            "fought_per_act": {act: len(e["drawn"][act]) for act in e["drawn"]},
            "encounters_per_act": {
                act: sorted(set(ids)) for act, ids in e["drawn"].items() if ids
            },
            "bosses_per_act": {
                act: sorted(set(ids)) for act, ids in e["bosses"].items() if ids
            },
        }
        for e in sorted(episodes, key=lambda row: (-row["deepest_act"], -row["deepest_floor"]))
    ]
    return {
        "measured": True,
        "seeds_tried": len(episodes),
        "seeds_offered": len(seeds),
        "policy_checkpoint": str(checkpoint),
        "policy_note": "the checkpoint behind this sample was trained under approx-v1, so "
                       "how deep it walks is a property of that policy, not of this "
                       "environment; what is measured here is which encounter each act "
                       "actually puts on screen",
        "reached": reached,
        "deepest_episode_rows": slim[:200],
        "dead_ends": sorted({e["dead_end"] for e in episodes if e["dead_end"]}),
        "drawn_by_act": {k: sorted(set(v)) for k, v in drawn.items()},
        "boss_encounters_by_act": {k: sorted(set(v)) for k, v in bosses.items()},
        "node_types_by_act": {k: sorted(set(v)) for k, v in nodes.items()},
        "drawn_counts": {k: len(v) for k, v in drawn.items()},
        "deepest_floor": deepest_floor,
        "illegal_actions_total": illegal_total,
    }


def apply_dynamic_sample(
    gates: dict[str, Any], ids: dict[str, int], sample: dict[str, Any]
) -> dict[str, Any]:
    """Judge G1 on what a campaign walk met, act by act: N/N draws in the right pool.

    Kept separate from ``main`` so the rule that closes the gate is the rule a test
    can exercise, and so a sample that measured nothing can never read as clean.
    """
    gates["G1_act_pools"]["dynamic_sample"] = {
        k: v for k, v in sample.items() if k != "drawn_by_act"
    }
    if not sample.get("measured"):
        gates["G1_act_pools"]["dynamic_sample_failure"] = sample.get("reason", "unmeasured")
        gates["G1_act_pools"]["passed"] = False
        return gates

    drawn = sample["drawn_by_act"]
    if not any(drawn.get(act) for act in ("act_1", "act_2", "act_3")):
        # A probe that records nothing is not a probe that found nothing.
        gates["G1_act_pools"]["dynamic_sample_failure"] = (
            "the walk recorded no in-combat encounter at all; the sample sees a "
            "different info field than this probe reads"
        )
        gates["G1_act_pools"]["passed"] = False
        return gates
    for act, pool_key in (
        ("act_1", "act_1_overgrowth"),
        ("act_2", "act_2_hive"),
        ("act_3", "act_3_glory"),
    ):
        allowed = {
            ids[name]
            for tier in ("weak", "normal", "elite", "boss")
            for name in REAL_POOLS[pool_key][tier]
            if name in ids
        }
        seen = set(drawn.get(act, []))
        offside = sorted(seen - allowed)
        gates["G1_act_pools"][f"{act}_offside"] = offside
        gates["G1_act_pools"][f"{act}_in_pool"] = [len(seen & allowed), len(seen)]
    gates["G1_act_pools"]["dynamic_leg_passed"] = all(
        not gates["G1_act_pools"][f"{act}_offside"]
        and gates["G1_act_pools"][f"{act}_in_pool"][1] > 0
        for act in ("act_1", "act_2")
    ) and all(
        # Any act the walk did reach must be clean; acts it never reached are
        # reported, not counted as passes.
        not gates["G1_act_pools"][f"{act}_offside"]
        for act in ("act_1", "act_2", "act_3")
    )
    gates["G1_act_pools"]["unreached_acts"] = [
        act for act in ("act_2", "act_3")
        if gates["G1_act_pools"][f"{act}_in_pool"][1] == 0
    ]
    gates["G1_act_pools"]["passed"] = bool(
        gates["G1_act_pools"]["dynamic_leg_passed"] and gates["G1_act_pools"]["pool_leg"]
    )
    gates["G1_act_pools"]["accepted_as"] = (
        "walk leg: every encounter a played episode fought sat inside its own act's pool, "
        "with the act counts recorded; generator leg: the engine's own per-act draw test "
        "over 60 seeds, which is what covers an act no policy on disk reaches"
    )
    gates["G1_act_pools"].pop("passed_because", None)
    return gates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seeds", type=int, default=0,
                        help="campaign seeds to walk; 0 skips the dynamic sample")
    parser.add_argument("--seed-start", type=int, default=130008177)
    parser.add_argument(
        "--seed-file",
        type=Path,
        help="one seed per line, replacing --seed-start/--seeds; name the list in the "
             "report so a reach-directed sample is not read as a random one",
    )
    parser.add_argument(
        "--checkpoint", type=Path,
        help="policy archive used for the dynamic walk; recorded in the report",
    )
    parser.add_argument(
        "--sample-label",
        help="what the seed list is, e.g. 'the 25 seeds that reached a boss in act 2 "
             "under approx-v1' -- a reach-directed sample has to say so",
    )
    parser.add_argument("--max-steps", type=int, default=4800)
    parser.add_argument(
        "--workers", type=int, default=1,
        help="campaign walks in flight while the per-act sample fills up",
    )
    parser.add_argument(
        "--per-act-target", type=int, default=0,
        help="stop the sweep once this many episodes fought in every act; 0 walks the "
             "whole seed list",
    )
    parser.add_argument(
        "--no-engine-scenario",
        action="store_true",
        help="skip the engine's own scenario tests; G3 and G6 then report "
             "not_measured and cannot pass",
    )
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    ids = emulator_encounter_ids()
    scenario = None if args.no_engine_scenario else engine_scenario_tests()
    gates = measure_gates(ids, pool_tables(), scenario)
    sample = None
    if args.seed_file or args.seeds:
        if args.seed_file:
            seeds = [
                int(line)
                for line in args.seed_file.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")
            ]
        else:
            seeds = [args.seed_start + stride for stride in range(args.seeds)]
        sample = sample_walks(
            seeds, args.max_steps, args.checkpoint,
            workers=args.workers, per_act_target=args.per_act_target,
        )
        sample["seed_source"] = str(args.seed_file) if args.seed_file else (
            f"{args.seed_start}..{args.seed_start + args.seeds - 1} by stride 1"
        )
        if args.seed_file:
            # The list itself usually lives under runtime/, which is not committed, so
            # the artifact records its digest: the run is re-derivable without
            # trusting that the file still says what it said.
            import hashlib

            sample["seed_file_sha256"] = hashlib.sha256(
                args.seed_file.read_bytes()
            ).hexdigest()
        if args.sample_label:
            sample["label"] = args.sample_label
        apply_dynamic_sample(gates, ids, sample)

    published_gates = (
        "G1_act_pools",
        "G2_ancients",
        "G3_second_boss_structure",
        "G6_no_boss_relic_reward",
    )
    payload = {
        "schema_version": 1,
        "environment_version": _current_version(),
        "authority": "decompiled sources of the locked build; engine side is the "
                     "vendored emulator checkout",
        "gates": gates,
        "engine_scenario": scenario,
        "dynamic_sample": sample,
        # What the published version was allowed to claim.  The exit code is still
        # the six-gate verdict: closing four of them must not quietly turn the tool
        # into a four-gate tool.
        "published_gates_passed": all(bool(gates[g].get("passed")) for g in published_gates),
        "published_gates": list(published_gates),
        "hard_gate_passed": all(bool(g.get("passed")) for g in gates.values()),
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    for name in sorted(gates):
        gate = gates[name]
        print(f"{'PASS' if gate.get('passed') else 'FAIL'} {name}")
    print(f"published_gates_passed={payload['published_gates_passed']}")
    print(f"hard_gate_passed={payload['hard_gate_passed']}")
    return 0 if payload["hard_gate_passed"] else 1


def _current_version() -> str:
    from training.campaign_content import CAMPAIGN_ENVIRONMENT_VERSION
    return CAMPAIGN_ENVIRONMENT_VERSION


if __name__ == "__main__":
    raise SystemExit(main())
