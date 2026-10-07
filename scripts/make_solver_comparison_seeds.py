"""Generate the pre-registered fixed-battle seed file for the Combat Solver
comparison track (Phase B of docs/COMBAT_SOLVER.md).

The seed range 1,600,000,000+ is allocated here and is disjoint from every
existing partition: V2 curriculum (100M-130M+), teacher batch0/1 lineage
(1,400,000,000-1,401,000,000), reserved teacher-test corpus (>=1,410,000,000),
dagger0 (1,500,100,000-1,500,102,000) and the teacher-strength evaluation
(1,550,000,000+). The file is committed once; regenerating overwrites it in
place with identical content for identical arguments (deterministic output,
no RNG).

Real-game caveat: a run seed fixes the encounter sequence only if the whole
pre-battle action sequence is also fixed. The file therefore records seeds as
the *procedure* anchor; per-battle identity is additionally pinned by the
state hash (decision_id) captured for every turn. If the live bridge cannot
start a run with a chosen seed, the seed column is advisory and the actual
run seed is recorded per battle.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

PARTITION_NAME = "combat_solver_fixed_battles"
PARTITION_START = 1_600_000_000
DEFAULT_COUNT = 12
BATTLES_PER_SEED_ESTIMATE = 9


def build_seed_file(num_seeds: int, battles_per_seed: int) -> dict:
    return {
        "schema_version": 1,
        "allocation_kind": "run_seed",
        "seed_mode": "fixed_verification_required",
        "seed_injection": {
            # Unverified default, deliberately: a freshly generated allocation records no
        # capability verdict. Carrying the flag into a NEW file while the measured one
        # says true would restate a stale claim, so copy the verdict from the committed
        # allocation (see docs/FIXED_SEED_FEASIBILITY.md, 2026-10-07) when needed.
        "installed_bridge_supported": False,
            "candidate_bridge_supported": True,
            "verification_field": "current_run.seed",
            "note": (
                "The candidate bridge can inject these seeds, but the currently "
                "installed bridge is not yet verified or lock-updated for "
                "injection. A fixed run is valid only when the candidate bridge "
                "is installed and the authoritative current_run.seed is read "
                "back and matches the requested canonical seed."
            ),
        },
        "partition": {
            "name": PARTITION_NAME,
            "start": PARTITION_START,
            "count": num_seeds,
        },
        "allocation_note": (
            "disjoint from V2 curriculum partitions (100M-130M+), teacher "
            "batch0/1 (1,400,000,000-1,401,000,000), reserved test corpus "
            "(>=1,410,000,000), dagger0 (1,500,100,000-1,500,102,000) and "
            "teacher-strength eval (1,550,000,000+)"
        ),
        "battles_per_seed_estimate": battles_per_seed,
        "expected_battles": num_seeds * battles_per_seed,
        "seeds": list(range(PARTITION_START, PARTITION_START + num_seeds)),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "determinism_note": (
            "seeds are the identity range of the partition, not a random draw; "
            "the generated_at_utc field is the only non-reproducible byte"
        ),
    }


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--num-seeds", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--battles-per-seed", type=int, default=BATTLES_PER_SEED_ESTIMATE)
    parser.add_argument(
        "--out",
        default="data/combat_solver/fixed_battle_seeds.json",
    )
    args = parser.parse_args(argv)
    if args.num_seeds <= 0 or args.num_seeds > 200:
        raise SystemExit("--num-seeds must be in 1..200 (allocation guard)")
    payload = build_seed_file(args.num_seeds, args.battles_per_seed)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "written": str(out_path),
                "seeds": len(payload["seeds"]),
                "expected_battles": payload["expected_battles"],
                "sha256": file_sha256(out_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
