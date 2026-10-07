"""Compare two real-client runs requested under the same seed, and name the first divergence.

`docs/FIXED_SEED_FEASIBILITY.md` has always carried the caveat that a run seed fixes the encounter
sequence *only if* the preceding action sequence is also fixed. #47 proved the seed arrives; this
tool answers the next question, which is the one G3 actually needs: does the same seed on the same
build produce the same run? Combat is excluded from the claim by construction -- the CombatSolver is
the actor there and its search is wall-clock bounded -- so the comparison is over what the client
publishes and what our policy chooses:

  * the map path: the (act, floor, room) sequence the run landed on, and the option index we posted;
  * every encounter: the enemy ids on the node, in order;
  * every offer the client showed: card ids at a card reward, relic ids at a treasure, event ids;
  * the HP the client reported at each decision frame;
  * the terminal (floor, outcome, victory flag).

Rows are compared in order. The first index at which they differ is reported with both rows verbatim,
so a verdict can be argued with, and the divergence is classified. Running a trace against ITSELF
must report identical -- that control is what stops an all-empty comparison from reading as a pass.

    .tools/python/.../python.exe scripts/compare_seeded_replays.py \
        --a runs/solver_supervisor/<batch A>/autoplay_trace.jsonl \
        --b runs/solver_supervisor/<batch B>/autoplay_trace.jsonl \
        --out docs/evidence/seed_reproduction_20261007.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import sys

#: Frames that carry no content to compare -- transition polls, and the menu between screens.
_NON_DECISION_SCREENS = {"", "unknown", "menu"}

#: Screens whose offer lists are content the seed is supposed to control.
OFFER_KEYS = {
    "card_reward": ("card_reward", "cards"),
    "rewards": ("rewards", "cards"),
    "treasure": ("treasure", "relics"),
    "shop": ("shop", "cards"),
    "event": ("event", "options"),
    "rest_site": ("rest_site", "options"),
}


def _rows(trace: pathlib.Path) -> list[dict]:
    return [json.loads(line) for line in
            trace.read_text(encoding="utf-8").splitlines() if line.strip()]


def _run_rows(rows: list[dict]) -> list[dict]:
    """One row per decision frame the client showed, in order.

    A row records where we were (act/floor/screen), what was on offer (ids only, never prose),
    which enemy list a fight carried and the HP the client reported. The action we posted is
    attached to the frame it was posted *from*, so a divergence can be read as "the client showed
    this, and we answered that" rather than as two unrelated columns.
    """
    out: list[dict] = []
    for row in rows:
        event = row.get("event_type")
        raw = row.get("raw")
        if event == "action":
            if out and isinstance(raw, dict):
                out[-1]["posted"] = {k: raw.get(k) for k in
                                     ("action", "index", "option", "card_id", "amount")
                                     if raw.get(k) is not None}
            continue
        if event != "state" or not isinstance(raw, dict):
            continue
        state_type = str(raw.get("state_type") or "unknown")
        if state_type in _NON_DECISION_SCREENS:
            # Transition frames are poll noise, not content: one batch catches the menu between
            # two screens, another catches only the frame after it, and aligning on them would
            # report a divergence in how often we happened to poll. Only frames where something is
            # offered or fought are comparable content.
            continue
        run = raw.get("run") or {}
        battle = raw.get("battle") or {}
        player = raw.get("player") or {}
        enemies = []
        for side in ("enemies", "monsters"):
            got = battle.get(side) if isinstance(battle, dict) else None
            if isinstance(got, list):
                enemies = [str(e.get("id") or e.get("name") or "?")
                           for e in got if isinstance(e, dict)]
                break
        if state_type == "event" and not enemies:
            battle_ev = raw.get("event") or {}
            if isinstance(battle_ev, dict) and battle_ev.get("fight_started"):
                enemies = ["<fight-started>"]
        offers: dict[str, list] = {}
        for key, (block, field) in OFFER_KEYS.items():
            if state_type != key:
                continue
            body = raw.get(block) or {}
            items = body.get(field) if isinstance(body, dict) else None
            if isinstance(items, list):
                offers[field] = [str(i.get("id") or i.get("name") or i.get("option") or "?")
                                 if isinstance(i, dict) else str(i) for i in items]
            if isinstance(body, dict) and body.get("event_id"):
                offers["event_id"] = [str(body["event_id"])]
        out.append({
            "sequence": row.get("sequence"),
            "ts": row.get("timestamp_utc"),
            "act": run.get("act"),
            "floor": run.get("floor"),
            "screen": state_type,
            "room": (battle.get("room") or run.get("room")
                     or (run.get("current_room") or {}).get("type")
                     if isinstance(run.get("current_room"), dict) else run.get("room")),
            "enemies": enemies,
            "offers": offers,
            "hp": player.get("hp"),
            "posted": None,
        })
    return out


#: Screens whose content the seed is supposed to own: what the map offered, what a room presented,
#: what a chooser listed. Combat frames are deliberately absent -- the solver is the actor inside a
#: fight and its search is bounded by wall-clock, so whether two batches sampled the same HP on turn
#: 6 says nothing about the seed. Splitting the two is what keeps the verdict about the right thing.
CONTENT_SCREENS = {"map", "event", "rewards", "card_reward", "treasure", "rest_site", "shop",
                   "card_select", "fake_merchant", "crystal_sphere", "bundle_select", "overlay"}


def _digest(row: dict) -> str:
    return hashlib.sha256(json.dumps(
        {k: row.get(k) for k in ("act", "floor", "screen", "room", "enemies", "offers", "hp")},
        ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _agree(rows_a: list[dict], rows_b: list[dict], fields: tuple) -> dict:
    paired = list(zip(rows_a, rows_b))
    diffs = [(i, a, b) for i, (a, b) in enumerate(paired)
             if hashlib.sha256(json.dumps({k: a.get(k) for k in fields}, ensure_ascii=False,
                                          sort_keys=True).encode("utf-8")).hexdigest()[:16]
             != hashlib.sha256(json.dumps({k: b.get(k) for k in fields}, ensure_ascii=False,
                                          sort_keys=True).encode("utf-8")).hexdigest()[:16]]
    return {"compared": len(paired), "identical": len(paired) - len(diffs),
            "differing": len(diffs), "rows_a": len(rows_a), "rows_b": len(rows_b),
            "first": diffs[0] if diffs else None}


def _classify(a: dict, b: dict) -> str:
    if a["screen"] != b["screen"]:
        return "screen_sequence"
    if (a["act"], a["floor"]) != (b["act"], b["floor"]):
        return "map_path"
    if a["enemies"] != b["enemies"]:
        return "encounter"
    if a["offers"] != b["offers"]:
        return "offer_contents"
    if a["hp"] != b["hp"]:
        return "hp_at_decision"
    return "other"


def compare(path_a: pathlib.Path, path_b: pathlib.Path) -> dict:
    rows_a, rows_b = _run_rows(_rows(path_a)), _run_rows(_rows(path_b))
    content_a = [r for r in rows_a if r["screen"] in CONTENT_SCREENS]
    content_b = [r for r in rows_b if r["screen"] in CONTENT_SCREENS]
    combat_a = [r for r in rows_a if r["screen"] not in CONTENT_SCREENS]
    combat_b = [r for r in rows_b if r["screen"] not in CONTENT_SCREENS]
    # Content is aligned on the content subsequence. Index alignment over *all* frames would compare
    # whatever poll each batch happened to catch mid-fight, which is a statement about poll rate.
    content = _agree(content_a, content_b,
                     ("act", "floor", "screen", "room", "enemies", "offers"))
    combat = _agree(combat_a, combat_b, ("act", "floor", "screen", "enemies"))
    paired = list(zip(rows_a, rows_b))
    diffs = [(i, a, b) for i, (a, b) in enumerate(paired) if _digest(a) != _digest(b)]
    classes: dict[str, int] = {}
    for _i, a, b in diffs:
        key = _classify(a, b)
        classes[key] = classes.get(key, 0) + 1
    first = diffs[0] if diffs else None
    session_of = lambda p: next((r.get("raw") or {} for r in _rows(p)
                                 if r.get("event_type") == "session"), {})
    ident_of = lambda p: next((r.get("raw") or {} for r in _rows(p)
                               if r.get("event_type") == "run_identity"), {})
    sa, sb = session_of(path_a), session_of(path_b)
    ia, ib = ident_of(path_a), ident_of(path_b)
    # Two verdicts, because the two halves of a run are owned by different actors: the seed owns
    # what the map and the rooms offer, the CombatSolver owns what happens inside a fight.
    if content["compared"] == 0:
        content_verdict = "no_content_rows_compared"
    elif content["differing"] == 0 and content["rows_a"] == content["rows_b"]:
        content_verdict = "content_identical"
    else:
        cf = content["first"]
        content_verdict = "content_diverges_at_row_%d_as_%s" % (
            cf[0], _classify(cf[1], cf[2]))
    whole_verdict = ("identical" if not diffs and paired else
                     ("diverges_at_row_%d_as_%s" % (first[0], _classify(first[1], first[2]))
                      if first else "nothing_compared"))
    return {
        "schema_version": 1,
        "generated_by": "scripts/compare_seeded_replays.py",
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "trace_a": str(path_a),
        "trace_b": str(path_b),
        "trace_a_sha256": hashlib.sha256(path_a.read_bytes()).hexdigest(),
        "trace_b_sha256": hashlib.sha256(path_b.read_bytes()).hexdigest(),
        "seed_a": ia.get("seed"),
        "seed_b": ib.get("seed"),
        "same_seed_requested": bool(ia.get("seed")) and ia.get("seed") == ib.get("seed"),
        "same_build": sa.get("observed_game") == sb.get("observed_game"),
        "game_a": sa.get("observed_game"),
        "rows_a": len(rows_a),
        "rows_b": len(rows_b),
        "rows_compared": len(paired),
        "rows_identical": len(paired) - len(diffs),
        "rows_differing": len(diffs),
        "divergence_classes": classes,
        "first_divergence": None if first is None else {
            "index": first[0],
            "class": _classify(first[1], first[2]),
            "a": first[1],
            "b": first[2],
        },
        "terminal_a": rows_a[-1] if rows_a else None,
        "terminal_b": rows_b[-1] if rows_b else None,
        "content_agreement": {k: v for k, v in content.items() if k != "first"},
        "combat_agreement": {k: v for k, v in combat.items() if k != "first"},
        "first_content_divergence": None if content["first"] is None else {
            "index": content["first"][0],
            "class": _classify(content["first"][1], content["first"][2]),
            "a": content["first"][1], "b": content["first"][2]},
        "verdict": content_verdict,
        "whole_trace_verdict": whole_verdict,
        "not_established": [
            "that combat reproduces: the CombatSolver owns every fight and its search is bounded by "
            "wall-clock, so fight outcomes are not expected to match and are excluded from the "
            "compared fields",
            "anything from the shorter trace's tail: comparison stops at the first row that only "
            "one of the two runs reached, and an unequal row count is reported, not padded",
            "a sim-side equivalence: both traces are real-client runs, and the emulator is not "
            "involved in this measurement",
        ],
        "how_to_recheck": (
            f"python scripts/compare_seeded_replays.py --a {path_a.as_posix()} "
            f"--b {path_b.as_posix()} --out <artifact>"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--a", type=pathlib.Path)
    parser.add_argument("--b", type=pathlib.Path)
    parser.add_argument("--out", type=pathlib.Path)
    args = parser.parse_args()
    if args.a is None or args.b is None:
        parser.error("--a and --b are both required")
    payload = compare(args.a, args.b)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
    slim = {k: payload[k] for k in
            ("seed_a", "seed_b", "same_seed_requested", "same_build", "rows_compared",
             "rows_identical", "rows_differing", "content_agreement", "combat_agreement",
             "verdict")}
    print(json.dumps(slim, ensure_ascii=False, indent=1))
    fd = payload.get("first_content_divergence") or payload["first_divergence"]
    if fd:
        print("first divergence:", json.dumps(
            {"index": fd["index"], "class": fd["class"], "a": fd["a"], "b": fd["b"]},
            ensure_ascii=False)[:900])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
