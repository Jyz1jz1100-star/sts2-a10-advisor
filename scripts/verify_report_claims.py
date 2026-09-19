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
from datetime import UTC, datetime
from pathlib import Path


def rows_digest(recorded: list) -> str:
    """Hash the ledger rows, not the file -- the convention scripts/act1_win_ledger.py uses."""
    return hashlib.sha256(
        json.dumps(recorded, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

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
    runs = ledger.get("verification_runs", [])
    recorded_wins = [row for entry in ledger.get("rows", [])
                     for row in (entry.get("per_seed") or [])]
    provenance = json.loads((ROOT / "docs/evidence/emulator_source_provenance_20260920.json")
                            .read_text(encoding="utf-8"))
    boss_row = next((int(match.group(1))
                     for cite in provenance["citations"]
                     if cite.get("file") == "RunConstants.cs"
                     and str(cite.get("first_line")) == "14"
                     and (match := re.search(r"MapBossRow\s*=\s*(\d+)", str(cite.get("snippet"))))),
                    None)
    return {"wins_reproduced": ledger["wins_reproduced"],
            "wins_failed_to_reproduce": ledger["wins_failed_to_reproduce"],
            "rows": ledger["win_rows"],
            # The objective's own three qualifiers, recomputed from the per-seed rows rather than read
            # out of the summary table: every reproduced win is a terminal win, and not one of them
            # was bought with an illegal action, an unclassified dead end or a truncation.
            "every_reproduced_win_is_a_clean_terminal_win": (
                len(recorded_wins) == 9 and all(row["won"] for row in recorded_wins)
                and {int(row["final_floor"]) for row in recorded_wins} == {17}
                and all(int(row["illegal_actions"]) == 0 for row in recorded_wins)
                and all(int(row["unclassified_dead_ends"]) == 0 for row in recorded_wins)
                and all(int(row["truncations"]) == 0 for row in recorded_wins)
                and len({int(row["seed"]) for row in recorded_wins}) == 9),
            # The +1 from boss row to terminal floor is RunEngine.cs:1986-1989, whose text is
            # hash-pinned by the provenance claim; a snippet only carries 90 characters, so this reads
            # the constant from the snippet and takes the relation from that pinned citation.
            "the_terminal_floor_agrees_with_the_engines_boss_row_constant": (
                boss_row is not None and boss_row + 1 == 17),
            # The campaign's headline is nine individually reproducible wins, so the thing worth
            # pinning is not "they reproduced once" but "two runs, days and one code refactor
            # apart, produced rows that hash the same".  Booleans and a digest, no counts: a
            # legitimate future re-run must not be able to make this read as drift.
            "two_runs_recorded_and_their_rows_hash_alike": (
                len(runs) >= 2
                and ledger.get("rows_identical_across_runs") is True
                and all(entry["wins_failed_to_reproduce"] == 0 for entry in runs)),
            "the_recorded_rows_match_the_rows_this_file_carries": bool(runs) and rows_digest(
                ledger["rows"]) == runs[-1]["rows_sha256"]}


def claim_ladder_rungs():
    """Which configured warm-start rungs actually exist as artifacts.

    The report claims a per-stage ladder, so "configured" and "ran" are
    different facts that must not be conflated: a stage with no directory was
    never trained, and a stage without a promotion decision never passed its
    gate, so no checkpoint legitimately continues from it.  A decision passing
    *at one episode* is not that stage's gate either -- ``gate_passed`` only
    counts decisions whose own ``required.min_episodes`` reaches the stage's
    promotion scale.
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
        # A promotion_decision.json is only a promotion at the scale its stage asks for.  42 of
        # the 69 decision files on disk say promoted=true, and the throughput probes among them
        # reached that with required.min_episodes = 1 against a 500-episode stage.
        scale = (raw["stages"][stage].get("promotion_eval_episodes") or 0)
        at_scale, promoted_below_scale = [], 0
        for path in decisions:
            try:
                recorded = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            minimum = (recorded.get("required") or {}).get("min_episodes")
            promoted = bool(recorded.get("promoted"))
            if minimum is not None and minimum >= scale:
                at_scale.append(promoted)
            elif promoted:
                promoted_below_scale += 1
        passed = any(at_scale)
        out[stage] = {
            "ran": bool(dirs),
            # three distinct facts that are easy to conflate: a stage can run and
            # never record a decision (floor10), or record one that says "no" (act1)
            "decision_recorded": bool(decisions),
            "gate_passed": passed,
            # True would mean the only thing standing between this stage and a claimed
            # promotion is a probe that asked for fewer episodes than the stage requires.
            "promotion_recorded_only_below_scale": bool(promoted_below_scale) and not passed,
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
    """Check the exhaustive two-act frontier, including that the population really is exhaustive.

    The sweep's value is that it covers every checkpoint on disk, so the arithmetic of coverage is
    part of the claim: rolled plus skipped must equal what exists, and "what exists" is recounted
    here by re-running the sweep's own discovery against the live tree -- not taken from the
    artifact. The 2026-09-19 sweep said "every checkpoint on disk" with no committed way to re-derive
    that set, and by 2026-09-20 the population had grown from 84 to 102 without anything noticing,
    which is exactly how a completeness statement rots. The previous attempt also crashed on the
    first foreign-contract checkpoint and produced no artifact, so the skips are counted too.
    """
    import importlib.util

    data = json.loads((ROOT / "docs/evidence/chained_frontier_full_20260920.json")
                      .read_text(encoding="utf-8"))
    spec = importlib.util.spec_from_file_location(
        "run_chained_frontier_sweep", ROOT / "scripts/run_chained_frontier_sweep.py")
    sweep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sweep)
    live_digests = {hashlib.sha256(path.read_bytes()).hexdigest() for path in sweep.discover()}
    runs = data["chained_runs"]
    probe = (data.get("step_cap_budget_probe") or [{}])[0]
    return {
        "population_is_recounted_not_asserted": (
            len(live_digests) == data["checkpoints_found_on_disk"]
            and live_digests == set(data["checkpoint_digests"])),
        "coverage_exact": (data["rolled"] + data["skipped_contract_mismatch"]
                           == data["checkpoints_found_on_disk"]),
        "skipped_are_contract_mismatch": data["skipped_contract_mismatch"] == 9,
        "chained_into_act_two": data["chained_into_act_two"],
        "deepest_chained_floor": max(run["max_floor"] for run in runs),
        "environment_truncated_alive": sum(
            1 for run in runs
            if run["run_outcome"] == "truncated" and (run["final_player_hp"] or 0) > 0),
        "wins_anywhere": data["wins_anywhere"],
        "illegal_actions_total": data["illegal_actions_total"],
        # The one run that hit the 4,000-step cap was re-rolled at 10x: same floor, same HP, still
        # not won -- so the recorded frontier is a property of the policy, not of the budget.
        "the_frontier_is_not_budget_limited": (
            len(data["results_step_cap_hits"]) == 1
            and probe.get("steps", 0) >= 10 * data["max_steps_per_run"]
            and probe.get("max_floor") == 17 and not probe.get("run_won")),
        "population_grew_since_the_previous_sweep": (
            data["checkpoints_found_on_disk"] == 102
            and data["previous_sweep"]["digests_absent_from_the_previous_artifact"] > 0),
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
    sampled = fork["sampled_lost_runs_boss_relic_screen"]
    runs = sampled["runs"]
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
        "sampled_losses_are_internally_consistent": len(runs) == sampled["runs_sampled"],
        # The claim that matters for the unfreeze decision: no sampled loss is a stuck
        # state, every one of them is one legal action from a judged win.
        "proceed_recovers_every_sampled_loss": all(3 in row["completing_bases"] for row in runs)
                                               and sampled["proceed_completes"] == len(runs),
        "leftmost_claim_truncates_in_every_sampled_loss": all(
            0 not in row["completing_bases"] for row in runs)
            and sampled["policy_choice_in_all_runs"] == 0,
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


def claim_boss_misexit_rate():
    """Recompute the whole-partition boss numbers from the census artifact's own counts.

    The report quotes 21/86 and several Wilson intervals next to per-act arrival counts
    that were assembled from five shards, so the arithmetic is checked rather than
    trusted: each act's rows must add up to its arrivals, the pooled win counts must
    equal the per-window sums, the intervals must match their own numerator and
    denominator, and the class must be confined to generated act 2.  The independent
    `checkpoint`-split window is checked the same way, because it is what turned the
    promotion head window's 1/26 from "the rate is unquotable" into "that window is low".
    """
    from training.wilson import wilson_interval

    data = json.loads((ROOT / "docs/evidence/act2_boss_misexit_rate_20260919.json")
                      .read_text(encoding="utf-8"))
    whole = data["whole_partition"]
    windows = data["per_window"]
    acts = (whole["act1"], whole["act2"])
    adds_up = all(row["arrivals_at_floor_17"] == row["boss_win"]
                  + row["cleared_not_judged"] + row["died_in_fight"] for row in acts)
    pooled = {key: sum(windows[name][key]["arrivals_at_floor_17"] for name in windows)
              for key in ("act1", "act2")}
    kills = whole["act2"]["boss_win"] + whole["act2"]["cleared_not_judged"]
    mis_low, mis_high = wilson_interval(whole["act2"]["cleared_not_judged"], kills)
    kill_low, kill_high = wilson_interval(kills, whole["act2"]["arrivals_at_floor_17"])
    win_low, win_high = wilson_interval(whole["act2"]["boss_win"],
                                        whole["act2"]["arrivals_at_floor_17"])
    a1_low, a1_high = wilson_interval(whole["act1"]["boss_win"],
                                      whole["act1"]["arrivals_at_floor_17"])
    indep = data["independent_window_checkpoint_split"]
    pooled_block = data["pooled_two_disjoint_splits"]
    indep_kills = indep["act2"]["boss_win"] + indep["act2"]["cleared_not_judged"]
    pooled_kills = kills + indep_kills
    pooled_mis = whole["act2"]["cleared_not_judged"] + indep["act2"]["cleared_not_judged"]
    pooled_low, pooled_high = wilson_interval(pooled_mis, pooled_kills)
    ind_low, ind_high = wilson_interval(indep["act2"]["cleared_not_judged"], indep_kills)
    second = data["second_checkpoint_generality"]
    second_act2 = second["act2"]
    second_kills = second_act2["boss_win"] + second_act2["cleared_not_judged"]
    pooled_share = pooled_block["cleared_not_judged"] / pooled_kills
    gate = data["promotion_gate_impact"]
    import tomllib
    with (ROOT / "config/training_v2.toml").open("rb") as handle:
        cfg = tomllib.load(handle)
    act1 = cfg["stages"]["act1"]
    lost = whole["act2"]["cleared_not_judged"]
    return {
        "seeds_enumerated": whole["seeds"],
        "rows_add_up_per_act": adds_up and all(
            indep[key]["arrivals_at_floor_17"] == indep[key]["boss_win"]
            + indep[key]["cleared_not_judged"] + indep[key]["died_in_fight"]
            for key in ("act1", "act2")),
        "windows_sum_to_partition": pooled == {
            "act1": whole["act1"]["arrivals_at_floor_17"],
            "act2": whole["act2"]["arrivals_at_floor_17"]},
        "floor17_completed_wins_match": whole["floor17_completed_wins"] ==
                                        whole["act1"]["boss_win"] + whole["act2"]["boss_win"],
        "truncation_seed_list_matches_count": len(data["truncation_seeds"]) ==
                                              whole["act2"]["cleared_not_judged"],
        "class_confined_to_generated_act2": whole["act1"]["cleared_not_judged"] == 0
                                            and indep["act1"]["cleared_not_judged"] == 0,
        "independent_window_replicates_the_class": len(indep["truncation_seeds"]) ==
                                                   indep["act2"]["cleared_not_judged"]
                                                   and indep_kills > 0,
        "pooled_block_equals_its_parts": (pooled_block["act2_boss_kills"] == pooled_kills
                                          and pooled_block["cleared_not_judged"] == pooled_mis
                                          and pooled_block["seeds"] == whole["seeds"] + indep["seeds"]),
        "pooled_interval_recomputes": [round(pooled_low, 4), round(pooled_high, 4)]
                                      == pooled_block["wilson_mis_exit_share"],
        "windows_compatible": ind_low <= pooled_block["cleared_not_judged"] / pooled_kills <= ind_high
                              and mis_low <= pooled_block["cleared_not_judged"] / pooled_kills <= mis_high,
        "second_checkpoint_adds_up": second_act2["arrivals_at_floor_17"] == second_kills
                                     + second_act2["died_in_fight"]
                                     and second["act1"]["cleared_not_judged"] == 0,
        "second_checkpoint_is_a_different_checkpoint": (
            second["checkpoint_sha256_first16"] != data["checkpoint_sha256"][:16]
            and second["seeds"] == indep["seeds"]),
        "second_checkpoint_share_in_pooled_ci": (
            [round(x, 4) for x in wilson_interval(second_act2["cleared_not_judged"], second_kills)]
            == second_act2["wilson_mis_exit_share"]
            and second_act2["wilson_mis_exit_share"][0] <= pooled_share
            <= second_act2["wilson_mis_exit_share"][1]),
        # Read the live gate out of the config rather than trusting the artifact's copy, so a
        # future threshold change invalidates this claim instead of leaving it quietly true.
        "gate_math_uses_the_live_config": (
            gate["promotion_eval_episodes"] == act1["promotion_eval_episodes"]
            and gate["max_truncation_rate"] == act1["max_truncation_rate"]
            and gate["min_win_rate"] == act1["min_win_rate"]
            and act1["promotion_eval_episodes"] * lost / whole["seeds"]
            < act1["promotion_eval_episodes"] * act1["max_truncation_rate"]
            and not gate["would_flip_the_truncation_gate"]
            and gate["judged_clears_per_episode"] == round(
                (whole["act1"]["boss_win"] + whole["act2"]["boss_win"]) / whole["seeds"], 6)),
        "contract_clean": data["contract"]["illegal_actions_total"] == 0
                          and data["contract"]["unclassified_dead_ends_total"] == 0
                          and indep["contract"]["illegal_actions_total"] == 0
                          and indep["contract"]["unclassified_dead_ends_total"] == 0,
        "published_intervals_recompute": [
            [round(mis_low, 4), round(mis_high, 4)] == whole["wilson_mis_exit_share_of_act2_kills"],
            [round(kill_low, 4), round(kill_high, 4)] == whole["act2"]["wilson_killed_boss_given_arrival"],
            [round(win_low, 4), round(win_high, 4)] == whole["act2"]["wilson_judged_win_given_arrival"],
            [round(a1_low, 4), round(a1_high, 4)] == whole["act1"]["wilson_win_given_arrival"]],
        "head_tail_heterogeneity_is_real_in_the_data": (
            windows["head_seeds_130010000_130013499"]["act2"]["cleared_not_judged"] == 1
            and windows["tail_seeds_130013500_130019999"]["act2"]["cleared_not_judged"] == 20),
    }


def claim_boss_misexit_signature():
    """Check that every seed behind the mis-exit rate really has the shape the rate claims.

    The rate is about one specific terminal signature, so the per-run evidence is re-read
    rather than summarised: all sampled runs must conform, the seed sets must be exactly
    the ones the rate artifact counts, and each run must be judged truncated rather than a
    win.  This is what stops "21 truncations at floor 17" from quietly including a run that
    dead-ended somewhere else.
    """
    ver = json.loads((ROOT / "docs/evidence/act2_boss_misexit_signature_verified_20260919.json")
                     .read_text(encoding="utf-8"))
    rate = json.loads((ROOT / "docs/evidence/act2_boss_misexit_rate_20260919.json")
                      .read_text(encoding="utf-8"))
    blocks = ver["verification"]
    runs_a = blocks["checkpoint_a"]["runs"]
    runs_b = blocks["checkpoint_b"]["runs"]
    seeds_a = {row["seed"] for row in runs_a}
    expected_a = (set(rate["truncation_seeds"])
                  | set(rate["independent_window_checkpoint_split"]["truncation_seeds"]))
    return {
        "every_sampled_run_conforms": ver["verification_all_passed"]
                                      and all(r["signature_conforms"] for r in runs_a + runs_b),
        "seed_sets_match_the_rate_artifact": (
            seeds_a == expected_a
            and {r["seed"] for r in runs_b}
            == set(rate["second_checkpoint_generality"]["truncation_seeds"])),
        "counts_match_the_rate_artifact": (
            len(runs_a) == rate["whole_partition"]["act2"]["cleared_not_judged"]
            + rate["independent_window_checkpoint_split"]["act2"]["cleared_not_judged"]
            and len(runs_b) == rate["second_checkpoint_generality"]["act2"]["cleared_not_judged"]),
        "none_was_judged_a_win": all(not r["judged_win"] and r["run_outcome"] == "truncated"
                                     for r in runs_a + runs_b),
        "zero_illegal_across_sampled_runs": all(r["illegal_actions"] == 0 for r in runs_a + runs_b),
        "second_boss_encounter_also_affected": (
            set(blocks["checkpoint_a"]["encounters_seen"])
            < set(blocks["checkpoint_b"]["encounters_seen"])),
        # The runs are hash-linked to binaries, not to arm names: re-read the zips.
        "checkpoint_digests_match_the_files": all(
            (ROOT / meta["path"]).is_file()
            and hashlib.sha256((ROOT / meta["path"]).read_bytes()).hexdigest()
            == meta["sha256"] == blocks[name]["checkpoint_sha256"]
            for name, meta in ver["checkpoints"].items()),
        # "cleared the boss" was inferred from the phase for most of these runs; the re-roll
        # with a long watch window recorded the fight's end, so check the observation instead.
        "boss_emptied_is_directly_observed": all(
            row["no_enemies_in_final_rows"]
            and row["lowest_boss_hp_in_recorded_rows"] is not None
            and 0 < row["lowest_boss_hp_in_recorded_rows"] <= (row["boss_max_hp"] or 0)
            for block in ver["boss_emptied_direct_evidence"].values() for row in block),
        "boss_emptied_covers_every_counted_run": (
            sum(len(block) for block in ver["boss_emptied_direct_evidence"].values())
            == len(runs_a) + len(runs_b)),
        "more_than_one_act2_boss_tier_affected": (
            len({row["boss_max_hp"] for block in ver["boss_emptied_direct_evidence"].values()
                  for row in block}) > 1),
    }


def claim_chained_terminal_gates():
    """Check the objective's two gate clauses on the multi-act path, not just single-act.

    The campaign's "0 illegal / 0 unclassified" figures were all measured on generated
    single-act runs, while the objective's end state is a chained run. This re-reads the
    chained audit and also cross-checks its deepest floor against the exhaustive frontier
    artifact, so the two cannot silently disagree about how far the chain goes.
    """
    data = json.loads((ROOT / "docs/evidence/chained_terminal_gates_20260919.json")
                      .read_text(encoding="utf-8"))
    frontier = json.loads((ROOT / "docs/evidence/chained_frontier_full_20260919.json")
                          .read_text(encoding="utf-8"))
    summary = data["summary"]
    rows = data["rows"]
    return {
        "gates_clean_on_multi_act_path": summary["illegal_actions_total"] == 0
                                         and summary["unclassified_dead_ends_total"] == 0,
        "truncations_classified_and_map_exhausted": (
            summary["truncated_runs"] == 2
            and summary["truncated_all_classified_empty_mask"]
            and summary["truncated_all_zero_map_options"]
            and summary["truncated_all_at_floor"] == [19]),
        "no_chained_win": summary["wins"] == 0,
        "depth_agrees_with_the_frontier_sweep": (
            max(row["state_path"]["floor"] for row in rows)
            == max(run["max_floor"] for run in frontier["chained_runs"])),
        "checkpoint_digests_match_the_files": all(
            (ROOT / row["checkpoint"]).is_file()
            and hashlib.sha256((ROOT / row["checkpoint"]).read_bytes()).hexdigest()
            == row["checkpoint_sha256"]
            for row in rows),
    }


def claim_ladder_lineage():
    """Separate 'a ladder was configured' from 'a parent link is attestable'.

    The objective asks for a stage-by-stage warm-start ladder with hash-chained evidence, and
    the campaign report had been collapsing that into one sentence about narrative-only lineage.
    This re-reads the run's own files: both attestation mechanisms must agree with each other
    and with a SHA-256 recomputed from the parent checkpoint, and the rung census must be read
    from the live config rather than remembered.
    """
    import tomllib

    data = json.loads((ROOT / "docs/evidence/ladder_lineage_20260919.json")
                      .read_text(encoding="utf-8"))
    cfg = tomllib.loads((ROOT / "config/training_v2.toml").read_text(encoding="utf-8"))
    stages = cfg["stages"]
    configured = list(stages) if isinstance(stages, dict) else [s["name"] for s in stages]
    arms = data["attested_arms"]
    present = {stage for run in data["run_rungs_present_on_disk"].values() for stage in run}
    return {
        "attested_arms_are_mutually_consistent": all(
            a["plan_digest_matches_file"] and a["origin_digest_matches_plan"]
            and a["origin_path_matches_plan"] and a["parent_on_disk"] for a in arms),
        # Do not take the artifact's word for it: re-hash the parent checkpoints.
        "parent_digests_recompute": all(
            Path(a["plan_json_warm_start"]["initial_checkpoint"]).is_file()
            and hashlib.sha256(
                Path(a["plan_json_warm_start"]["initial_checkpoint"]).read_bytes()).hexdigest()
            == a["plan_json_warm_start"]["initial_checkpoint_sha256"]
            == a["stage_origin_json"]["initialized_from_sha256"]
            for a in arms),
        "attested_arms_count": len(arms) == 2,
        "configured_rungs_match_the_live_config": data["configured_stages_in_order"] == configured,
        "committed_rungs_are_a_subset_without_floor13": (
            present <= set(configured) and "floor13" not in present
            and {"floor3", "floor6", "floor10"} <= present),
        "campaign_arms_lack_attestation": (
            [row for row in data["campaign_arms_without_attestation"]["arms"]]
            and not any(row["has_warm_start"] or row["has_origin_json"]
                        for row in data["campaign_arms_without_attestation"]["arms"])
            and data["campaign_arms_without_attestation"]["count"]
            == len(data["campaign_arms_without_attestation"]["arms"])
            == data["campaign_arms_without_attestation"]["of_total"]),
        # The stage->stage link is exercised, not just shipped: exactly one edge exists and it
        # must be the launcher's own chaining (initialized_from_stage set, no CLI checkpoint).
        "smoke_ladder_edge_is_verifiable": (
            [e["stage"] for e in data["smoke_ladder"]["edges"]] == ["floor3", "floor6"]
            and all(e["digest_matches"] and e["parent_exists"]
                    for e in data["smoke_ladder"]["edges"])
            and data["smoke_ladder"]["edges"][1]["origin_json"]["initialized_from_stage"] == "floor3"
            and data["smoke_ladder"]["edges"][1]["origin_json"]["from_command_line_warm_start"]
            is False
            and f"{Path(data['smoke_ladder']['run']).name}/floor3/checkpoints"
            in data["smoke_ladder"]["edges"][1]["parent_path"].replace("\\", "/")),
        "smoke_ladder_stops_at_two_rungs": (
            data["smoke_ladder"]["stage_directories"] == ["floor3", "floor6"]
            and all(len(e["own_checkpoints"]) <= 3 for e in data["smoke_ladder"]["edges"])),
    }


def claim_contract_channels():
    """Recompute the three contract channels instead of quoting the policy-side one alone.

    The objective names "0 illegal actions", and on this stack that figure is partly
    structural: the default rejection mode re-asks the policy when the native layer refuses
    an advertised action, so the disagreements surface as rejection events rather than as
    illegal actions.  Both numbers, plus the legacy schema that counted them as episode
    endings, are re-derived here from every metrics file.
    """
    data = json.loads((ROOT / "docs/evidence/contract_channels_20260919.json")
                      .read_text(encoding="utf-8"))
    channels = data["channels"]
    current = legacy = 0
    events = episodes = illegal = native_current = native_legacy = 0
    for _path, payload in _metrics_payloads():
        reasons = payload.get("dead_end_reasons") or {}
        native = int(reasons.get("native_rejection") or 0)
        if "rejection_events" in payload:
            current += 1
            events += int(payload.get("rejection_events") or 0)
            episodes += int(payload.get("episodes") or 0)
            illegal += int(payload.get("illegal_actions") or 0)
            native_current += native
        else:
            legacy += 1
            native_legacy += native
    return {
        "policy_channel_is_zero": illegal == 0 and channels["illegal_actions_total_current_schema"] == 0,
        "mask_engine_disagreements_are_counted": events > 0 and events == channels["rejection_events_total_current_schema"],
        "file_population_matches": (current == channels["metrics_files_current_schema"]
                                    and legacy == channels["metrics_files_legacy_schema"]),
        "episode_total_matches": episodes == channels["episodes_current_schema"],
        "ending_channel_split_matches": (
            native_current == channels["episodes_ended_by_native_rejection_current_schema"] == 0
            and native_legacy == channels["episodes_ended_by_native_rejection_legacy_schema"] > 0),
        "per_episode_rate_matches": episodes and (
            round(events / episodes, 4) == channels["rejections_per_episode_overall"]),
    }


def claim_rejection_phase_attribution():
    """Recompute the phase attribution from its own shards, then bound what it can say.

    ``illegal_actions`` is structurally zero in filter mode, so the phase a refusal lands in
    is the only thing that tells a reader which campaign numbers the refusals can touch.
    The aggregate here is rebuilt from the committed per-shard rows rather than quoted, and
    the cross-check compares two independently computed rates over different populations:
    1,500 seeds of one checkpoint against every act1/promotion metrics file.
    """
    data = json.loads((ROOT / "docs/evidence/rejection_phase_attribution_20260919.json")
                      .read_text(encoding="utf-8"))
    from training.v2_constants import PHASE_NAMES
    agg = data["aggregation"]
    shards = data["shards"]

    def summed(key):
        out: collections.Counter = collections.Counter()
        for shard in shards:
            out.update(shard[key])
        return dict(out)

    decisions = agg["decisions_by_phase"]
    refusals = agg["refusals_by_phase"]
    campaigns = json.loads((ROOT / "docs/evidence/contract_channels_20260919.json")
                           .read_text(encoding="utf-8"))["by_stage_split"]["act1/promotion"]
    return {
        "aggregate_is_the_shard_sum": all(
            summed(key) == agg[key]
            for key in ("decisions_by_phase", "refusals_by_phase"))
            and all(sum(s[key] for s in shards) == agg[key] for key in (
                "episodes", "total_refusals", "states_with_refusals",
                "executed_after_refusal_differs")),
        "windows_are_disjoint_five_hundred_seed_slices": (
            [s["start_offset"] for s in shards] == [0, 500, 1000]
            and all(s["limit"] == 500 and s["seeds_enumerated"] == 500 for s in shards)
            and agg["episodes"] == 1500),
        "refusals_confined_to_shop_and_event": set(refusals) == {"shop", "event"},
        "combat_and_boss_reward_screens_are_refusal_free": (
            decisions["combat"] > 100_000 and decisions["relic_reward"] > 10_000
            and decisions["card_reward"] > 1_000
            and not any(p in refusals for p in ("combat", "relic_reward", "card_reward"))
            and all(not s["reward_screen_events"] for s in shards)),
        # Not "unobserved": RunPhase.cs declares 11 phases and none of them is a potion reward
        # screen, and PHASE_NAMES mirrors those 11 one-for-one, so `phase_name` can never take
        # that value. Checked against the contract, not against the sample.
        "potion_reward_is_not_an_engine_phase": (
            "potion_reward" not in decisions
            and "potion_reward" not in PHASE_NAMES and len(PHASE_NAMES) == 11),
        "the_reask_always_changed_the_action": (
            agg["executed_after_refusal_differs"] == agg["states_with_refusals"] > 0),
        "per_phase_rates_are_what_the_report_quotes": (
            round(refusals["shop"] / decisions["shop"], 4) == agg["shop_refusal_rate"]
            and round(refusals["event"] / decisions["event"], 4) == agg["event_refusal_rate"]),
        "decisions_total_is_the_sum_of_its_phases": (
            sum(decisions.values()) == agg["decisions_total"]
            and agg["reward_screen_decisions"]
            == decisions["relic_reward"] + decisions["card_reward"]),
        "cross_checks_the_campaign_counter": (
            round(agg["total_refusals"] / agg["episodes"], 3) == agg["refusals_per_episode"]
            and agg["refusals_per_episode"] > 0 and campaigns[2] > 0
            and abs(agg["refusals_per_episode"] - campaigns[2]) < 0.05),
        "single_checkpoint": (
            len({s["start_offset"] for s in shards}) == 3
            and data["checkpoint_sha256"]
            == "a1ada27ad997a425e6ad10d33c13d11f5a2f2c3a67003732a68f27cf73e57c1e"),
    }


def claim_refusal_root_cause():
    """Re-classify every refusal from the committed row table, against the engine's branches.

    The phase census said where the refusals are; this one says why, and the difference is
    worth verifying separately because the "why" is what decides whether the caveat touches
    any interpretation.  Each row is re-classified from its own recorded state rather than
    taken on trust: a shop row counts as the potion-capacity class only if the potion was
    offered, affordable, and sitting behind two full slots and one empty third slot.
    """
    data = json.loads((ROOT / "docs/evidence/refusal_root_cause_20260919.json")
                      .read_text(encoding="utf-8"))
    agg = data["aggregates"]
    rows = data["row_table"]
    handled = {int(k): v for k, v in data["event_pairs_handled_options"].items()}
    shop = [r for r in rows if r[2] == "shop"]
    event = [r for r in rows if r[2] == "event"]
    potion_class = [r for r in shop
                    if r[6] != 0 and r[4] >= r[5] and r[7] != 0 and r[8] != 0 and r[9] == 0]
    phantom = [r for r in event if r[3] not in handled.get(r[6], [])]
    precondition = [r for r in event if r[3] in handled.get(r[6], [])]
    potionless = [r for r in event if r[6] in (31, 35) and r[3] == 0
                  and all(s == 0 for s in r[7:10])]
    return {
        "row_table_backs_the_aggregates": (
            len(rows) == agg["refusals"] and len(shop) == agg["shop_refusals"]
            and len(event) == agg["event_refusals"]),
        "every_shop_refusal_is_the_potion_capacity_case": len(potion_class) == len(shop) > 0,
        "no_shop_refusal_was_unaffordable": all(r[4] >= r[5] for r in shop),
        "the_engine_disagrees_with_itself_not_with_our_wrapper": (
            agg["native_mask_advertised_the_refused_base"] == agg["refusals"]
            and agg["flat_mask_advertised_a_base_the_native_mask_has_off"] == 0
            and agg["executed_actions_outside_the_native_mask"] == 0),
        "event_split_recomputes": (
            len(phantom) == agg["event_phantom_option_rows"]
            and len(precondition) == agg["event_precondition_rows"]
            and len(phantom) + len(precondition) == len(event)),
        "the_two_named_clusters_match_their_source_branches": (
            len(potionless) == 58 and all(all(s == 0 for s in r[7:10]) for r in potionless)),
        "observed_events_are_all_inside_the_static_exposure": set(
            r[6] for r in event) <= set(handled) and bool(event),
        "static_audit_counts_are_internally_consistent": (
            data["static_event_audit"]["exposed_by_default_arm"]
            == len(json.loads((ROOT / "docs/evidence/event_mask_case_audit_20260919.json")
                              .read_text(encoding="utf-8"))["over_advertising_events"])
            and data["static_event_audit"]["declared_event_count"]
            >= data["static_event_audit"]["events_in_step_switch"]),
        "cross_checks_the_phase_census": (
            data["phase_census_cross_check"]["match"] is True
            and agg["refusals"] == 753 and agg["decisions"] == 176293),
    }


def claim_potion_slot_cost():
    """Check the phantom-slot study, including the two things that make it self-limiting.

    The artifact is honest that its own comparison is confounded, so the claim verifies the
    honesty rather than just the numbers: the exposure group must really carry more potions
    (that direction is the selection signature), and the raw Complete-phase count must really
    overstate clears, since the engine sets that phase on a loss as well.  The instrument is
    also required to reproduce the committed census by an independent path -- 162 arrivals,
    the 83/79 act split and 25 clears -- otherwise this script's boss detection is wrong and
    nothing else in the file can be trusted.
    """
    data = json.loads((ROOT / "docs/evidence/potion_slot_cost_20260919.json")
                      .read_text(encoding="utf-8"))
    agg = data["aggregates"]
    groups = data["groups"]
    rows = data["rows"]
    census = json.loads((ROOT / "docs/evidence/act1_terminal_census_3500_20260919.json")
                        .read_text(encoding="utf-8"))
    acts = json.loads((ROOT / "docs/evidence/act1_arrivals_by_act_20260919.json")
                      .read_text(encoding="utf-8"))["arrivals_by_generated_act"]
    cleared = lambda r: int(r["terminal_engine_phase"]) == 6 and bool(r["player_won"])  # noqa: E731
    arrivals = [r for r in rows if r["boss_seen"]]
    return {
        "rows_back_the_aggregates": (
            len(rows) == agg["episodes"] == 3500 and len(arrivals) == agg["arrivals"]),
        "instrument_reproduces_the_terminal_census": (
            agg["arrivals"] == census["reached_floor_17"] == 162
            and agg["cleared_arrivals"] == census["boss_win"] == 25
            and agg["arrivals_by_generated_act"]["act1_overgrowth"]
            == acts["act1_overgrowth"]["arrivals"] == 83
            and agg["arrivals_by_generated_act"]["act2_underdocks"]
            == acts["act2_underdocks"]["arrivals"] == 79
            and acts["act1_overgrowth"]["wins"] == 0
            and acts["act2_underdocks"]["wins"] == 25),
        "every_clear_is_act2_generated": (
            agg["cleared_by_generated_act"] == {"act2_underdocks": 25}
            and sum(1 for r in arrivals if cleared(r) and r["act_at_boss"] == 1) == 0),
        "phantom_slot_is_never_filled": (
            agg["phantom_third_slot_occupied_at_boss"] == 0
            and all(not r["phantom_at_boss"] for r in arrivals)
            and sum(1 for r in arrivals if r["potions_at_boss"] == 2) > 0),
        "complete_alone_would_overstate_clears": (
            agg["arrivals_ending_in_engine_phase_complete"] > 3 * agg["cleared_arrivals"]
            and sum(1 for r in arrivals
                    if int(r["terminal_engine_phase"]) == 6) ==
            agg["arrivals_ending_in_engine_phase_complete"]),
        "exposure_selects_for_potions_not_against_them": all(
            groups[f"{act}|refused_before_boss"]["mean_potions_at_boss"]
            > groups[f"{act}|no_refusal_before_boss"]["mean_potions_at_boss"]
            for act in ("act1_overgrowth", "act2_underdocks")),
        "all_four_cells_are_populated": all(
            block["n"] >= 30 for block in groups.values()) and len(groups) == 4,
        "arrival_rate_gap_is_a_selection_marker": (
            data["arrival_rate_by_exposure"]["refused_before_boss"]
            > 3 * data["arrival_rate_by_exposure"]["no_refusal_before_boss"]
            and round(sum(1 for r in rows if r["refusals_before_boss"] > 0 and r["boss_seen"])
                      / sum(1 for r in rows if r["refusals_before_boss"] > 0), 4)
            == data["arrival_rate_by_exposure"]["refused_before_boss"]),
        "shard_windows_cover_the_partition_without_overlap": (
            sorted(int(k) for k in data["shards"]) == list(range(0, 3500, 500))
            and all(s["seed_window"]["limit"] == 500 for s in data["shards"].values())),
    }


def claim_engine_findings_checklist():
    """Guard the consolidated engine-findings section against doc rot.

    The checklist is the part of the report an operator acts on, so every magnitude in it has
    to be the value its artifact carries, and every file it names has to exist.  Whitespace is
    collapsed before matching because the prose wraps mid-number.
    """
    report = (ROOT / "docs/ACT1_CAMPAIGN_2026-09-19.md").read_text(encoding="utf-8")
    header = "## 本战役查出的引擎侧问题"
    if header not in report:
        return {"section_present": False}
    section = re.sub(r"\s+", " ", report[report.index(header):])
    mis = json.loads((ROOT / "docs/evidence/act2_boss_misexit_rate_20260919.json")
                     .read_text(encoding="utf-8"))["whole_partition"]
    pot = json.loads((ROOT / "docs/evidence/potion_slot_cost_20260919.json")
                     .read_text(encoding="utf-8"))["aggregates"]
    rej = json.loads((ROOT / "docs/evidence/refusal_root_cause_20260919.json")
                     .read_text(encoding="utf-8"))["aggregates"]
    chan = json.loads((ROOT / "docs/evidence/contract_channels_20260919.json")
                      .read_text(encoding="utf-8"))["channels"]
    aud = json.loads((ROOT / "docs/evidence/event_mask_case_audit_20260919.json")
                     .read_text(encoding="utf-8"))
    census = json.loads((ROOT / "docs/evidence/reward_upgrade_availability_20260919.json")
                        .read_text(encoding="utf-8"))
    upg = census["aggregates"]
    named = ["docs/evidence/act2_boss_misexit_rate_20260919.json",
             "docs/evidence/potion_slot_cost_20260919.json",
             "docs/evidence/refusal_root_cause_20260919.json",
             "docs/evidence/event_mask_case_audit_20260919.json",
             "docs/evidence/contract_channels_20260919.json",
             "docs/evidence/reward_upgrade_availability_20260919.json"]
    odd_offers = [event for event in census["events"] if any(event["offered_upgraded"])]
    return {
        "section_present": True,
        "six_findings_listed": (
            len(re.findall(r"### \d\.", section)) == 6
            and "### 谁判定" in section),
        "upgrade_stub_numbers_match_the_census": (
            upg["card_reward_decisions"] == 12677
            and upg["decisions_with_an_upgraded_offer"] == 2
            and upg["took_an_upgraded_offer"] == 0
            and upg["episodes"] == 3500
            and upg["share_of_episodes_with_zero_upgrades"] == 0.7749
            and upg["mean_upgraded_in_final_deck"] == 0.326),
        "the_two_exceptions_are_the_hardcoded_floor_five_branch": (
            len(odd_offers) == 2 and all(event["floor"] == 5 and event["hp"] == 74
                                         and event["gold"] == 120
                                         and event["offered_upgraded"] == [1, 1, 1]
                                         for event in odd_offers)),
        # The scripted reward table is keyed on mutable state, so ordinary seeds can be served
        # from it -- but the leak is measured, not assumed big: three screens in 12,677.
        "the_scripted_leak_is_measured_not_assumed": (
            census["scripted_reward_screens"]["hits_by_floor"]
            == {"4": 1, "5": 2, "6": 0, "7": 0, "9": 0}
            and census["scripted_reward_screens"]["total_hits"] == 3),
        "misexit_magnitude_matches": (
            f"{mis['act2_boss_kills']} 次 boss 击杀" in section
            and f"{mis['act2']['cleared_not_judged']} 次没被判赢" in section
            and f"{mis['floor17_completed_wins']}" in section),
        "complete_overcount_matches": (
            f"{pot['arrivals_ending_in_engine_phase_complete']} 个以 Complete 结束" in section
            and f"{pot['arrivals']} 个 boss 到达局" in section
            and f"{pot['cleared_arrivals']}" in section),
        "phantom_slot_numbers_match": (
            f"{rej['shop_refusals']} 次拒绝全部是这一条" in section
            and f"{rej['shop_affected_distinct_seed_floor']} 家商店" in section
            and f"{pot['arrivals']} 个 boss 到达局里第三格有药水的是"
            f" **{pot['phantom_third_slot_occupied_at_boss']} 个**" in section),
        "event_exposure_numbers_match": (
            f"**{len(aud['over_advertising_events'])} / {aud['declared_event_count']} 个声明事件**" in section
            and f"{aud['events_in_mask_switch']} 个事件按条件开选项" in section
            and f"{aud['events_in_step_switch']} 个事件" in section
            and f"{rej['event_refusals']} 次" in section
            and f"{rej['event_precondition_rows']} 次前置不满足" in section
            and f"{rej['event_phantom_option_rows']} 次选项号压根不存在" in section),
        "channel_numbers_match": (
            f"{chan['rejection_events_total_current_schema']}" in section
            and f"{chan['episodes_current_schema']}" in section
            and f"{rej['refusals']}/{rej['refusals']}" in section),
        "cited_artifacts_exist": all((ROOT / path).exists() for path in named),
        "states_the_engine_was_not_edited": (
            "没有改引擎" in section and "没有关掉局内自动求解器" in section),
    }


def claim_refusal_class_generality():
    """Check the four-run generality study, including the finding it stumbled onto.

    The generality question is answered per run (both classes appear everywhere, our expansion
    is clean everywhere) and the mix is deliberately reported as unstable -- a per-episode
    refusal rate quoted from one stage does not transfer.  Event 7 is the interesting row: it
    is handled by the mask switch, so a refusal there is not the default-arm class, and it
    carries the shop bug's exact slot signature because the same faulty capacity test appears
    in its own mask arm.
    """
    data = json.loads((ROOT / "docs/evidence/refusal_class_generality_20260919.json")
                      .read_text(encoding="utf-8"))
    runs = data["per_run"]
    event7 = data["event_7_by_run"]
    stages = {run["stage"] for run in runs.values()}
    checkpoints = {run["checkpoint_sha256"] for run in runs.values()}
    return {
        "both_classes_reproduce_in_every_run": all(
            run["reasons"].get("potion_capacity_mask_mismatch", 0) > 0
            and run["reasons"].get("event_option_invalid_for_this_event", 0) > 0
            for run in runs.values()) and len(runs) == 6,
        "wrapper_is_clean_in_every_run": all(
            run["flat_wider_than_native"] == 0 and run["executed_outside_native"] == 0
            and run["native_advertised"] == run["refusals"] for run in runs.values()),
        "census_shards_still_sum_to_the_pinned_split": (
            sum(r["refusals"] for k, r in runs.items()
                if k.startswith("act1_main_census_checkpoint")) == 753),
        "more_than_one_checkpoint_and_stage": (
            len(checkpoints) == 4 and stages == {"act1", "floor6"}
            and len({k.split("|")[0] for k in runs}) == 4),
        "mix_is_not_stable": (
            max(r["reasons"]["potion_capacity_mask_mismatch"] / max(r["refusals"], 1)
                for r in runs.values()) > 0.8
            and min(r["reasons"]["potion_capacity_mask_mismatch"] / max(r["refusals"], 1)
                    for r in runs.values()) < 0.2),
        "event_7_is_the_same_capacity_bug_in_a_handled_event": (
            sum(v["rows"] for v in event7.values()) == 10
            and all(v["all_match_the_capacity_signature"] for v in event7.values() if v["rows"])
            and all({int(base) for base in v["bases"]} <= {1} for v in event7.values())),
        "capacity_pattern_site_count_matches_the_source": (
            data["capacity_pattern"]["sites_testing_capacity_as_any_empty_slot"]
            == ["RunEngine.cs:727", "RunEngine.cs:3519"]
            and data["capacity_pattern"]["sites_testing_holding_a_potion"]
            == ["RunNonCombatEffects.cs:377", "RunNonCombatEffects.cs:380"]),
    }


def claim_evidence_bundle_integrity():
    """Re-verify the evidence bundle's own hash binding, independently of the manifest.

    The report promises hash-chain evidence, and before this there was no binding over the *set*
    of artifacts -- an edited evidence file would not have disturbed anything.  Every value here
    is recomputed from the files on disk rather than read out of the manifest, which is what makes
    the manifest falsifiable: ``listing_matches_disk`` fails if anyone adds or edits an artifact
    without rebuilding, and ``manifest_excludes_itself`` fails if the manifest ever hashes itself.
    """
    manifest = json.loads((ROOT / "docs/evidence/MANIFEST_2026-09-19.json")
                          .read_text(encoding="utf-8"))
    evidence = ROOT / "docs/evidence"
    on_disk = sorted(p.name for p in evidence.glob("*.json")
                     if p.name != "MANIFEST_2026-09-19.json")
    listed = [entry["file"] for entry in manifest["files"]]
    digest_ok = all(
        hashlib.sha256((evidence / entry["file"]).read_bytes()).hexdigest() == entry["sha256"]
        and (evidence / entry["file"]).stat().st_size == entry["bytes"]
        for entry in manifest["files"] if (evidence / entry["file"]).exists())
    root = hashlib.sha256("".join(
        f"{entry['file']}  {entry['sha256']}\n"
        for entry in sorted(manifest["files"], key=lambda e: e["file"])).encode("utf-8")).hexdigest()
    checkpoints = manifest["resolved_checkpoints"]
    return {
        "listing_matches_disk": on_disk == sorted(listed),
        "digests_and_sizes_recompute": digest_ok and len(manifest["files"]) == len(on_disk),
        "bundle_root_recomputes": root == manifest["bundle_root"],
        "manifest_excludes_itself": "MANIFEST_2026-09-19.json" not in listed,
        # A count, not a boolean: the checkpoint zips live under gitignored runs/ and runtime/,
        # so on a machine without them this reads 0 and surfaces as drift instead of raising or
        # passing vacuously.
        "checkpoint_files_rehashed_here": sum(
            1 for record in checkpoints for path in record["paths"]
            if (ROOT / path).is_file()
            and hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == record["sha256"]),
        "every_citation_is_accounted_for": (
            manifest["checkpoint_digests_cited"]
            == len(checkpoints) + len(manifest["unresolved_on_this_machine"])),
        "no_citation_is_loose": all(
            record["recomputed_matches"] for record in checkpoints) and bool(checkpoints),
    }


def claim_dead_end_vocabulary():
    """Recount the dead-end labels the metrics have ever produced.

    This claim exists because the report asserted a gap that the vocabulary refutes: it said the
    labelling cannot tell "cannot act" from "acts too slowly", when `step_cap` is an assigned
    label (training/evaluation.py:129-132) and three episodes carry it. Pinning the whole
    vocabulary is the honest substitute for pinning the sentence -- if a fourth reason ever
    appears, or `step_cap` stops appearing, this goes red and the prose has to be re-read.
    """
    from collections import Counter

    data = json.loads((ROOT / "docs/evidence/dead_end_vocabulary_20260919.json")
                      .read_text(encoding="utf-8"))
    vocabulary: Counter = Counter()
    unclassified = 0
    cap_stages: set[str] = set()
    legacy_native = current_native = 0
    for _path, payload in _metrics_payloads():
        reasons = payload.get("dead_end_reasons") or {}
        vocabulary.update({k: int(v) for k, v in reasons.items()})
        unclassified += int(payload.get("unclassified_dead_ends", 0) or 0)
        if "step_cap" in reasons:
            cap_stages.add(str(payload.get("stage")))
        native = int(reasons.get("native_rejection", 0) or 0)
        if "rejection_events" in payload:
            current_native += native
        else:
            legacy_native += native
    extra_labels: Counter = Counter()
    unaccounted: list[str] = []

    def reasons_in(obj, depth=0):
        if depth > 5:
            return
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key == "dead_end_reasons" and isinstance(value, dict):
                    yield {k: int(v) for k, v in value.items()}
                else:
                    yield from reasons_in(value, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                yield from reasons_in(item, depth + 1)

    # The day the holdout census exposed a glob-limited zero, this vocabulary count got the same
    # question: does any JSON outside `**/metrics/*.json` carry a dead-end label the census never
    # saw? Files outside that glob do carry labels -- 45 hits from this session's own instruments
    # (the census and replay artifacts re-emit what they reproduced) plus 6 of `curriculum_truncated`
    # from two V1-era teacher files. So the check that matters is not "no file outside the glob has
    # a label" but "no label outside the glob is one nobody has explained".
    for pattern in ("runs/**/*.json", "runtime/**/*.json"):
        for path in sorted(ROOT.glob(pattern)):
            if "metrics" in path.parts:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            hit: Counter = Counter()
            for reasons in reasons_in(payload):
                hit.update(reasons)
            for label, count in hit.items():
                extra_labels[label] += count
                if label not in vocabulary and label != "curriculum_truncated":
                    unaccounted.append(f"{path.name}:{label}")
    return {
        "vocabulary_matches_the_artifact": dict(vocabulary) == data["vocabulary"],
        "vocabulary_is_exactly_three_labels": dict(vocabulary) == {
            "empty_action_mask": 50, "native_rejection": 861, "step_cap": 3},
        "nothing_outside_the_metrics_glob_adds_a_label_unaccounted_for": not unaccounted,
        "the_one_label_outside_the_vocabulary_is_named": (
            extra_labels["curriculum_truncated"] == 6
            and set(extra_labels) - set(vocabulary) == {"curriculum_truncated"}),
        "acts_too_slowly_is_labelled": vocabulary["step_cap"] > 0,
        "cannot_act_is_labelled": vocabulary["empty_action_mask"] > 0,
        "nothing_is_unclassified": unclassified == 0,
        "step_cap_is_act1_stage_only": cap_stages == {"act1"},
        "legacy_and_current_native_rejection_still_split": (
            legacy_native == 861 and current_native == 0),
        "artifact_and_disk_agree_on_step_cap_files": (
            len(data["step_cap_files"]) == vocabulary["step_cap"]
            and all("step_cap" in row["reasons"] for row in data["step_cap_files"])),
    }


def claim_report_exec_table_citations():
    """Every row the decision-maker reads must cite something that exists.

    The one-page table is the part of the report that actually gets read, and it drifted twice
    this session in ways the body did not: a row kept asserting a labelling gap the vocabulary
    census had just refuted, and another said "no checkpoint ever walked the cross-act branch"
    while the frontier recorded three. Claims cannot catch a stale *sentence*, but they can
    enforce the weaker invariant that keeps a row checkable at all -- each row names an evidence
    file or a section title that is really there.
    """
    report = (ROOT / "docs/ACT1_CAMPAIGN_2026-09-19.md").read_text(encoding="utf-8")
    start = report.index("| 问题 | 判定 | 出处 |")
    table = report[start:start + report[start:].index("\n\n")]
    # A row may wrap: continuation lines are indented, carry no leading pipe, and the citation
    # cell of such a row closes it on its LAST physical line. Joining them first is what makes
    # "every row cites a file" mean one row per judgement rather than one per source line.
    rows: list[str] = []
    for line in table.splitlines():
        if line.startswith("|"):
            rows.append(line)
        elif line.strip() and rows:
            rows[-1] = rows[-1] + " " + line.strip()
    rows = [row for row in rows if "---" not in row and "| 问题 |" not in row]
    headings = [re.sub(r"[*\s]+", "", heading)
                for heading in re.findall(r"^#+\s*(.+)$", report, re.M)]
    doc_stems = {path.stem for path in (ROOT / "docs").glob("*.md")}

    def cited_file_exists(name: str) -> bool:
        clean = name.strip("`").split("/")[-1]
        return any((ROOT / base / clean).exists()
                   for base in ("", "docs", "docs/evidence", "scripts"))

    unbacked = []
    for index, row in enumerate(rows):
        cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
        citation = cells[-1] if len(cells) >= 3 else ""
        quoted = [re.sub(r"[*\s]+", "", name)
                  for name in re.findall(r"[“\"]([^”\"]{2,})[”\"]", citation)]
        other_tokens = [token.strip("`") for token in re.findall(r"`([^`]+)`", citation)
                        if not token.endswith((".json", ".py", ".md"))]
        backed = (any(cited_file_exists(name)
                      for name in re.findall(r"`([^`]+\.(?:json|py|md))`", citation))
                  or any(needle in heading for needle in quoted for heading in headings)
                  or any(token in doc_stems for token in other_tokens))
        if not backed:
            head = cells[0][:22] if cells else "?"
            unbacked.append(f"row {index + 1} ({head}): citation {citation[:52]!r}")
    return {
        "row_count_is_the_expected_sixteen": len(rows) == 16,
        "every_row_has_a_verifiable_citation": not unbacked,
        "unbacked_rows": unbacked,
        "table_precedes_the_body": report.index("| 问题 | 判定 | 出处 |") < report.index("## 结论"),
        "the_two_rows_that_drifted_once_now_cite_files": (
            "chained_frontier_full_20260919.json" in rows[0]
            and "dead_end_vocabulary_20260919.json" in rows[5]
            and "solver_inventory_drift_20260919.json" in rows[-1]),
        "no_row_still_claims_the_refuted_labelling_gap": (
            "缺的是标签可分辨性，不是门槛" not in report),
    }


def claim_retraction_ledger_integrity():
    """The self-retraction ledger must stay counted, non-empty, and consistent with prose.

    This is the artifact that says what the night got wrong. It grew during the session, and the
    number quoted for it in the objective-clause audit lagged behind by one row -- the same class
    of drift as a stale verdict, just easier to prevent: parse the ledger, require three populated
    cells per row, and require every "N 行自我推翻账目" mention in the document to equal it.
    """
    report = (ROOT / "docs/ACT1_CAMPAIGN_2026-09-19.md").read_text(encoding="utf-8")
    header = "| 曾经写下 | 被什么推翻 | 现在的结论 |"
    block = report[report.index(header):]
    block = block[:block.index("\n\n")]
    rows = [[cell.strip() for cell in line.strip().strip("|").split("|")]
            for line in block.splitlines()
            if line.startswith("|") and "---" not in line and "曾经写下" not in line]
    quoted = {int(n) for n in re.findall(r"(\d+) 行自我推翻账目", report)}
    summary = {int(n) for n in re.findall(r"这\s*(\d+)\s*条里没有任何一条", report)}
    return {
        "the_ledger_row_count_is_the_pinned_number": len(rows) == 26,
        "every_row_has_three_populated_cells": all(
            len(row) == 3 and all(cell for cell in row) for row in rows),
        "no_row_is_a_bare_restatement": all(
            len(row[1]) > 8 and len(row[2]) > 8 for row in rows if len(row) == 3),
        "prose_count_matches_the_ledger": bool(quoted) and quoted == {len(rows)},
        "closing_paragraph_count_matches_the_ledger": bool(summary) and summary == {len(rows)},
        "the_objective_clause_is_in_the_ledger_or_the_audit": (
            "未达成" in report and "不能标记为完成" in report),
    }


def claim_harness_self_description():
    """What the report says about this harness has to be as checkable as what it says about runs.

    The report quotes its own claim count and its own gate-test count, and both have already
    lagged once this session. Each quote is re-derived here: the registry size, the number of
    test methods in the gate test file, and the interpreter caveat that explains why a
    contract-interpreter run prints a lower tally than a clean run.

    The evidence-file count is checked too, because the report used to quote a literal
    ``bundle_root`` there -- and the bundle contains the pinned expectations, so every re-pin
    changes that digest and silently invalidated the quote. A report must not embed a constant
    it rewrites on each run of its own harness.
    """
    report = (ROOT / "docs/ACT1_CAMPAIGN_2026-09-19.md").read_text(encoding="utf-8")
    gate_tests = len(re.findall(r"^\s+def test_",
                                (ROOT / "tests/test_report_claim_gate.py").read_text(
                                    encoding="utf-8"), re.M))
    claim_quotes = {(int(left), int(right)) for left, right in
                    re.findall(r"(\d+)/(\d+) 计分声明与盘上一致", report)}
    test_quotes = {int(n) for n in re.findall(r"test_report_claim_gate\.py`（(\d+) 项", report)}
    manifest = json.loads((ROOT / "docs/evidence/MANIFEST_2026-09-19.json")
                          .read_text(encoding="utf-8"))
    file_quotes = {int(n) for n in
                   re.findall(r"(\d+) 个[\s*]*(?:证据文件|JSON 产物|文件)", report.replace("**", ""))}
    literal_roots = [window for window in
                     (report[match.end():match.end() + 120]
                      for match in re.finditer("bundle_root", report))
                     if re.search(r"\b[0-9a-f]{16,}\b", window)]
    return {
        "report_quotes_the_live_claim_count_exactly_once": (
            claim_quotes == {(len(CLAIMS), len(CLAIMS))}),
        "gate_test_count_matches_the_quote": test_quotes == {gate_tests},
        "interpreter_caveat_is_documented": (
            "ModuleNotFoundError" in report and "训练 venv" in report),
        "an_unrunnable_claim_is_tallied_separately_in_this_code": (
            "were not scored" in (ROOT / "scripts/verify_report_claims.py").read_text(
                encoding="utf-8")),
        "evidence_file_count_matches_the_manifest": file_quotes == {manifest["evidence_file_count"]},
        "no_literal_bundle_root_survives_in_the_prose": not literal_roots,
    }


def claim_arrival_state_equivalence():
    """The cross-act comparison in the report assumes the two acts' arrivals are comparable runs.

    That was measured, not assumed: recomputed here from the committed per-seed rows are the
    reproduction counts (the same 83/79 arrivals and 25 clears as the earlier census), the nine
    cross-act resource differences with their intervals, and the within-Act-2 winner/loser
    comparison that has the statistical power Act 1 cannot have.

    """
    payload = json.loads((ROOT / "docs/evidence/arrival_state_at_boss_20260919.json")
                         .read_text(encoding="utf-8"))
    rows = payload["rows"]
    arrivals = [row for row in rows if row["arrived"]]
    resources = ("deck_size", "upgraded_in_deck", "distinct_card_defs", "relic_count", "gold",
                 "player_hp", "player_max_hp", "potions_at_boss", "shops_visited")
    cross = payload["cross_act_arrival_state"]
    win_loss = payload["act2_win_vs_loss"]
    groups = payload["groups"]

    def mean(key, sample):
        values = [row[key] for row in sample]
        return sum(values) / len(values) if values else None

    recomputed_ok = all(
        abs(cross[key]["diff"] - (mean(key, [r for r in arrivals if r["act"] == 1])
                                  - mean(key, [r for r in arrivals if r["act"] == 2]))) < 0.002
        for key in resources)
    covering = {key: block["bootstrap_ci_95"] for key, block in cross.items()}
    excludes_zero = sorted(key for key, ci in covering.items() if ci[0] > 0 or ci[1] < 0)
    return {
        "reproduction_of_the_committed_census_holds": (
            payload["reproduction_check"]["matches"] is True
            and payload["aggregates"]["arrivals_by_generated_act"]
            == {"act1_overgrowth": 83, "act2_underdocks": 79}
            and payload["aggregates"]["cleared_by_generated_act"] == {"act2_underdocks": 25}),
        "every_arrival_row_carries_every_resource": all(
            all(key in row and row[key] is not None for key in resources) for row in arrivals),
        "the_nine_cross_act_differences_recompute": recomputed_ok,
        # Direction, not just "covers zero": Act 2 being favoured would mean a difference whose
        # whole interval sits below zero (the diffs are Act-1 minus Act-2).
        "no_resource_favours_act_two": all(
            block["bootstrap_ci_95"][1] > 0 for block in cross.values()),
        "exactly_one_difference_favours_act_one": (
            excludes_zero == ["distinct_card_defs"]
            and cross["distinct_card_defs"]["diff"] > 0),
        "act_two_has_the_power_act_one_lacks": (
            groups["act2_underdocks|win"]["n"] == 25 and groups["act1_overgrowth|win"]["n"] == 0
            and payload["readable"]["threshold_met_20_arrivals_per_act_and_10_act2_wins"]),
        "arrival_resources_predict_nothing_within_act_two": all(
            block["bootstrap_ci_95"][0] <= 0 <= block["bootstrap_ci_95"][1]
            for block in win_loss.values()),
    }


def claim_upgrade_source_accounting():
    """Where an upgraded card can come from is now counted source by source, and it adds up.

    Three instruments over the same 3,500 seeds and checkpoint must agree: the reward-screen
    census, the event census and the campfire census all report the same mean upgrades per episode,
    and the event and campfire contributions are re-derived here from their per-visit rows rather
    than read out of their own aggregates. What is left over is reported as a number, because the
    claim this artifact supports is "one lever is policy-controlled", not "every upgrade is
    accounted for".
    """
    events = json.loads((ROOT / "docs/evidence/event_upgrade_opportunities_20260919.json")
                        .read_text(encoding="utf-8"))
    rest = json.loads((ROOT / "docs/evidence/rest_site_choice_20260919.json")
                      .read_text(encoding="utf-8"))
    reward = json.loads((ROOT / "docs/evidence/reward_upgrade_availability_20260919.json")
                        .read_text(encoding="utf-8"))
    realised_from_rows = sum(1 for sighting in events["sightings"]
                             if (sighting.get("upgrade_delta") or 0) > 0)
    rest_from_rows = sum(1 for visit in rest["visits"]
                         if (visit.get("upgrade_delta") or 0) > 0)
    advertised = [visit for visit in rest["visits"] if visit["legal"].get("upgrade")]
    taken = [visit for visit in advertised if visit["chosen"] == 1]
    total = sum(row["upgraded_in_final_deck"] for row in rest["per_seed"])
    return {
        "three_instruments_agree_on_upgrades_per_episode": (
            events["aggregates"]["mean_upgraded_in_final_deck"]
            == rest["aggregates"]["mean_upgraded_in_final_deck"]
            == reward["aggregates"]["mean_upgraded_in_final_deck"] == 0.326),
        "event_contribution_recomputes_from_rows": (
            realised_from_rows == 512 == events["aggregates"]["events_that_realised_an_upgrade"]),
        "campfire_contribution_recomputes_from_rows": (
            rest_from_rows == 29 == rest["aggregates"]["rest_visits_realising_an_upgrade"]),
        "the_upgrade_option_was_never_missing_at_a_campfire": (
            len(advertised) == len(rest["visits"]) == 1883
            and len(taken) == 29 and len(rest["visits"]) >= 10 * len(taken)),
        "the_residual_is_named_not_hidden": (
            total - 512 - 29 == 599
            and events["aggregates"]["episodes"] == rest["aggregates"]["episodes"] == 3500),
        "the_conditional_events_never_occurred": (
            rest["aggregates"]["episodes"] == 3500
            and events["aggregates"]["conditional_event_visits"] == 0),
        # The relic route was the named candidate for the residual and is now measured instead:
        # filter on the offered relic, because the relic_reward phase is shared with other reward
        # picks and counting screens overstates the opportunity set sixtyfold.
        "the_relic_candidate_measures_zero_because_it_was_never_offered": (
            (relic := json.loads(
                (ROOT / "docs/evidence/relic_pickup_upgrades_20260919.json")
                .read_text(encoding="utf-8")))["aggregates"]["mean_upgraded_in_final_deck"]
            == 0.326
            and len(offers := [pick for pick in relic["pickups"] if pick["offered_relic"]]) == 193
            and sum(1 for pick in offers if (pick.get("upgrade_delta") or 0) > 0) == 0
            and 201 not in {pick["offered_relic"] for pick in offers}
            and 162 not in {pick["offered_relic"] for pick in offers}
            and sum(1 for pick in offers if pick["chosen"] == 0
                    and pick["offered_relic"] in (pick.get("new_relics") or [])) == 36),
    }


def claim_upgrade_step_attribution():
    """The upgrade residual is closed by watching the deck counter, and the books have to balance.

    Four phase-scoped censuses left 599 of 1,140 upgraded cards unattributed. This one records every
    step where the upgraded count changes, in any phase, so the sum of the channels is the whole
    population -- and the arithmetic only closes if removals are tracked as well as promotions,
    which is the trap an earlier version of the instrument fell into (it overshot by 40).
    """
    payload = json.loads((ROOT / "docs/evidence/upgrade_step_attribution_20260919.json")
                         .read_text(encoding="utf-8"))
    agg = payload["aggregates"]
    increments = payload["increments"]
    adds = sum(row["added"] for row in increments)
    drops = sum(-row["added"] for row in payload["decrements"])
    ancient = sum(row["added"] for row in increments if row["phase"] == "ancient")
    event = sum(row["added"] for row in increments if row["phase"] == "event")
    select = sum(row["added"] for row in increments if row["phase"] == "transform_select")
    rows = collections.Counter(row["phase"] for row in increments)
    return {
        "books_close_with_removals": (
            agg["upgrades_present_at_reset"] + adds - drops
            == agg["upgraded_cards_final_total"] == 1140 and agg["unresolved_remainder"] == 0
            and agg["books_close"] is True),
        "nothing_is_upgraded_at_reset": agg["upgrades_present_at_reset"] == 0
            and agg["episodes_with_upgrades_at_reset"] == 0,
        "the_opening_screen_was_the_missing_channel": (ancient == 463 and rows["ancient"] == 304),
        "it_agrees_with_the_campfire_census_exactly": select == 29,
        "the_event_channel_is_bigger_than_the_source_classified_list": (
            event == 686 and rows["event"] == 599),
        "removals_are_not_zero_so_tracking_them_mattered": drops == 40,
        "fourth_instrument_on_the_same_mean": agg["mean_upgraded_in_final_deck"] == 0.326,
    }


def claim_opening_choice_position_pref():
    """The opening three-way pick is a relic pick, and which slot it sits in changes the take rate.

    463 of the population's 1,140 upgraded cards arrive through this screen, so the choice is
    worth characterising: how often an upgrade relic is offered at all, how often it is taken, and
    whether the taken rate depends on the position rather than on the relic -- which it may,
    because ``neow_options`` is one of the lists copied verbatim into the V2 observation, so the
    policy is not blind to identity.
    """
    openings = [row for row in json.loads(
        (ROOT / "docs/evidence/opening_choice_20260919.json").read_text(encoding="utf-8"))["openings"]
        if row.get("resolved")]
    upgrade_relics = (201, 162)
    offered = [row for row in openings if any(relic in upgrade_relics for relic in row["offered"])]
    def take_rate(position):
        candidates = [row for row in offered
                      if row["offered"][position] in upgrade_relics] if len(
                          [row for row in offered if len(row["offered"]) > position]) else []
        if not candidates:
            return 0, 0
        return sum(1 for row in candidates if row["chosen"] == position), len(candidates)
    took0, offers0 = take_rate(0)
    took1, offers1 = take_rate(1)
    _, offers2 = take_rate(2)
    steps = json.loads((ROOT / "docs/evidence/upgrade_step_attribution_20260919.json")
                       .read_text(encoding="utf-8"))["increments"]
    ancient_cards = sum(row["added"] for row in steps if row["phase"] == "ancient")
    observation_spec = (ROOT / "training" / "v2_observation.py").read_text(encoding="utf-8")
    return {
        "every_run_reaches_the_opening_screen": len(openings) == 3500,
        "the_upgrade_relic_is_rarely_offered_at_all": (
            len(offered) == 436 and round(len(offered) / len(openings), 3) == 0.125),
        "but_it_is_usually_taken_when_offered": (
            sum(1 for row in offered if (row.get("upgrade_delta") or 0) > 0) == 304
            and round(304 / len(offered), 3) == 0.697),
        "the_take_rate_depends_on_the_position": (
            offers2 == 0 and took0 == 202 and offers0 == 220
            and took1 == 102 and offers1 == 216),
        "cards_agree_with_the_step_attribution_census": (
            sum(row.get("upgrade_delta") or 0 for row in offered) == ancient_cards == 463),
        "the_offering_is_observable_not_hidden": '("neow_options", 3)' in observation_spec
            and '_copy("neow_options", neow_options, 3)' in observation_spec,
    }


def claim_ladder_promotion_ledger():
    """Every rung's real gate decision, re-derived with the repository's own promotion function.

    The report had been describing the ladder from the absence of parent links; this ledger reads the
    500-episode promotion records the trainer wrote and judges them with ``decide_promotion``, so a
    missing rung and a rejected rung can no longer be reported as the same thing. ``digests_re-
    hashed_here`` is a count on purpose: checkpoints live under gitignored ``runs/``, so on another
    machine it reads 0 and surfaces as drift instead of passing vacuously.

    Two verdicts per rung, deliberately apart. The ``promotion_decision.json`` the trainer wrote says
    what the ladder decided that day, under the thresholds in force that day; re-judging the metrics
    says what today's config would say about the same sample. floor6 disagrees between the two, which
    is a fact about the thresholds moving, not about the policy.
    """
    payload = json.loads((ROOT / "docs/evidence/ladder_promotion_ledger_20260919.json")
                         .read_text(encoding="utf-8"))
    agg = payload["aggregates"]
    by_stage: dict[str, list] = {}
    digests_checked = 0
    for row in payload["rows"]:
        if row.get("run_kind") == "campaign":
            # An aborted run and a 30-episode smoke ladder are not rungs of this ladder.
            by_stage.setdefault(row["stage"], []).extend(row.get("promotion_evaluations", []))
        for checkpoint in row.get("checkpoints", []):
            path = ROOT / checkpoint["file"]
            if path.is_file():
                digests_checked += int(
                    __import__("hashlib").sha256(path.read_bytes()).hexdigest()
                    == checkpoint["sha256"])
    floor6, floor3, floor10 = by_stage.get("floor6", []), by_stage.get("floor3", []), by_stage.get("floor10", [])
    # runs/ is gitignored, so on a machine without it these come out False -- a visible failure,
    # which is what a claim about local run artifacts should do rather than raising.
    live = {row["stage"]: (row.get("live_decision") or {}) for row in payload["rows"]
            if row.get("run_kind") == "campaign" and row.get("live_decision")}
    f6, f3 = live.get("floor6") or {}, live.get("floor3") or {}
    return {
        "recomputation_used_the_repos_own_gate": all(
            "reasons" in record and "observed" in record for row in payload["rows"]
            for record in row.get("promotion_evaluations", [])),
        "five_campaign_evaluations_two_promotions": (
            agg["promotion_evaluations_total"] == 5
            and agg["promotion_evaluations_promoting_total"] == 2
            and agg["promotion_evaluations_excluded_by_run_kind"] == {"aborted": 3}),
        "floor3_promoted_and_floor6_never_rejudged": (
            len(floor3) == 2 and all(record["promoted"] for record in floor3)
            and len(floor6) == 2 and not any(record["promoted"] for record in floor6)),
        "floor6_promoted_when_it_ran_under_a_lower_bar": (
            f6.get("promoted_when_it_ran") is True
            and (f6.get("thresholds_in_force_then") or {}).get("min_boundary_rate") == 0.8
            and (f6.get("observed_when_it_ran") or {}).get("boundary_rate") == 0.86
            and f6.get("the_two_verdicts_agree") is False),
        # 2026-09-01T20:32:23+08:00, commit e195e9b, which raised min_boundary_rate 0.80 -> 0.90
        # and whose own message records the floor6 promotion it was reacting to.
        "the_promotion_predates_the_raise_that_now_rejects_it": (
            f6.get("decided_at_file_mtime_utc") == "2026-09-01T12:25:09+00:00"
            and f6.get("decided_at_file_mtime_utc", "") < "2026-09-01T12:32:23+00:00"),
        "no_gate_clause_was_scored_on_an_unrecorded_field": all(
            "was not recorded" in reason
            for row in payload["rows"]
            for record in row.get("promotion_evaluations", [])
            for reason in record["reasons"]
            if reason.split(" ", 1)[0] in record["fields_absent_from_the_record"]),
        "floor3s_live_verdict_still_reproduces": (
            f3.get("promoted_when_it_ran") is True
            and f3.get("the_two_verdicts_agree") is True),
        "floor6_final_two_missed_only_on_boundary_rate": all(
            record["observed"].get("defect_truncation_rate") == 0.0
            and any(reason.startswith("boundary_rate") for reason in record["reasons"])
            for record in floor6[-2:]),
        "floor10_was_evaluated_once_and_rejected": (
            len(floor10) == 1 and floor10[0]["promoted"] is False
            and any("boundary_rate" in reason for reason in floor10[0]["reasons"])),
        "floor13_has_no_run_at_all": "floor13" in agg["stages_with_no_run_at_all"],
        "digests_rehashed_here": digests_checked,
    }


def claim_chained_map_deadend():
    """Why the only expressible two-act flow stops at Act 2 floor 19 with the player alive.

    The frontier sweep's label "lost map successors" was ambiguous between two very different
    ceilings: a policy that picked a dead-ending node, or an engine state that offers no node at
    all. The fork probe answers it by replaying to the last map decision and reading the masks, and
    these checks pin that answer -- including that the finding still stands on the engine source it
    cites, re-read through the provenance builder rather than quoted from prose.
    """
    import importlib.util

    data = json.loads((ROOT / "docs/evidence/chained_map_deadend_fork_20260920.json")
                      .read_text(encoding="utf-8"))
    forks = data["forks"]
    spec = importlib.util.spec_from_file_location(
        "build_emulator_provenance", ROOT / "scripts/build_emulator_provenance.py")
    provenance = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(provenance)
    emulator = provenance.DEFAULT_EMULATOR.resolve()

    def engine_text(file_name: str, first: int, last: int) -> str:
        found = next((path for path in (emulator / "src").rglob(file_name)
                      if "bin" not in path.parts and "obj" not in path.parts), None)
        return provenance.cited_text(found, first, last) if found else ""

    return {
        "two_alive_chains_were_forked": (
            data["aggregates"]["alive_truncated_chains"] == 2
            and all(fork["frontier_row"]["final_player_hp"] > 0 for fork in forks)),
        "the_engine_offered_no_map_option_at_any_fork": (
            data["aggregates"]["forks_where_the_engine_offered_no_map_option"] == len(forks)
            and all(fork["fork_masks_at_state"]["native_bases"] == [] for fork in forks)),
        "only_the_contracts_synthetic_action_was_advertised": all(
            fork["fork_masks_at_state"]["flat_bases"] == [32]
            and len(fork["branches"]) == 1
            and fork["branches"][0]["is_sentinel_action"]
            and fork["branches"][0]["legal_in_native_mask"] is False
            and fork["branches"][0]["ended_on_the_step_itself"] for fork in forks),
        "the_evaluator_still_classifies_the_ending": all(
            fork["evaluator_judgement"]["dead_end_reasons"] == {"empty_action_mask": 1}
            and fork["evaluator_judgement"]["unclassified_dead_ends"] == 0
            and fork["evaluator_judgement"]["illegal_actions"] == 0 for fork in forks),
        "the_chained_dead_end_is_not_a_native_refusal": all(
            len(fork["branches"]) == 1
            and fork["branches"][0]["engine_mask_was_empty_at_the_fork"] is True
            and fork["branches"][0]["native_refusal_counted_on_this_step"] is False
            and fork["branches"][0]["rejection_events_before_step"]
            == fork["branches"][0]["rejection_events_after_step"]
            and fork["branches"][0]["dead_end_label"] == "empty_action_mask" for fork in forks),
        "the_cited_engine_code_still_says_it": (
            "MapNodeTypes" in engine_text("RunEngine.cs", 690, 695)
            and "ChooseMapNode" in engine_text("RunEngine.cs", 966, 966)
            and "NodeNone" in engine_text("RunMapGenerator.cs", 1017, 1029)),
    }


def claim_chained_act2_reachability():
    """Whether a different Act-2 map choice escapes the floor-19 dead end -- searched, not assumed.

    The dead-end finding said the engine offers no option at Act 2 floor 19, and explicitly did not
    establish whether an earlier node avoids that state. This pins the search that answers it: every
    checkpoint that chains into Act 2, with the map choice varied freely at each Act-2 decision,
    the policy still playing every combat. The interesting part is what the tree is bounded by --
    `search_was_not_cut_by_its_budget` distinguishes "no route goes deeper" from "we stopped
    looking" -- and `replay_was_faithful` is the guard that the forks are really the same
    environment rather than a lookalike.
    """
    data = json.loads((ROOT / "docs/evidence/chained_act2_reachability_20260920.json")
                      .read_text(encoding="utf-8"))
    agg = data["aggregates"]
    frontier = json.loads((ROOT / data["frontier_artifact"]).read_text(encoding="utf-8"))
    chains_in_frontier = sum(1 for row in frontier["chained_runs"] if row.get("chained_into_act_two"))
    dead_end_floors = {leaf["floor"] for search in data["searches"] for leaf in search["leaves"]
                       if leaf["stop_state"] == "engine_map_dead_end"}
    return {
        "every_chain_in_the_frontier_was_searched": (
            agg["chains_searched"] == chains_in_frontier == 3
            and len(data["searches"]) == chains_in_frontier),
        "the_floor19_dead_end_is_not_a_matter_of_choice": (
            agg["engine_map_dead_ends"] == 6 and dead_end_floors == {19}),
        "no_route_reached_the_act2_boss_or_a_win": (
            agg["boss_node_states_observed"] == 0 and agg["wins"] == 0
            and agg["deepest_act_floor_over_all_routes"] == [2, 22]
            and agg["act2_floors_reached_by_any_route"] == [18, 19, 22]),
        "search_was_not_cut_by_its_budget": "search_budget" not in agg["routes_by_stop_state"],
        "replay_was_faithful": agg["replay_mismatches"] == 0,
        "every_route_ended_in_a_real_terminal_state": set(agg["routes_by_stop_state"]) <= {
            "engine_map_dead_end", "death", "win", "search_budget", "truncated",
            "no_legal_action", "step_cap", "episode_over"},
    }


def claim_map_deadend_short_circuits_before_the_refusal_census():
    """Why the census's clean map phase and the chained map dead end are the same fact, not opposites.

    The rejection census reports 0 refusals across 11,060 map decisions; the chained wall is a map
    state with no successors. Read side by side those look contradictory, and my first write-up of
    the wall resolved it the wrong way by claiming the sentinel got refused and absorbed -- the
    contract intercepts the empty-mask step before the engine is asked at all (`v2_flat_env.py:240-250`
    takes it when the policy plays the sentinel, and `v2_run_wrapper.py:241-253` holds the same
    interception in reserve), so no refusal can be counted there. This pins the reconciliation from
    measured fields (the rejection counter is identical before and after that step, and the step's
    own label is ``empty_action_mask``), plus the fact that the episode does carry refusals -- from
    other phases -- so the census is not blind to this path, it is correctly reporting that nothing
    on this path goes through the refusal route.
    """
    census = json.loads((ROOT / "docs/evidence/rejection_phase_attribution_20260919.json")
                        .read_text(encoding="utf-8"))
    shards = census["shards"]
    decisions = collections.Counter()
    refusals = collections.Counter()
    for shard in shards:
        decisions.update(shard.get("decisions_by_phase", {}))
        refusals.update(shard.get("refusals_by_phase", {}))
    deadend = json.loads((ROOT / "docs/evidence/chained_map_deadend_fork_20260920.json")
                         .read_text(encoding="utf-8"))
    forks = deadend["forks"]
    return {
        "the_census_counted_map_decisions_rather_than_omitting_them": (
            decisions.get("map", 0) > 10_000 and "map" in decisions),
        "no_map_refusals_on_the_ordinary_promotion_population": (
            refusals.get("map", 0) == 0
            and sum(refusals.values()) == sum(
                shard.get("total_refusals", 0) for shard in shards)),
        "the_chained_dead_end_sits_in_map_and_short_circuits_before_the_engine": all(
            fork["fork_state"]["phase"] == "map"
            and fork["branches"][0]["dead_end_label"] == "empty_action_mask"
            and fork["branches"][0]["native_refusal_counted_on_this_step"] is False
            and fork["evaluator_judgement"]["illegal_actions"] == 0 for fork in forks),
        # Which of the two equivalent interceptions fired is now a measured field rather than an
        # attribution in prose: only the wrapper writes the sentinel-action key, and it wrote none.
        "the_layer_that_labelled_it_is_the_flat_env": (
            deadend["labelling_layers_observed"] == ["v2_flat_env_sentinel_step"]
            and all(branch["which_contract_layer_labelled_it"] == "v2_flat_env_sentinel_step"
                    for fork in forks for branch in fork["branches"]
                    if branch["dead_end_label"] == "empty_action_mask")),
        # The episode does carry filter-mode refusals, so the census is not blind to refusals on
        # this path -- they simply belong to other states, which is what reconciles the two.
        "refusals_on_the_chained_episode_come_from_elsewhere": all(
            fork["evaluator_judgement"]["rejection_events"] > 0
            and fork["branches"][0]["rejection_events_before_step"]
            == fork["evaluator_judgement"]["rejection_events"] for fork in forks),
        "the_two_populations_disagree_as_stated": (
            len(forks) == 2 and all(
                fork["fork_masks_at_state"]["native_bases"] == [] for fork in forks)
            and refusals.get("map", 0) == 0),
        "shop_and_event_remain_the_only_ordinary_classes": (
            set(refusals) == {"shop", "event"}),
    }


def claim_empty_mask_endings():
    """Locate every `empty_action_mask` ending the campaign ever recorded, and what it was sitting on.

    The dead-end vocabulary counts 50 of these repo-wide and the report named one instance. They are
    not in one window -- they sit in 35 metrics files across 35 checkpoints, one to five per file --
    so a single window roll finds none of them (mine did: 0 in 500 episodes). The census therefore
    walks the recordings: each file's own seed window is resolved from its `seed_sha256`, its
    checkpoint must still hash to what the file recorded, and the pair is re-rolled. `per_file` then
    compares reproduced against recorded, which is what makes `0 mismatches` a closure property
    rather than a sampling claim. Two things come out of the rows that the report had no basis for
    either way: the class is one Act-2 floor-17 wall rather than a scatter, and the layer that
    labels it is the flat env's own interception, not the wrapper's.
    """
    data = json.loads((ROOT / "docs/evidence/empty_mask_endings_20260920.json")
                      .read_text(encoding="utf-8"))
    agg = data["aggregates"]
    vocab = json.loads((ROOT / "docs/evidence/dead_end_vocabulary_20260919.json")
                       .read_text(encoding="utf-8"))
    dead = data["dead_ends"]
    traces = data["per_seed_explanations"]
    walls = data["explained_walls"]
    endings = agg["endings_across_rolled_episodes"]
    return {
        "every_ending_the_campaign_ever_recorded_was_located": (
            agg["empty_action_mask_total"] == agg["recorded_total_in_rolled_files"]
            == vocab["vocabulary"]["empty_action_mask"] == 50
            and agg["per_file_mismatch_count"] == 0 and agg["files_rolled"] == 37
            and len(dead) == 50),
        # This file first reported 49 of 50 with one window "unresolvable"; that gap was the
        # census reading digests only against the current config. Resolving each run against its own
        # declared plan.json partitions closed it, so the check now pins both the zero exclusions and
        # the route that produced the last one.
        "the_former_coverage_gap_was_an_instrument_limit": (
            data["coverage_exclusions"] == []
            and sum(1 for entry in data["plan"]
                    if entry["window_source"] == "the run's own plan.json seed_partitions") == 2
            and sum(e["recorded_empty_action_mask"] for e in data["plan"])
            == vocab["vocabulary"]["empty_action_mask"]),
        "every_episode_is_counted_once": (
            sum(endings.values()) == agg["episodes_rolled"] == 12900),
        "the_engine_offered_no_action_at_any_located_ending": (
            agg["dead_ends_with_no_engine_legal_basis"] == 50
            and all(row["engine_legal_bases"] == [] for row in dead)),
        "the_player_was_alive_at_every_one": agg["alive_at_the_dead_end"] == 50,
        "the_class_is_one_wall_not_a_scatter": (
            agg["empty_action_mask_by_phase"] == {"map": 50}
            and agg["empty_action_mask_by_act"] == {"1": 1, "2": 49}
            and agg["empty_action_mask_by_floor"] == {"17": 49, "3": 1}),
        # Only V2RunEnvWrapper.step() writes the sentinel-action key, so its absence across every
        # row says the flat env's interception is the path the campaign's records actually took --
        # which is the opposite of what this report said before the rows existed.
        "the_labelling_layer_is_measured_not_assumed": (
            agg["labelling_layers_observed"] == {"v2_flat_env_sentinel_step": 50}
            and all(row["short_circuit"] is False for row in dead)),
        "the_explained_walls_are_a_boss_cleared_into_an_empty_map": (
            walls["traces"] == 5 and walls["engine_offered_nothing_at_every_explained_wall"]
            and walls["node_types_at_the_explained_walls"] == {"4": 1, "6": 4}
            and walls["won_the_last_combat_and_still_not_a_terminal"] == 5
            and all(trace["observation_at_the_wall"]["map_option_coords"] == [-1] * 8
                    for trace in traces)
            and all(trace["observation_at_the_wall"]["phase_onehot_name"] == "map"
                    for trace in traces)),
        "no_ending_was_dropped_to_make_the_finding_clean": (
            agg["roll_anomaly_total"] == len(data["anomaly_rows"])
            == sum(count for outcome, count in endings.items()
                   if outcome not in ("death", "win", "empty_action_mask"))
            and all(row["outcome"] == "step_cap" and row["steps"] == 1600
                    for row in data["anomaly_rows"])),
        "the_instrument_reports_its_own_health": (
            data["close_failures"] == []
            and "native_reset_raised" not in endings),
        # The census is not only about the empty-mask class: any truncation the evaluator can name
        # is rolled for, so `no unclassified dead end` is checked against every named class at once.
        "every_named_truncation_in_the_rolled_files_is_located": (
            agg["named_truncation_located_total"]
            == agg["named_truncation_recorded_in_rolled_files"] == 53
            and agg["named_truncation_mismatch_count"] == 0
            and agg["named_truncation_located_by_outcome"]
            == {"empty_action_mask": 50, "step_cap": 3}),
        "the_step_cap_endings_are_stalls_in_a_fight_not_at_a_boss": (
            len([row for row in data["anomaly_rows"] if row["outcome"] == "step_cap"]) == 3
            and all(row["phase"] == "combat" and row["steps"] == 1600
                    for row in data["anomaly_rows"] if row["outcome"] == "step_cap")
            and {row["floor"] for row in data["anomaly_rows"]
                 if row["outcome"] == "step_cap"} == {6, 8}),
    }


def claim_reward_fork_on_census_seeds():
    """Some of the located empty-mask endings had a winning action one click away.

    The census said 48 of 49 endings are Act 2 floor 17 map with the engine offering nothing. That is
    true *of the state the run arrived at*; the boss-completion fork happens one screen earlier, at the
    relic reward. Forking that screen on two seeds the census itself located -- not only the anecdote
    seed the mechanism was first measured on -- says which of these endings were recoverable victories,
    and the card-reward control says the decision is screen-specific rather than "anything at floor 17".
    """
    paths = ("docs/evidence/act2_reward_fork_census_seeds_relic_20260920.json",
             "docs/evidence/act2_reward_fork_census_seeds_card_20260920.json")
    relic, card = (json.loads((ROOT / path).read_text(encoding="utf-8")) for path in paths)
    census = json.loads((ROOT / "docs/evidence/empty_mask_endings_20260920.json")
                        .read_text(encoding="utf-8"))
    located = {int(row["seed"]) for row in census["dead_ends"]}

    def winners(data):
        return {row["seed"]: sorted(variant["action"] for variant in row["variants"]
                                    if variant["outcome"]["final_phase"] == "complete"
                                    and variant["outcome"]["run_won"])
                for row in data["seeds"]}

    def illegal_variants(data):
        return [variant["action"] for row in data["seeds"] for variant in row["variants"]
                if variant["action"] not in row["legal_actions"]]

    relic_wins, card_wins = winners(relic), winners(card)
    return {
        "the_forked_seeds_are_ones_the_census_located": (
            set(relic_wins) == set(card_wins) <= located and len(relic_wins) == 2),
        "the_fork_was_found_on_every_seed": all(
            row["fork_found"] for data in (relic, card) for row in data["seeds"]),
        "the_relic_screen_has_a_winning_action_for_every_seed": all(
            relic_wins[seed] for seed in relic_wins),
        "the_card_screen_is_the_control_that_does_not": all(
            not card_wins[seed] for seed in card_wins),
        "both_artifacts_fork_the_phase_they_claim": (
            relic["fork_phase"] == "relic_reward" and card["fork_phase"] == "card_reward"
            and relic["fork_floor"] == card["fork_floor"] == 17),
        "every_variant_substituted_a_legal_action_only": (
            not illegal_variants(relic) and not illegal_variants(card)),
    }


def claim_boss_clear_recoverability():
    """Every Act-2 boss clear the campaign lost had a winning action at the relic screen, untaken.

    `act2_boss_misexit_rate_20260919.json` called these runs false negatives, which assumes a win was
    available. This forks the relic-reward state of every seed that measurement recorded, at the
    checkpoint that recorded it, and asks per seed whether any legal action ends the run `complete`.
    The interesting pair of numbers is that the answer is yes for all of them and the policy's own
    choice won none of them, while the checkpoint the measurement never named still had to reproduce
    the recorded loss to be counted at all.
    """
    data = json.loads((ROOT / "docs/evidence/act2_boss_clear_recoverability_20260920.json")
                      .read_text(encoding="utf-8"))
    source = json.loads((ROOT / data["source_measurement"]["artifact"]).read_text(encoding="utf-8"))
    expected_seeds = (len(source["truncation_seeds"])
                      + len(source["independent_window_checkpoint_split"]["truncation_seeds"])
                      + len(source["second_checkpoint_generality"]["truncation_seeds"]))
    agg = data["aggregates"]
    rows = [row for group in data["groups"] for row in group["rows"]]
    return {
        "every_recorded_loss_was_forked": (
            len(rows) == expected_seeds == 34 and agg["forks_found"] == agg["rows"]),
        "a_group_counts_only_if_it_replays_the_recorded_loss": (
            len(agg["groups_in_the_pooled_verdict"]) + len(
                agg["groups_excluded_for_not_replaying_the_loss"]) == 3
            and agg["rows"] == sum(agg["rows_per_group"].values())),
        "no_lost_clear_was_structurally_unwinnable_at_that_screen": (
            agg["unrecoverable"] == 0 and agg["recoverable"] == agg["rows"]
            and all(int(count) >= 1 for count in agg["winning_action_counts"])),
        "the_policy_took_no_available_win": agg["seeds_where_policy_chose_a_winning_action"] == 0,
        "every_winning_action_was_one_the_state_offered": all(
            len(row["winning_actions"]) <= row["legal_action_count"] for row in rows),
        "the_verdict_declares_its_own_limit": any(
            "counterfactual" in line for line in data["not_established"]),
    }


def claim_boss_reward_rule_end_to_end():
    """A stated rule at one screen turns every recorded lost Act-2 boss clear into a judged win.

    The recoverability census is a one-state counterfactual; this is the same substitution applied
    inside a full episode, classified by `training/evaluation.py` rather than by the probe, on three
    populations: the 27 lost seeds at the checkpoint that recorded them, the 7 at a second arm's
    checkpoint, and an ordinary 1,000-seed slice of the promotion partition to ask what the rule
    *costs*. The population slice is the informative one even though it changes nothing: it fires on
    seven boss relic screens, all of which were already judged wins, so the rule neither manufactures
    a win where none is needed nor breaks one that is.
    """
    here = ROOT / "docs/evidence"
    lost = json.loads((here / "boss_reward_rule_on_lost_clears_20260920.json")
                      .read_text(encoding="utf-8"))
    second = json.loads((here / "boss_reward_rule_second_checkpoint_20260920.json")
                        .read_text(encoding="utf-8"))
    pop = json.loads((here / "boss_reward_rule_population_window_20260920.json")
                     .read_text(encoding="utf-8"))
    tally = pop["per_seed_tally"]
    rows = pop["per_seed"]
    matched = [row for row in rows if row["matched_screen"]]
    return {
        "the_rule_converts_every_lost_clear_in_both_checkpoint_groups": (
            lost["aggregates"]["plain_wins"] == 0 and second["aggregates"]["plain_wins"] == 0
            and lost["aggregates"]["ruled_wins"] == lost["aggregates"]["episodes"]
            and second["aggregates"]["ruled_wins"] == second["aggregates"]["episodes"]),
        "the_rule_costs_nothing_on_an_ordinary_window": (
            tally["cost_a_win"] == 0 and tally["converted_to_win"] == 0
            and pop["aggregates"]["plain_wins"] == pop["aggregates"]["ruled_wins"]),
        "every_screen_the_rule_fired_on_had_already_won": (
            len(matched) == 7 and all(row["plain_won"] and row["ruled_won"] for row in matched)),
        "the_window_had_nothing_to_convert_which_explains_the_zero_delta": (
            sum(1 for row in matched if not row["plain_won"]) == 0
            and len(rows) == pop["aggregates"]["episodes"] == 1000),
        "no_pass_bought_a_win_with_a_contract_violation": all(
            data["before_rule"]["illegal_actions"] == data["after_rule"]["illegal_actions"] == 0
            and data["before_rule"]["unclassified_dead_ends"] == 0
            and data["after_rule"]["unclassified_dead_ends"] == 0
            for data in (lost, second, pop)),
        "the_rule_is_declared_as_a_hand_written_bound": any(
            "hand-written" in line for line in pop["not_established"]),
    }


def claim_boss_reward_rule_population():
    """The rule's whole-partition number, with two closure checks against a committed measurement.

    Rolling all 10,000 promotion seeds twice -- argmax, then argmax plus the boss relic-screen rule
    -- is only meaningful if the plain pass lands where the campaign already measured: it must
    reproduce the recorded judged-win count, and the seeds the rule converts must be the same seeds
    the Act-2 mis-exit measurement listed as cleared-but-not-judged. Two instruments agreeing on a
    seed set is the claim; a larger win count on its own would prove nothing.
    """
    data = json.loads((ROOT / "docs/evidence/boss_reward_rule_promotion_partition_20260920.json")
                      .read_text(encoding="utf-8"))
    agg = data["aggregates"]
    return {
        "the_plain_pass_reproduces_the_committed_baseline": (
            agg["plain_win_matches_the_recorded_baseline"]
            and agg["plain_win_count"] == agg["recorded_baseline_win_count"] == 68),
        "the_converted_seeds_are_the_recorded_losses": (
            agg["converted_seeds_equal_the_recorded_losses"]
            and data["converted_seeds"] == sorted(data["recorded_losses_for_comparison"])
            and agg["converted_seed_count"] == 21),
        "the_rule_never_costs_a_win": (
            agg["lost_win_count"] == 0
            and agg["kept_win_count"] == agg["plain_win_count"]
            and agg["rule_win_count"] == agg["plain_win_count"] + agg["converted_seed_count"]),
        "no_win_was_bought_with_a_contract_violation": (
            agg["illegal_actions_plain"] == agg["illegal_actions_after_rule"] == 0
            and agg["unclassified_dead_ends_plain"] == 0
            and agg["unclassified_dead_ends_after_rule"] == 0),
        "the_partition_is_covered_once": (
            agg["episodes"] == 10000 and agg["slices"] == 8
            and all(len(detail["seed_source"]) > 10 for detail in data["slice_detail"])),
        "the_bound_declares_that_it_is_a_bound": any(
            "hand-written" in line for line in data["not_established"]),
    }


def claim_boss_reward_rule_generality():
    """Does the one-screen rule hold for a different policy? Two checkpoints, checked seed by seed.

    The promotion-partition result could have been an accident of one arm's policy, and the artefact
    said so. These are the other two groups the mis-exit measurement pre-registered -- the same
    3,500-seed checkpoint-partition window at the first checkpoint, and at a different arm's
    checkpoint -- each judged against the seed list and win count that measurement wrote down before
    this ran. Both reproduce the baseline exactly, convert exactly the seeds that measurement listed
    (6 and 7), and lose none.
    """
    here = ROOT / "docs/evidence"
    cp1 = json.loads((here / "boss_reward_rule_generality_cp1_20260920.json")
                     .read_text(encoding="utf-8"))
    cp2 = json.loads((here / "boss_reward_rule_generality_cp2_20260920.json")
                     .read_text(encoding="utf-8"))
    groups = []
    for data in (cp1, cp2):
        agg = data["aggregates"]
        groups.append({
            "baseline": agg["plain_win_matches_the_recorded_baseline"]
                        and agg["plain_win_count"] == agg["recorded_baseline_win_count"],
            "converted": agg["converted_seeds_equal_the_recorded_losses"]
                         and data["converted_seeds"] == sorted(
                             data["recorded_losses_for_comparison"]),
            "count": agg["converted_seed_count"],
            "clean": (agg["illegal_actions_plain"] == agg["illegal_actions_after_rule"] == 0
                      and agg["unclassified_dead_ends_plain"] == 0
                      and agg["unclassified_dead_ends_after_rule"] == 0),
            "lost": agg["lost_win_count"],
        })
    return {
        "both_groups_reproduce_their_recorded_win_baseline": all(g["baseline"] for g in groups),
        "both_groups_convert_exactly_their_recorded_seeds": all(g["converted"] for g in groups),
        "the_two_groups_are_different_policies": (
            cp1["checkpoint"] != cp2["checkpoint"]
            and cp1["compare_group"] != cp2["compare_group"]
            and [g["count"] for g in groups] == [6, 7]),
        "neither_group_costs_a_win": all(g["lost"] == 0 for g in groups),
        "no_win_was_bought_with_a_contract_violation": all(g["clean"] for g in groups),
        "the_groups_compared_are_the_pre_registered_ones": (
            cp1["compare_group"] == "independent_window_checkpoint_split"
            and cp2["compare_group"] == "second_checkpoint_generality"
            and bool(cp1["label"]) and bool(cp2["label"])
            and cp1["aggregates"]["episodes"] == cp2["aggregates"]["episodes"] == 3500),
    }


def claim_truncation_ledger():
    """`unclassified_dead_ends = 0` has to mean the books balance, not that nothing was written down.

    Every promotion decision leans on that counter, and a truncation no field describes would leave it
    at zero too. So this checks an identity over the whole committed corpus instead: truncations equal
    dead-end reasons plus unclassified plus boundary truncations (`boundary_hits` counts wins as well,
    hence subtracting them). It closes on all 216 current-schema files, and the leftover it names as
    `dead ends` is the same 53 the per-seed dead-end census locates -- two instruments, one number.
    The 54 older files are excluded on a measured basis rather than by asserting they are different.
    """
    data = json.loads((ROOT / "docs/evidence/truncation_ledger_20260920.json")
                      .read_text(encoding="utf-8"))
    agg = data["aggregates"]
    census = json.loads((ROOT / "docs/evidence/empty_mask_endings_20260920.json")
                        .read_text(encoding="utf-8"))["aggregates"]
    rows = data["rows_current_schema"]
    return {
        "the_ledger_closes_on_every_current_schema_file": (
            agg["nonzero_residual_files"] == 0 and agg["current_schema_residual_total"] == 0
            and agg["current_schema_files"] == 216
            and all(row["residual"] == 0 for row in rows)),
        "the_named_endings_equal_the_per_seed_census": (
            agg["current_schema_truncations"]
            - agg["current_schema_boundary_minus_wins_truncations"] == 53
            == census["named_truncation_located_total"]),
        "closure_does_not_lean_on_the_unclassified_counter": all(
            row["truncations"] == sum(row["dead_end_reasons"].values())
            + max(0, row["boundary_hits"] - row["wins"]) for row in rows)
            and agg["current_schema_unclassified_dead_ends"] == 0,
        "every_stage_and_schema_resolves_to_zero": (
            agg["residual_by_stage"] == {"act1/schema3": 0, "act1/schema6": 0,
                                         "floor3/schema3": 0, "floor3/schema6": 0,
                                         "floor6/schema3": 0, "floor6/schema6": 0}),
        "the_legacy_exclusion_has_a_measured_cause": (
            agg["legacy_files_excluded"] == 54
            and agg["legacy_files_missing_the_boundary_field"] == 54
            and agg["legacy_files_with_truncations"] == 46
            and agg["legacy_files_with_no_truncations_at_all"] == 8
            and agg["legacy_truncations_excluded"] == 4470),
        "the_ledger_does_not_close_because_a_field_was_blank": (
            agg["current_schema_files_missing_any_identity_field"] == 0),
    }


def claim_boss_reward_rule_holdout():
    """The reward-screen rule on the partition nothing was ever tuned on: 16 wins gained, none lost.

    Every prior instance of this result lived in the promotion partition, which the arms trained
    beside, so the +21 could have been an artifact of that window. The `act1.final` partition
    (130020000 upward) is the holdout the campaign declares but never evaluates on, and this rolls
    all 10,000 of it twice. It also shows what a holdout may and may not be claimed for: no earlier
    measurement listed seeds here, so the reconciliation keys come back None rather than quietly
    true, and the only claims are the internal ones -- the join, the arithmetic, and the contract
    counters.
    """
    data = json.loads((ROOT / "docs/evidence/boss_reward_rule_holdout_final_20260920.json")
                      .read_text(encoding="utf-8"))
    agg = data["aggregates"]
    return {
        "the_holdout_was_never_evaluated_before_this": agg["recorded_baseline_win_count"] is None
            and agg["plain_win_matches_the_recorded_baseline"] is None
            and agg["converted_seeds_equal_the_recorded_losses"] is None,
        "the_rule_converts_on_the_holdout_and_costs_nothing": (
            agg["episodes"] == 10000 and agg["slices"] == 4
            and agg["rule_win_count"] == agg["plain_win_count"] + agg["converted_seed_count"]
            and agg["converted_seed_count"] == 16 and agg["lost_win_count"] == 0
            and agg["kept_win_count"] == agg["plain_win_count"] == 67),
        # Every converted run stops being a truncation, so the fall in truncations should equal the
        # conversions exactly -- and the one truncation still left is the run the rule could not
        # rescue, which is why nothing is added here.
        "the_conversion_arithmetic_matches_the_truncation_drop": (
            agg["truncations_plain"] - agg["truncations_after_rule"]
            == agg["converted_seed_count"]
            and agg["truncations_after_rule"] == 1),
        "the_holdout_declares_the_same_clean_contracts": (
            agg["illegal_actions_plain"] == agg["illegal_actions_after_rule"] == 0
            and agg["unclassified_dead_ends_plain"] == 0
            and agg["unclassified_dead_ends_after_rule"] == 0),
        "the_holdout_plain_rate_is_in_line_with_the_tuned_window": (
            abs(agg["plain_win_count"] - 68) <= 5),
    }


def claim_boss_reward_rule_window_self_consistency():
    """Each merged reward-screen artifact must name the window its own slices rolled.

    All four carried one hardcoded `scope` sentence that named the promotion partition -- including
    the two generality artifacts, which rolled `act1/checkpoint`, and the holdout, which rolled
    `act1/final`. The prose described each correctly and `slice_detail` held the true window, so a
    reader trusting the machine-readable population field got a false statement about the seeds
    behind the numbers -- and for the holdout, which window it rolled is the entire point. The
    merger now derives the sentence from the slices and refuses to join slices from different
    partitions; this checks the artifacts rather than the source, since the artifacts are what the
    report cites.
    """
    merged = ("boss_reward_rule_promotion_partition_20260920.json",
              "boss_reward_rule_generality_cp1_20260920.json",
              "boss_reward_rule_generality_cp2_20260920.json",
              "boss_reward_rule_holdout_final_20260920.json")
    stated_windows, all_agree, reconciliation_is_backed = set(), True, True
    for name in merged:
        data = json.loads((ROOT / "docs/evidence" / name).read_text(encoding="utf-8"))
        stated = str(data["scope"]).split("every seed of the ")[-1].split(" partition")[0]
        rolled = {str(row["seed_source"]).split(" partition slice")[0]
                  for row in data["slice_detail"]}
        stated_windows.add(stated)
        all_agree = all_agree and rolled == {stated}
        group = data.get("compare_group")
        # A group other than "none" means some earlier measurement pre-registered a seed list for
        # this window; without one the reconciliation keys would be comparing against nothing.
        reconciliation_is_backed = reconciliation_is_backed and bool(
            (group not in (None, "none")) == bool(data.get("recorded_losses_for_comparison")))
    return {
        "every_merged_artifact_names_the_window_it_rolled": all_agree,
        "the_four_windows_are_not_one_partition_wearing_four_labels": len(stated_windows) >= 2,
        "a_reconciliation_group_is_backed_by_a_pre_registered_seed_list": (
            reconciliation_is_backed),
        "the_holdout_still_declares_no_baseline_to_match": (
            json.loads((ROOT / "docs/evidence/boss_reward_rule_holdout_final_20260920.json")
                       .read_text(encoding="utf-8"))["aggregates"]
            ["converted_seeds_equal_the_recorded_losses"] is None),
    }


def claim_holdout_partition_usage():
    """What has actually been evaluated on the `act1.final` partition, counted two ways.

    The report long asserted "`split = final`: 0 artifacts, so the 10,000 seeds from 130020000 are a
    clean holdout". That census walked ``**/metrics/*.json``. Widening it to every JSON under
    ``runs/`` and ``runtime/`` finds two final-split artifacts that never sat in a ``metrics/``
    directory -- so the zero was the glob's, not the corpus's. And the correction is not narrow: those
    two scans record 2 wins between four checkpoints, so no "nothing was ever selected there" survives
    either. What is left is a tail of 9,800 seeds nobody read, and the report's reward-screen result is
    split across that boundary so a reader can see the gain does not rest on the 2% that had been.
    """
    cut = datetime(2026, 9, 18, 16, 0, tzinfo=UTC)
    narrow, wide = collections.Counter(), collections.Counter()
    final_artifacts = []
    for pattern in ("runs/**/*.json", "runtime/**/*.json"):
        for path in sorted(ROOT.glob(pattern)):
            try:
                if datetime.fromtimestamp(path.stat().st_mtime, UTC) <= cut:
                    continue
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(payload, dict) or not isinstance(payload.get("split"), str):
                continue
            split = payload["split"]
            wide[split] += 1
            if "metrics" in path.parts:
                narrow[split] += 1
            if split == "final":
                seeds = payload.get("seeds") or {}
                final_artifacts.append({
                    "count": seeds.get("count"), "end": seeds.get("end"),
                    "path": path.relative_to(ROOT).as_posix(),
                    "start": seeds.get("start"),
                    "wins": sum(int(row.get("wins") or 0) for row in payload.get("results") or []
                                if isinstance(row, dict)),
                })
    from training.v2_config import load_v2_training_config

    partition = load_v2_training_config((ROOT / "config/training_v2.toml").resolve()).partition(
        "act1", "final")
    holdout = json.loads((ROOT / "docs/evidence/boss_reward_rule_holdout_final_20260920.json")
                         .read_text(encoding="utf-8"))
    covered = {tuple(sorted({int(row["start"]), int(row["end"])})) for row in final_artifacts}
    seeds_in_partition = set(partition.seeds())
    # The union, not the sum: both scans cover the same first 200 seeds, so adding their counts would
    # report a 9,600 tail and shrink the holdout by seeds nobody read twice.
    read = set()
    for row in final_artifacts:
        read.update(range(int(row["start"]), int(row["end"]) + 1))
    untouched = len(seeds_in_partition - read)
    wins_recorded = sum(row["wins"] for row in final_artifacts)
    converted = [int(seed) for seed in holdout["converted_seeds"]]
    agg = holdout["aggregates"]
    kept_all = {int(seed) for seed in holdout["kept_win_seeds"]}
    tail_kept = kept_all - read
    kept_in_read = kept_all & read
    tail_converted = set(converted) - read
    return {
        "the_narrow_glob_still_reports_zero_and_that_was_its_blind_spot": (
            narrow.get("final", 0) == 0 and wide["final"] == len(final_artifacts) > 0),
        "both_final_artifacts_are_the_same_first_200_seeds": covered == {(130020000, 130020199)},
        "the_scans_recorded_two_wins_so_nothing_can_claim_that_window_was_never_read": (
            wins_recorded == 2),
        "the_untouched_tail_is_the_partition_less_the_union_of_what_was_read": (
            partition.seeds()[0] == 130020000 and len(read & seeds_in_partition) == 200
            and untouched == 9800),
        "every_reward_rule_conversion_lies_in_the_untouched_tail": (
            len(converted) == 16 and not (read & set(converted))
            and all(130020200 <= seed <= 130029999 for seed in converted)),
        "the_holdout_label_discloses_the_prior_reads": (
            "never used by any training or evaluation record" not in str(holdout.get("label"))
            and "200" in str(holdout.get("label")) and "09-18" in str(holdout.get("label"))),
        # Splitting the window by what had been read only means something if the seed lists are the
        # same quantity the aggregates report, so the join identities are checked before the split.
        "the_seed_lists_and_the_aggregates_describe_the_same_wins": (
            len(holdout["kept_win_seeds"]) + len(converted) == agg["rule_win_count"]
            and len(holdout["kept_win_seeds"]) + len(holdout["lost_win_seeds"])
            == agg["plain_win_count"] and agg["lost_win_count"] == 0),
        "the_untouched_tail_carries_the_whole_gain_on_its_own": (
            len(tail_kept) == 66 and len(tail_converted) == 16
            and len(read & set(converted)) == 0
            and len(tail_kept) + len(tail_converted) == 82
            and len(kept_in_read) == 1 and agg["episodes"] == 10000),
    }


def claim_promotion_gate_refuses_unrecorded_inputs():
    """A gate clause whose input was never measured must be refused, not scored on a default.

    Every promotion record the campaign wrote predates ``boundary_wilson_95_low``, so rehydrating one
    and comparing the field against the stage's threshold turns an absent key into a rejection that
    reads as a measurement -- ``boundary_wilson_95_low 0.0000 < required 0.9000`` appeared in five
    reasons for a number nobody ever computed.  The mirror case is worse: an absent
    ``defect_truncation_rate`` defaults to 0.0, which is a *pass* against the truncation cap.  These
    checks run the gate, rather than reading its source, because the property is about behaviour.
    """
    from training.config import PromotionConfig
    from training.metrics import EvaluationMetrics
    from training.promotion import DEFAULTED_GATE_INPUTS, decide_promotion

    ledger = json.loads((ROOT / "docs/evidence/ladder_promotion_ledger_20260919.json")
                        .read_text(encoding="utf-8"))
    failing = next(json.loads((ROOT / entry["file"]).read_text(encoding="utf-8"))
                   for row in ledger["rows"] for entry in row.get("promotion_evaluations", [])
                   if entry["observed"].get("defect_truncation_rate", 0) > 0.2)
    gate = PromotionConfig(min_episodes=500, min_win_rate=0.0, min_wilson_lower=0.0,
                           max_truncation_rate=0.03, max_illegal_actions=0)
    without_cap = {key: value for key, value in failing.items()
                   if key != "defect_truncation_rate"}
    refused = decide_promotion(EvaluationMetrics.from_payload(without_cap), gate)
    scored = decide_promotion(EvaluationMetrics.from_payload(dict(failing)), gate)
    untouched = {key: value for key, value in failing.items() if key != "rejection_events"}
    inert = decide_promotion(EvaluationMetrics.from_payload(untouched), gate)
    rehydrated = EvaluationMetrics.from_payload(dict(failing))
    reread = EvaluationMetrics.from_payload(rehydrated.to_dict())
    return {
        "the_gate_names_the_four_clauses_at_risk": sorted(
            name for name, _ in DEFAULTED_GATE_INPUTS) == [
            "boundary_rate", "boundary_wilson_95_low", "defect_truncation_rate",
            "unclassified_dead_ends"],
        "a_real_record_is_rejected_on_its_measurement": (
            not scored.promoted and any(
                reason.startswith("defect_truncation_rate 0.") and "not recorded" not in reason
                for reason in scored.reasons)),
        "deleting_that_measurement_cannot_buy_a_pass": (
            not refused.promoted and any(
                "defect_truncation_rate" in reason and "was not recorded" in reason
                for reason in refused.reasons)),
        "the_default_is_never_printed_as_a_number": not any(
            "defect_truncation_rate 0.0000 > allowed" in reason
            for reason in refused.reasons),
        "dropping_a_field_the_gate_never_reads_changes_nothing": (
            inert.reasons == scored.reasons and inert.promoted == scored.promoted),
        "what_the_evaluator_writes_today_has_nothing_absent": (
            reread.absent_metrics == frozenset()
            and "absent_metrics" not in rehydrated.to_dict()),
    }


def claim_emulator_source_provenance():
    """Every ``*.cs:line`` citation in the report still points at the code it was read out of.

    The campaign's engine findings cite source lines in a third-party checkout that is not a git
    repository, so there is no commit to reference; the emulator's own guard only compares the built
    library's mtime against the newest source, which a preserved timestamp defeats. This recomputes
    the recorded digests with the builder's own functions: if the engine source moves, or the report
    gains a citation nobody hashed, the affected line stops resolving here rather than silently
    re-pointing at different code.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "build_emulator_provenance", ROOT / "scripts/build_emulator_provenance.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)

    payload = json.loads((ROOT / "docs/evidence/emulator_source_provenance_20260920.json")
                         .read_text(encoding="utf-8"))
    emulator = builder.DEFAULT_EMULATOR.resolve()
    report = (ROOT / "docs/ACT1_CAMPAIGN_2026-09-19.md").read_text(encoding="utf-8")
    live = {(name, int(first), int(last or first))
            for name, first, last in builder.CITATION.findall(report)}
    recorded = {(c["file"], c["first_line"], c["last_line"]) for c in payload["citations"]}
    library = emulator / "out" / "Sts2Emulator.dll"
    sources = builder.source_files(emulator)
    by_name = {f["file"]: emulator / f["path"] for f in payload["cited_files"]}
    return {
        "the_report_and_the_snapshot_agree_on_every_citation": live == recorded,
        "every_citation_resolves_in_the_recorded_build": (
            payload["aggregates"]["citations_resolved"]
            == payload["aggregates"]["citation_mentions"] == len(payload["citations"])),
        "cited_files_match_their_recorded_digests": bool(by_name) and all(
            path.is_file() and builder.digest_file(path) == f["sha256"]
            for f in payload["cited_files"] for path in [by_name[f["file"]]]),
        "cited_lines_still_hold_the_recorded_text": all(
            hashlib.sha256(builder.cited_text(
                by_name[c["file"]], c["first_line"], c["last_line"]).encode("utf-8")).hexdigest()
            == c["text_sha256"] for c in payload["citations"] if c["file"] in by_name),
        "engine_source_tree_digest_recomputes": bool(sources) and (
            builder.tree_digest(emulator, sources) == payload["source_tree_digest"]
            and len(sources) == payload["aggregates"]["engine_source_files_hashed"]),
        "native_library_digest_recomputes": (
            library.is_file() == payload["native_api"]["library"]["present"]
            and builder.digest_file(library) == payload["native_api"]["library"]["sha256"]),
    }


def claim_objective_clause_audit():
    """Keep the clause-by-clause audit honest: nine clauses, and the failing one stays failing.

    A summary table is the easiest place for a claim of completion to creep in, so this checks
    the audit against the things it cites (files on disk, claims registered in CLAIMS) and
    against itself: the Act 1-3 row must still read as not achieved, the warm-start ladder row as
    partly achieved, and the closing sentence must still say the goal cannot be marked complete.
    Those three are the load-bearing parts of the report's scope statement; if any flips while
    the underlying artifacts are unchanged, the flip is the finding.
    """
    report = (ROOT / "docs/ACT1_CAMPAIGN_2026-09-19.md").read_text(encoding="utf-8")
    header = "## 目标条款逐条对账"
    if header not in report:
        return {"audit_section_present": False}
    section = report[report.index(header):]
    rows = []
    for line in section.splitlines():
        if line.startswith("|") and "---" not in line and "目标条款" not in line:
            rows.append([cell.strip() for cell in line.strip("|").split("|")])
    verdicts = [row[1] for row in rows if len(row) > 1]
    cited_files = {name for row in rows if len(row) > 3
                   for name in re.findall(r"`([^`]+\.json)`", row[3])}
    cited_claims = {name for row in rows if len(row) > 4
                    for name in re.findall(r"`([a-z0-9_]+)`", row[4])}

    def numbers(cell: str) -> set[int]:
        return {int(text.replace(",", "")) for text in re.findall(r"\d[\d,]*", cell)}

    def arm_counts(cell: str) -> set[int]:
        # A subset test alone missed one case: the fan-out row names the arm count twice, so
        # rewriting only the first still left a 21 somewhere in the cell. Every place that row
        # states the count has to say the same thing.
        return {int(text) for text in re.findall(
            r"(\d+) 个(?:带 plan\.json 的)?(?:臂运行| plan)", cell)}

    fanout = json.loads((ROOT / "docs/evidence/fanout_attestation_20260919.json")
                        .read_text(encoding="utf-8"))["aggregates"]
    manifest = json.loads((ROOT / "docs/evidence/MANIFEST_2026-09-19.json")
                          .read_text(encoding="utf-8"))
    channels = json.loads((ROOT / "docs/evidence/contract_channels_20260919.json")
                          .read_text(encoding="utf-8"))["channels"]
    vocabulary = json.loads((ROOT / "docs/evidence/dead_end_vocabulary_20260919.json")
                            .read_text(encoding="utf-8"))
    vocab_counts = set(vocabulary.get("vocabulary", vocabulary.get("counts", {})).values())
    clause_numbers = {index: numbers(row[2]) for index, row in enumerate(rows) if len(row) > 2}
    def _numbers(*paths):
        out = []
        for path in paths:
            out.append(json.loads((ROOT / path).read_text(encoding="utf-8"))["aggregates"])
        return out

    ledger, mask_census, holdout = _numbers(
        "docs/evidence/truncation_ledger_20260920.json",
        "docs/evidence/empty_mask_endings_20260920.json",
        "docs/evidence/boss_reward_rule_holdout_final_20260920.json")
    return {
        "audit_section_present": True,
        "all_nine_clauses_listed": len(rows) == 9,
        "act_1_to_3_clause_still_says_not_achieved": bool(verdicts) and verdicts[0].startswith(
            "**未达成"),
        "warm_start_ladder_still_says_partly": len(verdicts) > 5
        and verdicts[5].startswith("**部分达成"),
        "the_other_six_are_marked_achieved_with_limits": sum(
            1 for verdict in verdicts if "**达成" in verdict) == 6
        and len(verdicts) == 9
        and "第一幕达成" in verdicts[1] and "未达成" in verdicts[0]
        and "部分达成" in verdicts[5],
        "every_cited_file_exists": bool(cited_files) and all(
            (ROOT / "docs/evidence" / name).exists() or (ROOT / "docs" / name).exists()
            or (ROOT / name).exists() for name in cited_files),
        "every_cited_claim_is_registered": cited_claims <= set(CLAIMS) and bool(cited_claims),
        # The rows' verdicts were already pinned; their numbers were not, which is how a summary
        # table keeps quoting a superseded count while every existence check still passes.
        "fan_out_row_numbers_match_the_attestation": (
            {21, 118, 190, 110} <= clause_numbers.get(6, set())
            and arm_counts(rows[6][2] if len(rows) > 6 else "") == {21}
            and (fanout["arms_with_plans"], fanout["metrics_files_covered_by_the_index"],
                 fanout["window_overlap_pairs_possible"], fanout["window_overlap_pairs"])
            == (21, 118, 190, 110)),
        "hash_chain_row_numbers_match_the_manifest": (
            {manifest["evidence_file_count"], manifest["checkpoint_digests_cited"]}
            <= clause_numbers.get(7, set())),
        "zero_illegal_row_numbers_match_the_channel_census": (
            {channels["episodes_current_schema"],
             channels["rejection_events_total_current_schema"]} <= clause_numbers.get(2, set())),
        "dead_end_row_numbers_match_the_vocabulary_census": (
            vocab_counts <= clause_numbers.get(3, set())
            and vocab_counts == {50, 861, 3}),
        # The dead-end row used to lean on a counter that reads 0 whether the books balance or no
        # bookkeeping exists, so the row now has to carry the tested population, and those figures
        # have to be the ledger's own.
        "dead_end_row_cites_the_population_the_identity_tested": (
            {4383, 216, 53} <= clause_numbers.get(3, set())
            and ledger["current_schema_truncations"] == 4383
            and ledger["current_schema_files"] == 216
            and mask_census["named_truncation_located_total"] == 53
            == ledger["current_schema_truncations"]
            - ledger["current_schema_boundary_minus_wins_truncations"]),
        "the_act13_row_names_the_measured_near_miss_without_softening_the_verdict": (
            16 in clause_numbers.get(0, set())
            and holdout["converted_seed_count"] == 16 and holdout["lost_win_count"] == 0
            and "未达成" in rows[0][1]),
        "conclusion_still_refuses_completion": "不能标记为完成" in section,
    }


def claim_metrics_index_reviewability():
    """Whether the campaign's headline counts can be re-derived from committed content alone.

    The raw evaluation dumps live under ``runs/`` and ``runtime/``, which are gitignored, so the
    claims that walk them are only reproducible on the machine that ran the campaign. This claim is
    the portable half: it recomputes the aggregates *from the committed index*, then checks them
    against the evidence artifacts that quote them -- no gitignored file is read at all, so a fresh
    clone gets the same answer here.

    The companion fact, ``files_rehashed_here``, is deliberately a number rather than a boolean:
    on the campaign machine it equals the row count, and on a clean clone it reads 0 and shows up
    as drift, which is the honest signal ("that check needs the originals") rather than a vacuous
    pass.
    """
    index = json.loads((ROOT / "docs/evidence/metrics_index_20260919.json")
                       .read_text(encoding="utf-8"))
    rows = index["rows"]
    aggregates = index["aggregates"]
    current = [row for row in rows if row["has_rejection_events_field"]]
    recomputed = {
        "episodes_current_schema": sum(int(row["episodes"] or 0) for row in current),
        "illegal_actions_current_schema": sum(
            int(row["illegal_actions"] or 0) for row in current),
        "metrics_files": len(rows),
        "metrics_files_current_schema": len(current),
        "metrics_files_legacy_schema": len(rows) - len(current),
        "rejection_events_current_schema": sum(
            int(row["rejection_events"] or 0) for row in current),
        "unclassified_dead_ends_total": sum(
            int(row["unclassified_dead_ends"] or 0) for row in rows),
    }
    vocabulary: collections.Counter = collections.Counter()
    for row in rows:
        vocabulary.update({k: int(v) for k, v in row["dead_end_reasons"].items()})
    channels = json.loads((ROOT / "docs/evidence/contract_channels_20260919.json")
                          .read_text(encoding="utf-8"))["channels"]
    dead_end = json.loads((ROOT / "docs/evidence/dead_end_vocabulary_20260919.json")
                          .read_text(encoding="utf-8"))
    files_rehashed_here = sum(1 for row in rows
                              if (ROOT / row["path"]).is_file()
                              and hashlib.sha256((ROOT / row["path"]).read_bytes()).hexdigest()
                              == row["sha256"])
    return {
        "index_aggregates_are_self_consistent": recomputed == {
            key: aggregates[key] for key in recomputed},
        "index_backs_the_contract_channel_numbers": (
            aggregates["episodes_current_schema"]
            == channels["episodes_current_schema"] == 39891
            and aggregates["rejection_events_current_schema"]
            == channels["rejection_events_total_current_schema"] == 18160
            and aggregates["illegal_actions_current_schema"]
            == channels["illegal_actions_total_current_schema"] == 0
            and aggregates["metrics_files_current_schema"]
            == channels["metrics_files_current_schema"]
            and aggregates["metrics_files_legacy_schema"] == channels["metrics_files_legacy_schema"]
            and aggregates["unclassified_dead_ends_total"] == 0),
        "index_backs_the_dead_end_vocabulary": (
            dict(vocabulary) == dead_end["vocabulary"]
            == aggregates["dead_end_vocabulary"]),
        "every_row_names_a_committed_relative_path": all(
            not Path(row["path"]).is_absolute() and ".." not in Path(row["path"]).parts
            for row in rows),
        "files_rehashed_here": files_rehashed_here,
    }


def claim_fanout_overnight_attestation():
    """Check the objective's "multi-arm concurrent overnight run" from committed files only.

    Deliberately touches nothing under ``runtime/``: it reads the attestation and the metrics
    index, both committed, so it answers on a fresh clone too. The two numbers here that look
    like flaws are the point of recording them -- only 2 of 21 plans carry a ``warm_start`` block
    (the rest predate it, so their parent links rest on launcher text), and the concurrency
    evidence is overlapping mtime windows rather than a measured wall-clock fact.
    """
    attestation = json.loads((ROOT / "docs/evidence/fanout_attestation_20260919.json")
                             .read_text(encoding="utf-8"))
    index = json.loads((ROOT / "docs/evidence/metrics_index_20260919.json")
                       .read_text(encoding="utf-8"))
    indexed = {row["path"] for row in index["rows"]}
    aggregates = attestation["aggregates"]
    planned = [row for row in attestation["rows"] if row.get("plan_found")]
    every_path_listed = {path for row in planned for path in row.get("metrics_files", [])}
    first, last = aggregates["campaign_span_first_activity"], aggregates["campaign_span_last_activity"]
    hours = (datetime.fromisoformat(last) - datetime.fromisoformat(first)).total_seconds() / 3600
    return {
        "planned_arm_runs_are_twenty_one": len(planned) == 21,
        "every_plan_row_carries_a_digest": all(
            re.fullmatch(r"[0-9a-f]{64}", row.get("plan_sha256") or "") for row in planned),
        "every_metrics_path_is_in_the_committed_index": (
            every_path_listed <= indexed
            and aggregates["metrics_files_listed"]
            == aggregates["metrics_files_covered_by_the_index"] == len(every_path_listed) > 0),
        "the_window_crosses_the_night": first.startswith("2026-09-18T1") and hours >= 6.0
        and last.startswith("2026-09-19T0"),
        "windows_overlap_so_arms_ran_together": (
            0 < aggregates["window_overlap_pairs"] < aggregates["window_overlap_pairs_possible"]),
        "fanout_shape_is_per_run_parallel_envs_12": all(
            row.get("parallel_envs") == 12 for row in planned),
        # 19 arms ran the act1 stage; the other 2 are the floor3 -> floor6 ladder, which is
        # the same evidence the ladder clause cites, so state the split rather than assuming
        # every arm targeted act1.
        "stage_shapes_are_19_act1_plus_the_two_rung_ladder": (
            sum(1 for row in planned if row.get("stages") == ["act1"]) == 19
            and sum(1 for row in planned
                    if row.get("stages") == ["floor3", "floor6"]) == 2),
        "the_warm_start_attestation_gap_is_still_two_of_twenty_one": (
            aggregates["plans_recording_warm_start"] == 2),
        # The gap has a shape, and it is not "13 arms": 17 act1-stage runs plus the two ladder
        # runs, against the two 2026-09-19 A/B arms that do attest.
        "attestation_gap_breakdown_is_17_plus_2_vs_2": (
            sum(1 for row in planned if row.get("stages") == ["act1"]
                and not row.get("warm_start_recorded")) == 17
            and sum(1 for row in planned if row.get("stages") == ["floor3", "floor6"]
                and not row.get("warm_start_recorded")) == 2),
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
    "boss_misexit_rate": (claim_boss_misexit_rate,
                          "whole-partition boss kills, judged wins, and the ones the engine lost"),
    "boss_misexit_signature": (claim_boss_misexit_signature,
                               "every seed behind that rate really ends at a cleared boss node"),
    "chained_terminal_gates": (claim_chained_terminal_gates,
                               "the objective's two gate clauses, checked on the multi-act path"),
    "ladder_lineage": (claim_ladder_lineage,
                        "which warm-start parent links are attestable from a run's own files"),
    "contract_channels": (claim_contract_channels,
                          "illegal actions vs mask/engine disagreements, recomputed apart"),
    "rejection_phase_attribution": (claim_rejection_phase_attribution,
                                    "which phases the hidden refusals land in, rebuilt from its shards"),
    "refusal_root_cause": (claim_refusal_root_cause,
                           "why each refusal happens, re-classified row by row against the engine"),
    "potion_slot_cost": (claim_potion_slot_cost,
                         "the phantom potion slot, and why this study cannot price it"),
    "engine_findings_checklist": (claim_engine_findings_checklist,
                                "the operator-facing engine list, against its artifacts"),
    "refusal_class_generality": (claim_refusal_class_generality,
                                 "the refusal classes on three checkpoints and two stages"),
    "evidence_bundle_integrity": (claim_evidence_bundle_integrity,
                               "the whole docs/evidence bundle, re-hashed from disk"),
    "dead_end_vocabulary": (claim_dead_end_vocabulary,
                             "every dead-end label the metrics have ever produced"),
    "report_exec_table_citations": (claim_report_exec_table_citations,
                                   "each decision-table row cites something that exists"),
    "objective_clause_audit": (claim_objective_clause_audit,
                              "the goal clause by clause, with the failing clause still failing"),
    "retraction_ledger_integrity": (claim_retraction_ledger_integrity,
                                 "the self-retraction ledger, counted and consistent with the prose"),
    "metrics_index_reviewability": (claim_metrics_index_reviewability,
                                 "headline counts recomputed from committed content alone"),
    "fanout_overnight_attestation": (claim_fanout_overnight_attestation,
                                  "the multi-arm overnight run, from committed files only"),
    "harness_self_description": (claim_harness_self_description,
                                 "the report's own numbers about this harness, re-derived"),
    "arrival_state_equivalence": (claim_arrival_state_equivalence,
                                  "the two acts' boss arrivals are comparable runs"),
    "opening_choice_position_pref": (claim_opening_choice_position_pref,
                                     "the run-start relic pick, offered rarely and positionally"),
    "ladder_promotion_ledger": (claim_ladder_promotion_ledger,
                                "each rung's real gate decision, re-derived"),
    "map_deadend_short_circuits_before_the_refusal_census": (
        claim_map_deadend_short_circuits_before_the_refusal_census,
        "map refusals belong to the chained branch; on ordinary seeds the phase is measured clean"),
    "holdout_partition_usage": (
        claim_holdout_partition_usage,
        "what has really been evaluated on the act1.final partition, counted with both globs"),
    "boss_reward_rule_window_self_consistency": (
        claim_boss_reward_rule_window_self_consistency,
        "each merged rule artifact states the partition its own slices actually rolled"),
    "boss_reward_rule_holdout": (
        claim_boss_reward_rule_holdout,
        "the rule on the untouched final partition: wins gained, none lost"),
    "boss_reward_rule_generality": (
        claim_boss_reward_rule_generality,
        "the same rule at two other checkpoints, against pre-recorded seed lists"),
    "boss_reward_rule_population": (
        claim_boss_reward_rule_population,
        "the whole promotion partition, checked against the recorded baseline seed for seed"),
    "boss_reward_rule_end_to_end": (
        claim_boss_reward_rule_end_to_end,
        "a hand-written reward-screen rule, judged by the campaign evaluator"),
    "boss_clear_recoverability": (
        claim_boss_clear_recoverability,
        "were the lost Act-2 boss clears winnable at the screen before the wall"),
    "reward_fork_on_census_seeds": (
        claim_reward_fork_on_census_seeds,
        "whether the located endings had a winning action available one screen earlier"),
    "truncation_ledger": (
        claim_truncation_ledger,
        "do the campaign's truncations add up, or is a counter just quiet"),
    "empty_mask_endings": (
        claim_empty_mask_endings,
        "where every recorded empty_action_mask ending sits, and what the engine was showing there"),
    "chained_act2_reachability": (
        claim_chained_act2_reachability,
        "whether a different Act-2 map choice escapes the floor-19 dead end (it does not)"),
    "chained_map_deadend": (
        claim_chained_map_deadend,
        "why the two-act flow ends alive at floor 19: the engine offers no map option there"),
    "promotion_gate_refuses_unrecorded_inputs": (
        claim_promotion_gate_refuses_unrecorded_inputs,
        "a gate clause is refused when its input was never measured, not scored on a default"),
    "emulator_source_provenance": (
        claim_emulator_source_provenance,
        "every *.cs:line citation still points at the hashed engine build it was read from"),
    "upgrade_step_attribution": (claim_upgrade_step_attribution,
                                 "every upgraded card, attributed to the step that made it"),
    "upgrade_source_accounting": (claim_upgrade_source_accounting,
                                  "where the policy's upgrades come from, counted per source"),
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
    # A claim that raises is usually this interpreter missing a training-side package, not the
    # report drifting, so it is tallied separately; the exit code stays non-zero either way so a
    # run on the contract interpreter can never read as clean.
    unrunnable = []
    # An expectation is a snapshot, so a check that came out False and got pinned would pass
    # forever and read as "N/N match the disk". A False check is therefore drift on its own,
    # unless the run declares it deliberate in _expected_false_checks (currently unused).
    deliberate = expectations.get("_expected_false_checks", {})
    for name, (fn, note) in CLAIMS.items():
        try:
            actual = fn()
        except Exception as error:  # a broken claim is reported, not swallowed
            print(f"ERROR     {name}: {type(error).__name__}: {error}")
            unrunnable.append(name)
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
        failed = sorted(key for key, value in (actual.items() if isinstance(actual, dict) else ())
                        if value is False and key not in deliberate.get(name, ()))
        if status == "ok" and failed:
            status = "FALSE_CHECK"
            drift += 1
        if status != "ok" or not args.quiet:
            print(f"{status:11} {name}: {json.dumps(actual, sort_keys=True)}")
            if status == "DRIFT":
                print(f"          expected {json.dumps(expected, sort_keys=True)}")
            if failed:
                print(f"          false checks: {failed}")
            print(f"          {note}")
    ran = len(CLAIMS) - len(unrunnable)
    print(f"\n{ran - drift}/{ran} scored claims match the disk ({len(CLAIMS)} registered)")
    if unrunnable:
        print(f"          {len(unrunnable)} claim(s) raised and were not scored: "
              f"{', '.join(unrunnable)}")
        print("          an import error here means the wrong interpreter: this harness needs the "
              "training venv (see scripts/test.ps1 STS2_TRAINING_PYTHON), not .venv or system python")
    return 1 if (drift or unrunnable) else 0


if __name__ == "__main__":
    raise SystemExit(main())
