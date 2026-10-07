"""Capture the evidence that decides whether the installed bridge supports seeded embark.

`data/combat_solver/fixed_battle_seeds.json` records `installed_bridge_supported`, and the rule in
`docs/FIXED_SEED_FEASIBILITY.md` is that it may only be flipped when three things come from the same
real embark: the POST echoes `seed_requested` and `seed_canonical` equal to the registered value, it
names an injection path (`seed_injection`), and the authoritative read-back
(`compendium.current_run.seed`, via the controller's run-identity guard) equals the canonical value.
The bridge's own `seed_verified` stays false by design -- it does not attest itself -- so the
read-back is the only verification, and the controller fails closed when the two disagree.

The trace it reads is gitignored, so the rows are copied verbatim into the committed artifact.

    .tools/python/.../python.exe scripts/capture_seeded_embark_evidence.py \
        --batch runs/solver_supervisor/<ssb-...> \
        --out docs/evidence/seeded_embark_20261007.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bridge.trace_controller import canonicalize_game_seed  # noqa: E402


def _rows(trace: pathlib.Path) -> list[dict]:
    return [json.loads(line) for line in
            trace.read_text(encoding="utf-8").splitlines() if line.strip()]


def capture(batch: pathlib.Path) -> dict:
    rows = _rows(batch / "autoplay_trace.jsonl")
    session = next((r.get("raw") or {} for r in rows if r.get("event_type") == "session"), {})
    identity = next((r for r in rows if r.get("event_type") == "run_identity"), None)
    embark = next((r for r in rows
                   if r.get("event_type") == "result"
                   and "Embarking on run" in str((r.get("raw") or {}).get("message") or "")),
                  None)
    request = next((r for r in rows
                    if r.get("event_type") == "action"
                    and (r.get("raw") or {}).get("seed") is not None), None)
    if identity is None or embark is None:
        raise SystemExit(
            f"{batch.name}: no seeded embark (run_identity={identity is not None} "
            f"embark={embark is not None}) -- this batch was not a fixed-mode start")

    emb = embark.get("raw") or {}
    ident = identity.get("raw") or {}
    requested = str(emb.get("seed_requested") or "")
    canonical = str(emb.get("seed_canonical") or "")
    observed = str(ident.get("seed") or "")

    return {
        "schema_version": 1,
        "generated_by": "scripts/capture_seeded_embark_evidence.py",
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "batch_id": batch.name,
        "seed_mode": session.get("seed_mode"),
        "game": session.get("observed_game"),
        "solver_at_start": (session.get("solver_inventory") or [{}]),
        "requested_seed": requested,
        "canonical_seed_from_bridge": canonical,
        "canonical_seed_recomputed_here": canonicalize_game_seed(requested),
        "injection_path_named_by_bridge": emb.get("seed_injection"),
        "bridge_self_attestation": emb.get("seed_verified"),
        "authoritative_read_back": {
            "field": "compendium.current_run.seed (via run_identity)",
            "seed": observed,
            "run_id": ident.get("run_id"),
            "character": ident.get("character_id"),
            "ascension": ident.get("ascension"),
            "game_mode": ident.get("game_mode"),
        },
        "embark_result_verbatim": emb,
        "run_identity_verbatim": ident,
        "criteria": {
            "requested_equals_canonical": requested == canonical and requested != "",
            "bridge_names_an_injection_path": bool(emb.get("seed_injection")),
            "read_back_equals_canonical": observed == canonical and observed != "",
            "canonical_is_stable_under_the_games_rule": (
                canonical == canonicalize_game_seed(requested)),
        },
        "how_to_recheck": (
            "python scripts/supervise_solver_batch.py --mode fixed --track acceptance "
            "--seed-file data/combat_solver/fixed_battle_seeds.json --max-runs 1 "
            "--max-actions 3000 --poll 0.5 --allow-actions  "
            "(the controller refuses the run outright on a seed mismatch, so a batch that "
            "produces a run_identity is itself the test)"),
        "not_established": [
            "that the seeded run is reproducible in its outcomes -- this file answers only whether "
            "the requested seed reaches the game and is read back; comparing two runs on one seed "
            "is a separate measurement",
            "anything about the comparison track's mod lock: this batch ran on the acceptance "
            "track with --mod-gate attest, because the installed solver differs from the pin",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=pathlib.Path, required=True)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    args = parser.parse_args()
    payload = capture(args.batch)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    print(json.dumps({"batch_id": payload["batch_id"], "seed_mode": payload["seed_mode"],
                      "requested": payload["requested_seed"],
                      "read_back": payload["authoritative_read_back"]["seed"],
                      "criteria": payload["criteria"]}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
