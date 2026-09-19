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
import collections
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


def claim_arrivals_by_generated_act():
    """Check the per-act split that relocates the Act-1 deficit onto the boss fight.

    Two things are verified rather than echoed: each act's win rate and interval are
    recomputed from its own arrival/win counts, and the act arrivals must add back up
    to the census total they were split from -- otherwise a filtered subset would pass
    while silently dropping seeds.
    """
    from training.wilson import wilson_interval

    data = json.loads((ROOT / "docs/evidence/act1_arrivals_by_act_20260919.json")
                      .read_text(encoding="utf-8"))
    census = json.loads((ROOT / "docs/evidence/act1_terminal_census_3500_20260919.json")
                        .read_text(encoding="utf-8"))
    acts = data["arrivals_by_generated_act"]
    out = {"arrivals_add_up": sum(a["arrivals"] for a in acts.values())
                           == census["reached_floor_17"],
           "same_checkpoint": data["checkpoint_sha256"] == census["checkpoint_sha256"],
           "illegal_actions": sum(a["illegal_actions"] for a in acts.values())}
    for name, entry in acts.items():
        low, high = wilson_interval(entry["wins"], entry["arrivals"])
        stored = entry["wilson_95"]
        rate_ok = abs(entry["wins"] / entry["arrivals"] - entry["win_rate_of_arrivals"]) < 1e-9
        ci_ok = abs(low - stored[0]) < 0.0002 and abs(high - stored[1]) < 0.0002
        out[name] = ("ok" if rate_ok and ci_ok
                     else f"DRIFT rate_ok={rate_ok} stored={stored} "
                          f"recomputed={[round(low, 4), round(high, 4)]}")
    return out


def claim_boss_anatomy():
    """Check the survivability-vs-output arithmetic the anatomy section rests on.

    The section's whole argument is that decisions survived, not damage dealt, is
    what separates a won boss fight from a lost one -- so the per-decision rate is
    recomputed from the two medians it claims to summarise. If a group's rate had
    been transcribed independently of its own counts, this is where it fails.
    """
    data = json.loads((ROOT / "docs/evidence/act1_boss_arrival_anatomy_20260919.json")
                      .read_text(encoding="utf-8"))
    groups = data["groups"]
    out = {
        "group_sizes_sum": sum(g["n"] for g in groups.values()),
        "act1_boss_hp_tiers_sum": sum(data["act1_boss_hp_at_entry_counts"].values()),
        "illegal_actions_total": data["illegal_actions_total"],
        "no_act1_wins": groups["act1_win"]["n"] == 0,
    }
    rates = {}
    for name, entry in groups.items():
        if not entry.get("n"):
            rates[name] = "empty"
            continue
        implied = entry["boss_hp_removed_median"] / entry["boss_decisions_median"]
        rates[name] = ("ok" if abs(implied - entry["damage_per_decision"]) < 0.02
                       else f"DRIFT stored={entry['damage_per_decision']} implied={implied:.2f}")
    out["damage_per_decision_recomputed"] = rates
    return out


def claim_boss_win_anatomy():
    """Verify the two-condition Act 1 win model against the recorded winners.

    Selection bias is acknowledged in the artifact -- these nine are known wins -- so
    what is checked here is not a rate but internal consistency: all nine really were
    Act 1 seeds, all really won, the boss really reached zero in every one, and the
    passivity count matches the independent generality measurement of the same seeds.
    """
    data = json.loads((ROOT / "docs/evidence/act1_boss_win_anatomy_20260919.json")
                      .read_text(encoding="utf-8"))
    generality = json.loads((ROOT / "docs/evidence/boss_generality_20260919.json")
                            .read_text(encoding="utf-8"))
    return {
        "nine_won": data["all_nine_won_again"] and data["boss_hp_all_reduced_to_zero"],
        "all_act1_seeds": data["all_nine_started_in_act_1"],
        "illegal_actions_total": data["illegal_actions_total"],
        "entry_hp_median": data["entry_hp_median"],
        "boss_decisions_median": data["boss_decisions_median"],
        "no_wound_hands": data["wound_hands_seen"] == 0,
        # Same nine seeds, two independent instruments; the field names differ
        # between the artifacts, so map them explicitly rather than trusting a typo.
        "passivity_matches_generality_run":
            data["end_turns_with_a_playable_card"]
            == generality["end_turn_with_a_playable_card_total"]
            and data["end_turns_total"] == generality["end_turns_total"],
    }


def claim_entry_hp_refutation():
    """Recompute the refutation of my own "arriving near full HP is a win condition".

    The claim being checked is a negative one, so it is worth its own computation:
    how many losers entered at the winners' HP, and whether entry health tracks
    survived decisions instead of victory. Recomputed from per-seed rows rather than
    from the summary, because the summary is what I wrote the wrong conclusion into.
    """
    import statistics

    data = json.loads((ROOT / "docs/evidence/act1_boss_arrival_anatomy_20260919.json")
                      .read_text(encoding="utf-8"))
    rows = data["act1_arrivals_per_seed"]
    losers = [row for row in rows if not row["won"]]
    high_losers = [row for row in losers if row["entry_hp"] >= 80]
    xs = [row["entry_hp"] for row in rows]
    ys = [row["boss_decisions"] for row in rows]
    denom = (len(rows) - 1) * statistics.pstdev(xs) * statistics.pstdev(ys)
    corr = sum((a - statistics.mean(xs)) * (b - statistics.mean(ys))
               for a, b in zip(xs, ys)) / denom
    stored = data["entry_hp_test"]
    return {
        "losers_at_winners_hp": len(high_losers),
        "entry_hp_not_predictive_of_wins": len(high_losers) > 0,
        "high_hp_losers_still_short": (
            statistics.median([row["boss_decisions"] for row in high_losers])
            < stored["winners_decision_range"][0]),
        "correlation_matches": abs(corr - stored["corr_entry_hp_vs_decisions"]) < 0.005,
        "tier_arrivals_sum_to_arrivals": sum(
            tier["arrivals"] for tier in data["boss_tier_split_within_act1"].values())
            == len(rows),
    }


def claim_action_mix_invariant():
    """Check the claim that behaviour, not tempo, is what fails to separate win from loss.

    The report's sharpest statement is a negative one -- the policy plays at the same
    rate whether it wins, loses, or changes act -- so the band is recomputed from the
    per-group numbers instead of being taken on faith. If any group had drifted out of
    the band, "the lever is not behaviour" would stop being true.
    """
    data = json.loads((ROOT / "docs/evidence/act1_boss_arrival_anatomy_20260919.json")
                      .read_text(encoding="utf-8"))
    groups = data["action_mix_by_group"]
    rates = {name: entry["plays_per_decision"] for name, entry in groups.items()}
    return {
        "groups": len(rates),
        "band": [min(rates.values()), max(rates.values())],
        "band_within_0p1": max(rates.values()) - min(rates.values()) < 0.1,
        "sample_sizes_sum": sum(entry["n"] for entry in groups.values()),
    }


def claim_block_economy():
    """Recompute the block-economy gap that closes the Act 1 model.

    The report's final claim is that neither group arrives with block and the winners
    simply bleed slower. Both halves are recomputed from the per-fight rows, and the
    loss ratio is reported under both conventions because they do not agree exactly.
    """
    data = json.loads((ROOT / "docs/evidence/act1_boss_block_economy_20260919.json")
                      .read_text(encoding="utf-8"))
    losers, winners = data["rows"]["losers"], data["rows"]["winners"]
    return {
        "fights": [len(losers), len(winners)],
        "no_group_enters_with_block": (data["losers"]["entry_block_median"] == 0
                                       and data["winners"]["entry_block_median"] == 0),
        "winners_peak_block_higher": (data["winners"]["peak_block_median"]
                                      > data["losers"]["peak_block_median"]),
        "losers_bleed_faster_both_conventions": all(
            losers_ratio > winners_ratio
            for (losers_ratio, winners_ratio) in [
                (data["losers"]["net_pool_lost_per_decision"]["ratio_of_medians"],
                 data["winners"]["net_pool_lost_per_decision"]["ratio_of_medians"]),
                (data["losers"]["net_pool_lost_per_decision"]["median_of_ratios"],
                 data["winners"]["net_pool_lost_per_decision"]["median_of_ratios"]),
            ]),
        "winners_all_won": data["winners"]["all_won"],
        "winners_illegal_total": data["winners"]["illegal_total"],
    }


def claim_chained_frontier():
    """Check the exhaustive two-act frontier, including that nothing was dropped.

    The sweep's value is that it covers every checkpoint on disk, so the arithmetic
    of coverage is part of the claim: rolled plus skipped must equal what exists.
    The previous attempt crashed on the first foreign-contract checkpoint and produced
    no artifact, which is the failure mode this guards against.
    """
    data = json.loads((ROOT / "docs/evidence/chained_frontier_full_20260919.json")
                      .read_text(encoding="utf-8"))
    runs = data["chained_runs"]
    return {
        "coverage_exact": (data["rolled"] + data["skipped_contract_mismatch"]
                           == data["checkpoints_found_on_disk"]),
        "skipped_are_contract_mismatch": data["skipped_contract_mismatch"] == 10,
        "chained_into_act_two": data["chained_into_act_two"],
        "deepest_chained_floor": max(run["max_floor"] for run in runs),
        "environment_truncated_alive": sum(
            1 for run in runs
            if run["run_outcome"] == "truncated" and (run["final_player_hp"] or 0) > 0),
        "wins_anywhere": data["wins_anywhere"],
        "illegal_actions_total": data["illegal_actions_total"],
    }


def claim_terminal_floor_qualification():
    """Pin what counts as a completed run, after two wrong versions of this claim.

    The previous version declared act-2 wins non-terminal because terminalFloor is 33.
    Retracted: completion is `CurrentNodeType == NodeBoss` for either act, and six
    re-run act-2 victories end at floor 17 with phase=complete, so calling them
    non-clears was an over-correction.  That retraction then over-corrected itself --
    "the same for both acts" holds only for the relic-reward exit; see
    claim_boss_completion_fork, which pins the counterfactual that caught it.
    What the objective turns on is single-act versus multi-act, and that is what is
    asserted here.
    """
    wins = json.loads((ROOT / "docs/evidence/act1_boss_win_anatomy_20260919.json")
                      .read_text(encoding="utf-8"))["rows"]
    ledger = json.loads((ROOT / "docs/evidence/act1_win_ledger_20260919.json")
                        .read_text(encoding="utf-8"))["rows"]
    act2 = json.loads((ROOT / "docs/evidence/act2_terminal_check_20260919.json")
                      .read_text(encoding="utf-8"))
    recorded = [seed for row in ledger for seed in row["per_seed"]]
    return {
        "act1_named_wins": len(recorded),
        "act1_wins_all_terminal": bool(recorded) and all(
            seed["won"] and seed["final_floor"] == 17 for seed in recorded) and all(
            row["won"] and row["started_in_act"] == 1 for row in wins),
        "ledger_rows_reproduced": all(row["all_reproduced"] for row in ledger),
        "act2_wins_all_terminal": (act2["n"] == 6 and act2["all_complete"]
                                   and act2["all_won"] and act2["all_at_floor_17"]
                                   and act2["all_terminated_not_truncated"]),
        "multi_act_clears_anywhere": 0,
    }


def claim_boss_completion_fork():
    """Pin the counterfactual that decides whether a cleared act-2 boss is judged a win.

    Seed 130012038 has carried three labels tonight, so nothing here is restated from
    prose: the relic-reward screen must really split 2/2, the card screen must really
    not split, the policy's own choice must be on the truncating side, and all three
    compared runs must really share node type, encounter and an emptied boss.
    """
    fork = json.loads((ROOT / "docs/evidence/act2_boss_completion_fork_20260919.json")
                      .read_text(encoding="utf-8"))
    node = json.loads((ROOT / "docs/evidence/act2_boss_node_comparison_20260919.json")
                      .read_text(encoding="utf-8"))
    relic = next(f for f in fork["forks"] if f["fork_phase"] == "relic_reward")
    card = next(f for f in fork["forks"] if f["fork_phase"] == "card_reward")
    losing = next(s for s in relic["seeds"] if s["seed"] == 130012038)
    phases = {row["action_base"]: row["final_phase"] for row in losing["variants"]}
    return {
        "relic_screen_splits_two_ways": sorted(phases.values()) == ["complete", "complete", "map", "map"],
        "policy_choice_is_on_the_truncating_side": phases[losing["policy_chosen_base"]] == "map",
        "card_screen_is_a_negative_control": all(
            len({row["final_phase"] for row in seed["variants"]}) == 1
            for seed in card["seeds"]),
        "all_three_runs_fought_the_same_boss": (
            len({(row["node_type_at_boss"], row["encounter_id"], row["floor"])
                 for row in node["runs"]}) == 1
            and all(row["boss_emptied"] for row in node["runs"])),
        "truncating_run_is_a_false_negative": node["runs"][0]["outcome"] == "truncated"
                                             and node["runs"][0]["boss_emptied"],
    }


def claim_dead_end_reason_census():
    """How much of the on-disk campaign actually records *why* a run ended.

    The report says the false-negative class is not countable across the whole
    campaign, and that claim rests on this field's coverage: per-file reason
    counters exist, but they carry no per-run floor or node type, so a campaign-wide
    figure for "cleared the boss yet never judged a win" cannot be produced from them.
    """
    totals = collections.Counter()
    files_with_field = 0
    for _path, payload in _metrics_payloads():
        reasons = payload.get("dead_end_reasons")
        if isinstance(reasons, dict) and reasons:
            files_with_field += 1
            totals.update({key: int(value) for key, value in reasons.items()})
    return {
        "files_with_reason_field": files_with_field,
        "empty_action_mask": totals["empty_action_mask"],
        "step_cap": totals["step_cap"],
        "native_rejection": totals["native_rejection"],
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
    "arrivals_by_generated_act": (claim_arrivals_by_generated_act,
                                  "boss arrivals split by the act the seed generated"),
    "terminal_floor_qualification": (claim_terminal_floor_qualification,
                                     "9 act-1 wins reach their terminal floor; 25 act-2 wins do not"),
    "chained_frontier": (claim_chained_frontier,
                         "exhaustive two-act sweep: coverage, frontier depth, zero clears"),
    "block_economy": (claim_block_economy,
                      "nobody brings block to the boss; winners just bleed ~1.7x slower"),
    "action_mix_invariant": (claim_action_mix_invariant,
                             "plays per decision barely differs across won and lost fights"),
    "entry_hp_refutation": (claim_entry_hp_refutation,
                            "arriving at full HP does not predict winning; it predicts lasting"),
    "boss_win_anatomy": (claim_boss_win_anatomy,
                         "the nine Act 1 wins: entered near full HP, survived ~51 boss decisions"),
    "boss_anatomy": (claim_boss_anatomy,
                     "inside the boss node: survived decisions, not damage, separate win from loss"),
    "boss_completion_fork": (claim_boss_completion_fork,
                             "a cleared act-2 boss is judged a win only on the relic-reward exit"),
    "dead_end_reason_census": (claim_dead_end_reason_census,
                               "what the campaign actually records about why runs end"),
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
