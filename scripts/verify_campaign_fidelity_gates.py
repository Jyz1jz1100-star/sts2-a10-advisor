"""Measure the simulator's campaign against the locked build, gate by gate.

Six gates (G1-G6) decide whether a policy trained here can transfer.  This tool
does not read the fidelity document and agree with it; it reads the engine's own
C# tables, walks the environment with a reproducible checkpoint, and says for each
gate what the real build does, what this build does, and what still differs.

    <training python> scripts/verify_campaign_fidelity_gates.py --seeds 6 --out runtime/fidelity_gates.json

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


def measure_gates(ids: dict[str, int], tables: dict[str, list[int]]) -> dict[str, Any]:
    """Static comparison against the engine's own tables (G1, G2, G4, G5, G6)."""
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
    engine_stage2 = tables.get("UnderdocksWeakEncounters", []) + tables.get(
        "UnderdocksNormalEncounters", []
    )
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
        "stage_selector": "RunMapGenerator.cs:19 `underdocks = state.Act != ActOvergrowth`"
                          " -- acts 2 and 3 both take the Underdocks tables",
        "hive_ids_referenced_by_any_pool": hive_drawable,
        "glory_ids_referenced_by_any_pool": glory_drawable,
        "ids_the_emulator_cannot_name": missing_names,
        # The engine's own selector cannot express "stage 2 is Hive": it asks
        # whether the act is not act 1 and then takes the Underdocks tables, so
        # no static table comparison can call this gate passed.  The verdict is
        # left to the dynamic sample, and a run without one reports not_measured.
        "passed": False,
        "passed_because": "static tables alone cannot satisfy this gate; it needs the "
                          "dynamic per-act sample (--seeds)",
    }

    gates["G2_ancients"] = {
        "real": REAL_ANCIENTS,
        "engine": "no per-act Ancient event exists: the only ancient screen is the "
                  "run-start one (campaign_content 'not_modelled'); RunMapGenerator "
                  "event lists are the two fixed tables at :123-188",
        "passed": False,
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

    relic_on_boss = bool(
        re.search(r"NodeElite\s*(?:or|\|\|)\s*NodeType\.NodeBoss[^\n]*RelicReward",
                  reward_text, re.I)
    ) or "PendingRelicReward = true" in reward_text
    engine_text = _text(RUN_ENGINE)
    second_boss_in_engine = "SecondBoss" in engine_text or "DoubleBoss" in engine_text
    paired = re.search(
        r"private int PairedFinalActBoss\(\)\s*\{(?P<body>.*?)\n    \}", engine_text, re.S
    )
    paired_body = re.sub(r"\s+", " ", paired.group("body")).strip() if paired else ""
    gates["G3_second_boss_structure"] = {
        "real": "ActMap.cs:13 / StandardActMap.cs:88-91 add SecondBossMapPoint one row "
                "past the boss; RunManager.cs:685-690 fills it with a boss from "
                "AllBossEncounters excluding the first, only at AscensionLevel.DoubleBoss; "
                "RoomSet.cs:72-82 returns it as the next boss after one has been visited; "
                "RewardsSet.cs:65-74 gives the final act's bosses an empty reward set, and "
                "RunManager.cs:1207-1246 ends the run in EventRoom<TheArchitect>",
        "engine": {
            "second_boss_node_present": second_boss_in_engine,
            "paired_final_act_boss_body": paired_body or "not found",
            "site": "RunEngine.cs:1982-1994 PairedFinalActBoss() and :1996-2012 "
                    "StartFinalActBoss() -- the second boss is the next id in the same "
                    "pool and combat starts on the spot, so there is no intermediate "
                    "state, no reward, and no route choice between the two",
        },
        # A resource decision cannot be trained on a structure that does not exist:
        # with no map node and no reward between the two fights, "save HP and potions
        # for the second boss" has no observable consequence in the environment.
        "passed": second_boss_in_engine,
    }

    gates["G6_no_boss_relic_reward"] = {
        "real": BOSS_REWARD_RULE,
        "engine": {
            "relic_reward_granted_for_elite_or_boss": relic_on_boss,
            "site": "RunRewardGenerator.cs:394-395 (PendingRelicReward) and :403 "
                    "(Phase = RelicReward on every combat win)",
        },
        "passed": not relic_on_boss,
    }
    return gates


def sample_walks(seeds: list[int], max_steps: int) -> dict[str, Any]:
    """Draw what the campaign actually meets, per act, with a reproducible policy."""
    try:
        import numpy as np
        import sts2_gym
        from sb3_contrib import MaskablePPO
        from training.evaluation import evaluate_policy  # noqa: F401
        from training.v2_config import load_v2_training_config
        from training.v2_curriculum import _environment_factory
    except ImportError as exc:  # pragma: no cover - needs the training venv
        return {"measured": False, "reason": f"import failed: {exc}"}

    config_path = ROOT / "runtime/fanout/b_terminal-1.toml"
    checkpoint = ROOT / (
        "runtime/fanout/b_terminal-1/v2curriculum-20260918T182830Z/act1/"
        "checkpoints/step_000004000032.zip"
    )
    if not config_path.is_file() or not checkpoint.is_file():
        return {"measured": False,
                "reason": f"missing {config_path.name if not config_path.is_file() else checkpoint}"}

    config = load_v2_training_config(config_path)
    stage = next(s for s in config.stages if s.name == "act1")
    factory = _environment_factory(config, stage, sts2_gym, max_episode_steps=max_steps)
    model = MaskablePPO.load(str(checkpoint), device="cpu")
    drawn: dict[str, list[int]] = {"act_1": [], "act_2": [], "act_3": []}
    illegal_total = 0
    deepest_floor = 0
    for seed in seeds:
        env = factory(seed)
        obs, info = env.reset(seed=seed, options={"campaign": True})
        for _ in range(max_steps):
            mask = env.action_masks()
            if not any(bool(value) for value in mask):
                break
            action, _ = model.predict(
                obs, action_masks=mask, deterministic=True
            )
            obs, _reward, term, trunc, info = env.step(int(action))
            illegal_total += int(info.get("illegal_actions") or 0)
            deepest_floor = max(deepest_floor, int(info.get("floor") or 0))
            act_index = int(info.get("act_index") or 0)
            encounter_id = info.get("encounter_id")
            if act_index in (1, 2, 3) and encounter_id is not None and int(encounter_id) >= 0:
                drawn[f"act_{act_index}"].append(int(encounter_id))
            if term or trunc:
                break
    return {
        "measured": True,
        "seeds": seeds,
        "drawn_by_act": {k: sorted(set(v)) for k, v in drawn.items()},
        "drawn_counts": {k: len(v) for k, v in drawn.items()},
        "deepest_floor": deepest_floor,
        "illegal_actions_total": illegal_total,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seeds", type=int, default=0,
                        help="campaign seeds to walk; 0 skips the dynamic sample")
    parser.add_argument("--seed-start", type=int, default=130008177)
    parser.add_argument("--max-steps", type=int, default=4800)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    ids = emulator_encounter_ids()
    gates = measure_gates(ids, pool_tables())
    sample = None
    if args.seeds:
        sample = sample_walks(
            [args.seed_start + stride for stride in range(args.seeds)], args.max_steps
        )
        if sample.get("measured"):
            drawn = sample["drawn_by_act"]
            ids_by_act = {
                "act_1": set(
                    ids[n]
                    for tier in ("weak", "normal", "elite", "boss")
                    for n in REAL_POOLS["act_1_overgrowth"][tier] if n in ids
                ),
                "act_2": set(
                    ids[n]
                    for tier in ("weak", "normal", "elite", "boss")
                    for n in REAL_POOLS["act_2_hive"][tier] if n in ids
                ),
                "act_3": set(
                    ids[n]
                    for tier in ("weak", "normal", "elite", "boss")
                    for n in REAL_POOLS["act_3_glory"][tier] if n in ids
                ),
            }
            for act, allowed in ids_by_act.items():
                seen = set(drawn.get(act, []))
                offside = sorted(seen - allowed)
                gates["G1_act_pools"][f"{act}_offside"] = offside
            gates["G1_act_pools"]["dynamic_sample"] = sample
            gates["G1_act_pools"]["passed"] = all(
                not gates["G1_act_pools"].get(f"{act}_offside")
                and bool(drawn.get(act))
                for act in ids_by_act
            )
            gates["G1_act_pools"].pop("passed_because", None)
        else:
            gates["G1_act_pools"]["dynamic_sample"] = sample

    payload = {
        "schema_version": 1,
        "environment_version": _current_version(),
        "authority": "decompiled sources of the locked build; engine side is the "
                     "vendored emulator checkout",
        "gates": gates,
        "dynamic_sample": sample,
        "hard_gate_passed": all(bool(g.get("passed")) for g in gates.values()),
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    for name in sorted(gates):
        gate = gates[name]
        print(f"{'PASS' if gate.get('passed') else 'FAIL'} {name}")
    print(f"hard_gate_passed={payload['hard_gate_passed']}")
    return 0 if payload["hard_gate_passed"] else 1


def _current_version() -> str:
    from training.campaign_content import CAMPAIGN_ENVIRONMENT_VERSION
    return CAMPAIGN_ENVIRONMENT_VERSION


if __name__ == "__main__":
    raise SystemExit(main())
