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
