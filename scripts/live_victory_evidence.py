"""Build the evidence file for the live client's act-1-to-victory trace.

The delivery goal is one shape: three acts, each act's own Ancient, and that act's bosses, on the
installed build, decided by our out-of-combat policy while combat belongs to the mod's solver.
That claim is either readable off the recorded trace or it is not made, so this script derives
every field from the JSONL rather than restating a summary: the act timeline, the floors, the
enemy entity ids at each boss node, which events carried ``is_ancient``, who owned combat, which
policy version decided the out-of-combat picks, and the client's own terminal flag.

It reports the whole batch, not the winner alone.  A trace with one victory and five deaths in the
same batch is the denominator this file has to carry, otherwise the one run reads as a rate.

    .tools/python/.../python.exe scripts/live_victory_evidence.py \
        --trace runs/solver_supervisor/<batch>/autoplay_trace.jsonl \
        --out docs/evidence/live_victory_20261007.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: The act each Ancient belongs to, taken from the build's own act tables
#: (``training/campaign_content.py``), not inferred from where the run happened to be.
ANCIENT_ACT = {"NEOW": 1, "OROBAS": 2, "PAEL": 2, "TEZCATARA": 2,
               "NONUPETRA": 3, "TANX": 3, "VAKUU": 3}


def _iter_rows(trace: pathlib.Path):
    with trace.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def _run_windows(rows):
    """Split the trace into runs at each fresh act-1 floor-0/1 start after the menu."""
    runs, current = [], None
    for row in rows:
        raw = row.get("raw") or {}
        run = raw.get("run") or {}
        state = raw.get("state_type")
        if state == "menu":
            if current and current["frames"]:
                runs.append(current)
            current = {"frames": [], "ended_with": None}
            continue
        if current is None:
            current = {"frames": [], "ended_with": None}
        current["frames"].append(row)
        if state == "game_over":
            current["ended_with"] = "game_over"
    if current and current["frames"]:
        runs.append(current)
    return runs


def _summarise(window: dict) -> dict:
    frames = window["frames"]
    acts: dict[int, dict] = {}
    bosses: dict[tuple, list] = {}
    ancients: dict[str, dict] = {}
    victory = None
    hp_at_end = None
    for row in frames:
        raw = row["raw"]
        run = raw.get("run") or {}
        act, floor = run.get("act"), run.get("floor")
        state = raw.get("state_type")
        if isinstance(act, int) and isinstance(floor, int) and floor > 0:
            # Floor 0 is what the client reports on the menu frames around a run start, and the
            # act field is already populated there.  Counting it would make a run look like it
            # opened on floor 0.
            seen = acts.setdefault(act, {"first_ts": row["timestamp_utc"], "floors": set()})
            seen["last_ts"] = row["timestamp_utc"]
            seen["floors"].add(floor)
        battle = raw.get("battle") or {}
        enemies = [(e.get("entity_id"), e.get("hp")) for e in (battle.get("enemies") or [])]
        if state in ("boss", "elite", "monster") and enemies and isinstance(act, int):
            key = (act, floor, state)
            entry = bosses.setdefault(key, {"entities": [], "all_seen": set(),
                                            "first_ts": row["timestamp_utc"],
                                            "max_hp_seen": {}})
            ids = [e[0] for e in enemies if e[0]]
            # Keep the widest party ever seen on this node, not the last frame's.  The act-2 boss
            # is a pair, and the frame after one of them dies lists only the survivor -- reading
            # the last frame would report a double boss as a single one.
            if len(ids) >= len(entry["entities"]):
                entry["entities"] = ids
            entry["all_seen"].update(ids)
            for eid, hp in enemies:
                if eid:
                    entry["max_hp_seen"][eid] = max(entry["max_hp_seen"].get(eid, 0), hp or 0)
            entry["last_ts"] = row["timestamp_utc"]
        event = raw.get("event") or {}
        if isinstance(event, dict) and event.get("is_ancient") and event.get("event_id"):
            ancients.setdefault(event["event_id"], {
                "event_name": event.get("event_name"), "act": act, "floor": floor,
                "first_ts": row["timestamp_utc"]})
        if state == "game_over":
            game_over = raw.get("game_over") or {}
            victory = game_over.get("is_victory")
            hp_at_end = (raw.get("player") or {}).get("hp")
    return {
        "first_ts": frames[0]["timestamp_utc"],
        "last_ts": frames[-1]["timestamp_utc"],
        "frames": len(frames),
        "acts": {str(act): {"floors_seen": sorted(info["floors"]),
                            "min_floor": min(info["floors"]), "max_floor": max(info["floors"]),
                            "first_ts": info["first_ts"], "last_ts": info["last_ts"]}
                 for act, info in sorted(acts.items())},
        "deepest_act": max(acts) if acts else None,
        "deepest_floor": max((f for i in acts.values() for f in i["floors"]), default=None),
        "boss_nodes": [{"act": act, "floor": floor, "room": room, "entities": info["entities"],
                        "entities_ever_seen": sorted(info["all_seen"]),
                        "max_hp_seen": info["max_hp_seen"], "first_ts": info["first_ts"]}
                       for (act, floor, room), info in sorted(bosses.items())
                       if room == "boss"],
        "ancients": [{"event_id": eid, "act": info["act"], "floor": info["floor"],
                      "expected_act": ANCIENT_ACT.get(eid), "first_ts": info["first_ts"]}
                     for eid, info in sorted(ancients.items(), key=lambda kv: kv[1]["first_ts"])],
        "victory_flag": victory,
        "hp_at_game_over": hp_at_end,
        "ended_with": window["ended_with"],
    }


def _acceptance(batch_dir: pathlib.Path) -> dict:
    """What the batch's own machinery said about this run, blockers included.

    Read from the supervisor's status file rather than paraphrased: an acceptance claim of false
    with named blockers is part of the record, and the next reader has to see it next to the
    victory flag, not three documents away.
    """
    path = batch_dir / "status.json"
    if not path.is_file():
        return {"status_file": None, "available": False,
                "note": "the supervisor writes status.json at session_end; before that the "
                        "batch's own verdict does not exist yet"}
    status = json.loads(path.read_text(encoding="utf-8"))
    comparison = status.get("comparison_result") or {}
    attestation = (status.get("mod_attestation") or {})
    children = status.get("children") or {}

    def solver_record(block: dict) -> dict:
        for record in (block or {}).get("records", []):
            if record.get("mod_id") == "CombatSolver":
                return {k: record.get(k) for k in
                        ("mod_id", "mod_manifest_version", "path", "actual_sha256",
                         "expected_sha256", "matches_lock")}
        return {}

    return {
        "available": True,
        "result_code": status.get("result_code"),
        "batch_status": status.get("status"),
        "stop_reason": status.get("stop_reason"),
        "completed_at_utc": status.get("completed_at_utc"),
        "acceptance_claim": comparison.get("acceptance_claim"),
        "acceptance_blockers": comparison.get("acceptance_blockers"),
        "comparison_issues": comparison.get("issues"),
        "battle_rows": (comparison.get("counts") or {}).get("battle_rows"),
        "child_exit_codes": {name: (child or {}).get("exit_code")
                             for name, child in sorted(children.items())},
        "solver_at_start": solver_record(attestation.get("at_start")),
        "solver_at_end": solver_record(attestation.get("at_end")),
        "solver_drifted_from_lock": (attestation.get("at_end") or {}).get("drifted_from_lock"),
    }


def build(trace: pathlib.Path) -> dict:
    rows = list(_iter_rows(trace))
    session = next((r["raw"] for r in rows if r.get("event_type") == "session"), {})
    identity = next((r["raw"] for r in rows if r.get("event_type") == "run_identity"), {})
    runs = [_summarise(w) for w in _run_windows(rows) if len(w["frames"]) > 20]
    wins = [r for r in runs if r["victory_flag"] is True]
    return {
        "batch_id": trace.parent.name,
        "trace_sha256": hashlib.sha256(trace.read_bytes()).hexdigest(),
        "trace_rows": len(rows),
        "game": session.get("observed_game"),
        "version_lock": session.get("version_lock"),
        "execution_owner": session.get("execution_owner"),
        "live_choice_policy_version": session.get("live_choice_policy_version"),
        "cohort": session.get("cohort"),
        "seed_mode": session.get("seed_mode"),
        "git_head": session.get("git_head"),
        "first_run_identity": identity,
        "acceptance": _acceptance(trace.parent),
        "completed_runs": len(runs),
        "victories": len(wins),
        "defeats": len([r for r in runs if r["victory_flag"] is False]),
        "runs": runs,
        "victory_run": wins[-1] if wins else None,
        "not_established": [
            "a win rate: this is one batch's runs, and one trace is a reachability result, not a "
            "rate",
            "the acceptance contract's own verdict until the batch reaches session_end -- the "
            "11-item gate is evaluated on a finished batch, and a live batch is not proof",
            "that the ending's hp 0 is a story beat rather than a death: the client's own "
            "game_over flag says victory, the boss list was empty before it, and the reward "
            "screen was already claimed",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=pathlib.Path, required=True)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    args = parser.parse_args()
    payload = build(args.trace)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    summary = {k: payload[k] for k in
               ("batch_id", "completed_runs", "victories", "defeats",
                "live_choice_policy_version", "execution_owner")}
    if payload["victory_run"]:
        run = payload["victory_run"]
        summary["victory"] = {"deepest_act": run["deepest_act"],
                              "deepest_floor": run["deepest_floor"],
                              "ancients": [a["event_id"] for a in run["ancients"]],
                              "boss_nodes": [(b["floor"], b["entities"]) for b in run["boss_nodes"]]}
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
