"""Recompute the campaign report's headline numbers straight from disk.

The report quotes counts -- how many metrics files exist, how many Act-1
episodes were evaluated, how many wins, what the observation and action sizes
are, how many acts the emulator defines. Every one of those was derived by hand
during the night, which is exactly how a stale or wishful number survives into a
report. This turns each quoted figure into a named computation with an expected
value, so a reader can re-audit the summary table with one command instead of
trusting prose.

A DRIFT line means the report and the disk disagree. That is a finding about the
report, not a reason to edit the expectation to match: several of these numbers
moved during the night because a measurement superseded an earlier one.

Run it with the emulator's venv interpreter, like every other evaluation tool
here -- the repo's ``.tools/python`` has no numpy, so the ``v2_contract_sizes``
claim reports ERROR and the rest still verify::

    ../third_party/slay-the-spire-2-emulator-main/.venv/Scripts/python.exe \
        scripts/verify_report_claims.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SKIP = {".venv", "__pycache__", ".git", ".cache", "node_modules"}
EMULATOR = ROOT.parent / "third_party/slay-the-spire-2-emulator-main"


def _json_files(*parts):
    for path in ROOT.rglob("*.json"):
        if SKIP.intersection(path.parts):
            continue
        if all(part in path.parts for part in parts):
            yield path


def _metrics_payloads(scope=None, stage=None):
    for path in _json_files("metrics"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            continue
        if not isinstance(payload, dict):
            continue
        if scope is not None and payload.get("scope") != scope:
            continue
        if stage is not None and payload.get("stage") != stage:
            continue
        yield path, payload


def claim_metrics_file_count():
    return sum(1 for _ in _json_files("metrics"))


def claim_act1_scope_file_and_episode_count():
    files = episodes = 0
    for _path, payload in _metrics_payloads(scope="simulator_act1"):
        if not payload.get("episodes"):
            continue
        files += 1
        episodes += int(payload["episodes"])
    return {"files": files, "episodes": episodes}


def claim_campaign_truncation_class():
    """The population the report actually quotes: campaign files under runtime/."""
    files = episodes = truncations = 0
    non_floor17 = 0
    for path, payload in _metrics_payloads(scope="simulator_act1", stage="act1"):
        if "runtime" not in path.parts:
            continue
        count = payload.get("episodes")
        rate = payload.get("truncation_rate")
        if not count or rate is None:
            continue
        files += 1
        episodes += int(count)
        here = round(rate * int(count))
        truncations += here
        if here and payload.get("max_final_floor") != 17:
            non_floor17 += 1
    return {"files": files, "episodes": episodes, "truncated": truncations,
            "truncation_bearing_files_not_floor17": non_floor17}


def claim_repo_wide_truncation_population():
    """Why the same phrase measured repo-wide does not match the report.

    Two reasons, both traps: the V1 curriculum run is also ``stage=act1`` with
    ``scope=simulator_act1`` but a different contract, and its records exist in
    original plus ``-reevaluated-*`` copies, so summing files double-counts the
    same episodes. Reported separately rather than filtered silently.
    """
    buckets = {}
    for path, payload in _metrics_payloads(scope="simulator_act1", stage="act1"):
        count, rate = payload.get("episodes"), payload.get("truncation_rate")
        if not count or rate is None:
            continue
        contract = "v2" if ("v2" in payload or int(payload.get("schema_version") or 0) >= 3) else "v1"
        kind = "reevaluated" if "reevaluated" in path.name else "original"
        key = f"{contract}/{kind}"
        bucket = buckets.setdefault(key, {"files": 0, "episodes": 0, "truncated": 0})
        bucket["files"] += 1
        bucket["episodes"] += int(count)
        bucket["truncated"] += round(rate * int(count))
    return dict(sorted(buckets.items()))


def claim_v2_contract_sizes():
    from training.v2_observation import OBS_SIZE
    from training.v2_flat_env import FLAT_SIZE

    return {"observation_size": OBS_SIZE, "flat_action_size": FLAT_SIZE}


def claim_emulator_act_count():
    text = (EMULATOR / "src/Sts2Emulator/Core/Run/RunConstants.cs").read_text(encoding="utf-8")
    acts = sorted(set(re.findall(r"const int (Act[A-Za-z]+)\s*=", text)))
    boss_row = int(re.search(r"const int MapBossRow\s*=\s*(\d+)", text).group(1))
    return {"acts": acts, "act_count": len(acts), "map_boss_row": boss_row,
            "terminal_floor_single_act": boss_row + 1}


def claim_evidence_matrix_totals():
    matrix = json.loads((ROOT / "docs/evidence/act1_evidence_20260919.json").read_text(encoding="utf-8"))
    totals = matrix["totals"]
    return {"act1_episodes": totals["act1_evaluations_episodes"],
            "win_events": totals["win_events"],
            "distinct_winning_seeds": totals["distinct_winning_seeds"],
            "illegal_actions": totals["illegal_actions_total"],
            "unclassified_dead_ends": totals["unclassified_dead_ends_total"]}


def claim_win_ledger():
    ledger = json.loads((ROOT / "docs/evidence/act1_win_ledger_20260919.json").read_text(encoding="utf-8"))
    return {"wins_reproduced": ledger["wins_reproduced"],
            "wins_failed_to_reproduce": ledger["wins_failed_to_reproduce"],
            "rows": ledger["win_rows"]}


def claim_ladder_rungs():
    """Which configured warm-start rungs actually exist as artifacts.

    The report claims a per-stage ladder, so "configured" and "ran" are
    different facts that must not be conflated: a stage with no directory was
    never trained, and a stage without a promotion decision never passed its
    gate, so no checkpoint legitimately continues from it.
    """
    import tomllib

    with (ROOT / "config/training_v2.toml").open("rb") as handle:
        raw = tomllib.load(handle)
    stages = list(raw.get("stages", {}))
    out = {}
    for stage in stages:
        # Booleans, not counts: a count would go stale the moment legitimate new
        # work lands, which would train the reader to ignore DRIFT.
        dirs = [p for p in list((ROOT / "runs").rglob(stage)) + list((ROOT / "runtime").rglob(stage))
                if p.is_dir() and not SKIP.intersection(p.parts)]
        decisions = [p / "promotion_decision.json" for p in dirs
                     if (p / "promotion_decision.json").exists()]
        passed = False
        for path in decisions:
            try:
                passed = passed or bool(json.loads(path.read_text(encoding="utf-8")).get("promoted"))
            except (json.JSONDecodeError, OSError):
                continue
        out[stage] = {
            "ran": bool(dirs),
            # three distinct facts that are easy to conflate: a stage can run and
            # never record a decision (floor10), or record one that says "no" (act1)
            "decision_recorded": bool(decisions),
            "gate_passed": passed,
        }
    return out


def claim_promoted_checkpoint_digests():
    """The two frozen promoted rungs everyone warm-starts from still match their records."""
    targets = {
        "floor3": ("runs/curriculum_v2/v2curriculum-20260901T091856Z/floor3/checkpoints/step_000000500016.zip",
                   "69c6c32d693f51be"),
        "floor6": ("runs/curriculum_v2/v2curriculum-20260901T115142Z/floor6/checkpoints/step_000001000008.zip",
                   "b8deab061798daeb"),
    }
    out = {}
    for rung, (rel, want) in targets.items():
        path = ROOT / rel
        if not path.is_file():
            out[rung] = "MISSING"
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        out[rung] = "ok" if actual.startswith(want) else "MISMATCH"
    return out


def claim_stalemate_lock():
    """Re-derive the report's stalemate mechanism from the committed probe artifacts.

    The claim being checked is the strong one -- "the policy is locked out, not
    turtling" -- so the numbers that carry it are recomputed from the raw combat
    block rather than read back out of a summary field: a five-card hand of
    unplayable Wounds, full energy, and HP frozen across the whole sampled tail.
    """
    sixk = json.loads((ROOT / "docs/evidence/stalemate_mechanics_60k_20260919.json")
                      .read_text(encoding="utf-8"))
    tail = json.loads((ROOT / "docs/evidence/stalemate_tail_20k_20260919.json")
                      .read_text(encoding="utf-8"))
    wide = sixk["results"][0]["action_window"]
    block = tail["results"][0]["action_window"]["tail_last_block"]
    hand = [int(block[8 + i * 2]) for i in range(10) if int(block[8 + i * 2]) != 0]
    return {
        "end_turn_decisions": wide["chosen_action_kinds"].get("end_turn"),
        "play_card_decisions": wide["chosen_action_kinds"].get("play_card"),
        "end_turn_with_a_playable_card": wide["end_turn_with_a_playable_card"],
        "end_turn_was_the_only_legal_action":
            wide["end_turn_when_end_turn_was_the_only_legal_action"],
        # 10011 is "Wound": Cost -1, Type Status, Unplayable true
        # (third_party .../Generated/Cards.g.cs:136).
        "tail_hand_is_five_wounds": hand == [10011] * 5,
        "tail_energy_at_max": int(block[3]) == int(block[4]) and int(block[3]) > 0,
        "tail_hp_fields_not_varying": not ({0, 54} & set(
            tail["results"][0]["action_window"]["tail_varying_indices"])),
    }


def claim_boss_generality():
    """Recompute the non-scripted boss comparison from the committed rows.

    The report says three things off this artifact -- every sampled fight was won,
    none was Wound-locked, and passing the turn with a playable card is rare but
    not zero.  All three are re-derived from the per-row data instead of trusted
    from the summary, because the summary is the part a hand-edit would reach first.
    """
    data = json.loads((ROOT / "docs/evidence/boss_generality_20260919.json")
                      .read_text(encoding="utf-8"))
    rows = data["rows"]
    decisions = [row["boss_decisions"] for row in rows]
    return {
        "fights": len(rows),
        "won": sum(1 for row in rows if row["won"]),
        "boss_decision_range": [min(decisions), max(decisions)],
        "end_turns": sum((row["chosen_action_kinds"] or {}).get("end_turn", 0)
                         for row in rows),
        "end_turn_with_a_playable_card":
            sum(row["end_turn_with_a_playable_card"] or 0 for row in rows),
        "wound_locked_fights": sum(1 for row in rows if row["wound_in_tail_hand"]),
        "illegal_actions": sum(row["illegal_actions"] for row in rows),
        "every_fight_ended_at_floor_17": all(row["final_floor"] == 17 for row in rows),
    }


def claim_terminal_census():
    """Recompute the census' published rates and intervals from its own counts.

    The report quotes percentages and Wilson bounds next to raw counts. Recomputing
    the intervals from those counts catches the failure mode that actually happens
    here -- a correct numerator paired with a copied-in stale denominator.
    """
    from training.wilson import wilson_interval

    data = json.loads((ROOT / "docs/evidence/act1_terminal_census_3500_20260919.json")
                      .read_text(encoding="utf-8"))
    total = data["enumerated"]
    arrivals = data["reached_floor_17"]
    wins = data["boss_win"]
    truncations = len(data["boss_truncation_seeds"])

    def bound(key, num, den):
        low, high = wilson_interval(num, den)
        stored = data["wilson_95"][key]
        ok = abs(low - stored[0]) < 0.0002 and abs(high - stored[1]) < 0.0002
        return "ok" if ok else f"DRIFT stored={stored} recomputed={[round(low, 4), round(high, 4)]}"

    return {
        "enumerated": total,
        "arrivals_plus_below": arrivals + (total - arrivals) == total,
        "arrival_outcomes_sum": wins + truncations + data["died_at_floor_17"] == arrivals,
        "illegal_actions": data["illegal_actions"],
        "unclassified_dead_ends": data["unclassified_dead_ends"],
        "distinct_winning_seeds": len(set(data["winning_seeds"])),
        "boss_truncation_seeds": data["boss_truncation_seeds"],
        "wilson_recomputed": {
            "arrival": bound("reached_floor_17_of_all", arrivals, total),
            "win_of_arrival": bound("boss_win_of_arrivals", wins, arrivals),
            "lock_of_arrival": bound("boss_truncation_of_arrivals", truncations, arrivals),
        },
    }


CLAIMS = {
    "metrics_file_count": (claim_metrics_file_count,
                           "how many metrics JSON files exist repo-wide"),
    "act1_scope_population": (claim_act1_scope_file_and_episode_count,
                              "files and episodes labelled scope=simulator_act1"),
    "campaign_truncation_class": (claim_campaign_truncation_class,
                                  "act1-stage truncations, and whether they sit at floor 17"),
    "repo_wide_truncation_population": (claim_repo_wide_truncation_population,
                                        "the same phrase measured repo-wide, split by "
                                        "contract and by original-vs-reevaluated file"),
    "v2_contract_sizes": (claim_v2_contract_sizes,
                          "OBS_SIZE and flat action size quoted in the anti-misreading note"),
    "emulator_act_ceiling": (claim_emulator_act_count,
                              "the two-act ceiling the whole scope statement rests on"),
    "evidence_matrix_totals": (claim_evidence_matrix_totals,
                               "the committed Act-1 matrix totals"),
    "win_ledger": (claim_win_ledger,
                   "the committed reproduction ledger"),
    "ladder_rungs": (claim_ladder_rungs,
                     "configured warm-start rungs vs the ones that actually ran and promoted"),
    "promoted_checkpoint_digests": (claim_promoted_checkpoint_digests,
                                    "the frozen floor3/floor6 checkpoints everyone continues from"),
    "stalemate_lock": (claim_stalemate_lock,
                       "the boss stalemate is a lockout (unplayable Wound hand), not turtling"),
    "boss_generality": (claim_boss_generality,
                        "the same measurement on non-scripted seeds: won, unlocked, rarely passive"),
    "terminal_census": (claim_terminal_census,
                        "per-seed Act-1 terminal categories and the rates the report quotes"),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expect", type=Path,
                        default=ROOT / "docs/evidence/act1_report_expectations.json")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    expectations = {}
    if args.expect.exists():
        expectations = json.loads(args.expect.read_text(encoding="utf-8"))

    drift = 0
    for name, (fn, note) in CLAIMS.items():
        try:
            actual = fn()
        except Exception as error:  # a broken claim is reported, not swallowed
            print(f"ERROR   {name}: {type(error).__name__}: {error}")
            drift += 1
            continue
        expected = expectations.get(name)
        if expected is None:
            status = "UNPINNED"
            drift += 1
        elif expected == actual:
            status = "ok"
        else:
            status = "DRIFT"
            drift += 1
        if status != "ok" or not args.quiet:
            print(f"{status:9} {name}: {json.dumps(actual, sort_keys=True)}")
            if status == "DRIFT":
                print(f"          expected {json.dumps(expected, sort_keys=True)}")
            print(f"          {note}")
    print(f"\n{len(CLAIMS) - drift}/{len(CLAIMS)} claims match the disk")
    return 1 if drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
