"""Re-verify every named Act 1 win and emit an auditable ledger.

The campaign report's strongest claim is "N individually reproducible Act 1
terminal victories". That claim is only as good as a reader's ability to check it
without re-running thousands of episodes, so this walks the committed evidence
matrix, re-runs each winning seed once against the checkpoint that won it, and
records for every row: the checkpoint digest, the seed list digest, the measured
act, floor, legality and dead-end classification, and the command to repeat it.

A win that no longer reproduces is reported as a failure, not dropped: the ledger
is the artifact, so a stale row must be visible rather than quietly corrected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "third_party" / "slay-the-spire-2-emulator-main" / "src"))
sys.path.insert(0, str(ROOT))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path,
                        default=ROOT / "docs/evidence/act1_evidence_20260919.json")
    parser.add_argument("--config", type=Path,
                        help="stage/partition TOML; defaults to each row's source arm config")
    parser.add_argument("--out", type=Path,
                        default=ROOT / "docs/evidence/act1_win_ledger_20260919.json")
    parser.add_argument("--keep_run_history", action="store_true",
                        help="merge a verification_runs history into the output file, so a re-run "
                             "records that the rows themselves did not change")
    args = parser.parse_args()

    matrix = json.loads(args.matrix.read_text(encoding="utf-8"))

    # Each matrix row was produced from one arm's own config; the ledger must
    # re-evaluate under that same config or the partition means something else.
    config_for = {
        "b_terminal-1": ROOT / "runtime/fanout/b_terminal-1.toml",
        "b_terminal-0": ROOT / "runtime/fanout/b_terminal-0.toml",
        "b_terminal-2": ROOT / "runtime/fanout/b_terminal-2.toml",
        "fancont b_terminal-1": ROOT / "runtime/act1_overnight/fan_cont/b_terminal-1.toml",
        "a_base": ROOT / "runtime/act1_overnight/a_base.toml",
    }

    import sts2_gym  # noqa: F401

    from sb3_contrib import MaskablePPO
    from stable_baselines3.common.vec_env import DummyVecEnv

    from training.evaluation import evaluate_policy
    from training.v2_config import load_v2_training_config
    from training.v2_curriculum import _environment_factory

    rows = []
    for entry in matrix["rows"]:
        seeds = entry.get("winning_seeds") or []
        if not seeds:
            continue
        arm = entry["arm"]
        # Match by arm prefix rather than a fixed token count: the matrix labels
        # rows "<arm> <checkpoint> x <split>", and "fan_cont" rows carry a slash.
        matches = [key for key in config_for if arm.startswith(key)]
        arm_key = max(matches, key=len) if matches else arm
        config_path = args.config or config_for.get(arm_key)
        if config_path is None or not Path(config_path).exists():
            rows.append({**entry, "verification": "NO_CONFIG", "config": str(config_path)})
            print(f"  {arm}: no config recorded, cannot re-verify")
            continue
        checkpoint = None
        source = entry.get("source")
        if source:
            # The matrix stores digests; the originating split artifact records
            # the path each digest was computed over.
            source_payload = json.loads((ROOT / source).read_text(encoding="utf-8"))
            checkpoint = Path(source_payload.get("checkpoint") or "")
        if checkpoint is None or not checkpoint.is_file():
            rows.append({**entry, "verification": "NO_CHECKPOINT"})
            print(f"  {arm}: checkpoint path missing ({checkpoint})")
            continue

        config = load_v2_training_config(Path(config_path).resolve())
        stage = next(s for s in config.stages if s.name == "act1")
        factory = _environment_factory(config, stage, sts2_gym)
        probe = DummyVecEnv([lambda: factory(seeds[0])])
        model = MaskablePPO.load(str(checkpoint), env=probe, device="cpu")

        verified = []
        for seed in seeds:
            metrics = evaluate_policy(
                model,
                env_factory=factory,
                seeds=[int(seed)],
                stage="act1",
                split=f"{entry['split']}_act1_ledger",
                scope="simulator_act1",
                checkpoint=checkpoint,
                max_steps_per_episode=stage.max_episode_steps,
            ).to_dict()
            won = int(metrics["wins"]) > 0
            verified.append({
                "seed": int(seed),
                "won": won,
                "final_floor": metrics["max_final_floor"],
                "illegal_actions": metrics["illegal_actions"],
                "unclassified_dead_ends": metrics["unclassified_dead_ends"],
                "truncations": metrics["truncations"],
            })
            print(f"  {arm}  seed {seed}: won={won} floor={metrics['max_final_floor']} "
                  f"illegal={metrics['illegal_actions']} unc={metrics['unclassified_dead_ends']}")
        rows.append({
            "arm": arm,
            "split": entry["split"],
            "checkpoint": str(checkpoint),
            "checkpoint_sha256": _sha256(checkpoint),
            "matrix_checkpoint_sha256": entry.get("checkpoint_sha256"),
            "config": str(config_path),
            "seeds": [int(s) for s in seeds],
            "seed_sha256": hashlib.sha256(
                ",".join(str(s) for s in seeds).encode("ascii")).hexdigest(),
            "per_seed": verified,
            "all_reproduced": all(v["won"] and not v["illegal_actions"]
                                 and not v["unclassified_dead_ends"] for v in verified),
        })

    ok = [r for r in rows if r.get("all_reproduced")]
    failed = [r for r in rows if "all_reproduced" in r and not r["all_reproduced"]]
    ledger = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "source_matrix": str(args.matrix),
        "scope": "simulator_act1_terminal_victories_individual_reproduction",
        "not_scope": ("real-machine A10 acceptance; the emulator defines two acts and "
                      "has no Act 3, so no row here evidences an Act 1-3 clear"),
        "win_rows": len(rows),
        "wins_reproduced": sum(len(r.get("per_seed", [])) for r in ok),
        "wins_failed_to_reproduce": sum(len(r.get("per_seed", [])) for r in failed),
        "rows": rows,
        "how_to_recheck": (
            "python scripts/act1_win_ledger.py "
            "--matrix docs/evidence/act1_evidence_20260919.json"),
    }
    def digest_of(recorded: list) -> str:
        # Hashes the rows only, so a re-run that reproduces everything matches bit-for-bit
        # even though its own timestamp differs -- which is the whole point of keeping history.
        return hashlib.sha256(
            json.dumps(recorded, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    def summary_of(recorded: dict) -> dict:
        return {"generated_at": recorded.get("generated_at"),
                "win_rows": recorded.get("win_rows"),
                "wins_reproduced": recorded.get("wins_reproduced"),
                "wins_failed_to_reproduce": recorded.get("wins_failed_to_reproduce"),
                "rows_sha256": digest_of(recorded.get("rows", []))}

    if args.keep_run_history and args.out and args.out.is_file():
        previous = json.loads(args.out.read_text(encoding="utf-8"))
        history = list(previous.get("verification_runs", []))
        if not history and previous.get("rows"):
            history.append(summary_of(previous))
        ledger["verification_runs"] = history + [summary_of(ledger)]
        ledger["rows_identical_across_runs"] = len(
            {entry["rows_sha256"] for entry in ledger["verification_runs"]}) == 1
    print(f"\nwins reproduced: {ledger['wins_reproduced']}  "
          f"failed: {ledger['wins_failed_to_reproduce']}  rows: {len(rows)}")
    if "verification_runs" in ledger:
        print(f"verification runs: {len(ledger['verification_runs'])}, "
              f"rows identical: {ledger['rows_identical_across_runs']} "
              f"({ledger['verification_runs'][-1]['rows_sha256'][:12]})")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(ledger, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8")
        print(f"wrote {args.out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
