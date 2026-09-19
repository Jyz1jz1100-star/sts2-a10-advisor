"""Paired comparison of two arms on the same act-filtered evaluation seeds.

Written and committed *before* the A/B checkpoints existed, so the analysis
cannot be reshaped by the result.  The decisive quantity is fixed there:
``mean_final_floor`` over the same Act-1 seeds for both arms, with a paired
bootstrap because both arms are evaluated on the identical seed list.

Paired, not two-sample: the same seed generates the same map, so per-seed
differences carry the signal and the between-seed variance cancels.
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

ACT_NAMES = {1: "overgrowth", 2: "underdocks"}
DECISIVE_THRESHOLD = 0.5  # floors; pre-registered, see docs/ACT1_CAMPAIGN_2026-09-19.md


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _floors_by_seed(model, factory, seeds, max_steps):
    """Per-seed outcome, reusing ``evaluate_policy`` one seed at a time.

    The metrics object summarizes (``episodes`` is a count, not a row list), and
    reimplementing the rollout here would risk drifting from the win / illegal /
    dead-end semantics the campaign numbers were measured under -- the same
    reason the chained-flow probe copies them instead of inventing them.
    """
    from training.evaluation import evaluate_policy

    per_seed: dict[int, tuple[float | None, bool, bool, int]] = {}
    totals = {"trunc": 0, "illegal": 0, "unclassified": 0}
    for seed in seeds:
        metrics = evaluate_policy(
            model,
            env_factory=factory,
            seeds=[seed],
            stage="act1",
            split="promotion_act1_paired",
            scope="simulator_act1",
            checkpoint="paired",
            max_steps_per_episode=max_steps,
        ).to_dict()
        per_seed[int(seed)] = (
            metrics.get("mean_final_floor"),
            int(metrics["wins"]) > 0,
            int(metrics["truncations"]) > 0,
            int(metrics["illegal_actions"] or 0),
        )
        totals["trunc"] += int(metrics["truncations"])
        totals["illegal"] += int(metrics["illegal_actions"] or 0)
        totals["unclassified"] += int(metrics.get("unclassified_dead_ends") or 0)
    summary = {
        "mean_final_floor": sum(
            value[0] for value in per_seed.values() if value[0] is not None
        ) / max(1, sum(1 for value in per_seed.values() if value[0] is not None)),
        "wins": sum(1 for value in per_seed.values() if value[1]),
        "episodes": len(per_seed),
        "truncation_rate": totals["trunc"] / max(1, len(per_seed)),
        "illegal_actions": totals["illegal"],
        "unclassified_dead_ends": totals["unclassified"],
    }
    return summary, per_seed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="arm TOML (partitions)")
    parser.add_argument("--arm", action="append", nargs=2, required=True,
            metavar=("LABEL", "CHECKPOINT"),
            help="exactly two: label and checkpoint zip; both get the same seeds")
    parser.add_argument("--act", type=int, choices=(1, 2), default=1)
    parser.add_argument("--split", default="promotion")
    parser.add_argument("--episodes", type=int, default=1000)
    parser.add_argument("--bootstrap", type=int, default=20000)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    arms = dict(args.arm)
    if len(arms) != 2:
        raise SystemExit("supply exactly two --arm LABEL CHECKPOINT pairs")
    left_label, right_label = list(arms)

    import sts2_gym  # noqa: F401

    from sb3_contrib import MaskablePPO
    from stable_baselines3.common.vec_env import DummyVecEnv

    from training.v2_config import load_v2_training_config
    from training.v2_curriculum import _environment_factory

    config = load_v2_training_config(args.config.resolve())
    stage = next(s for s in config.stages if s.name == "act1")
    partition = config.partition(stage.name, args.split)
    factory = _environment_factory(config, stage, sts2_gym)

    # Phase 1: which seeds generate the act under test.  One reused env: an env per
    # seed leaks a native run handle and the emulator starts failing GetInfo.
    all_seeds = partition.seeds(min(args.episodes, partition.count))
    census_env = factory(all_seeds[0])
    acts = {seed: int(census_env.reset(seed=seed)[1].get("act") or 0) for seed in all_seeds}
    census_env.close()
    seeds = [seed for seed in all_seeds if acts[seed] == args.act]
    if len(seeds) < 30:
        raise SystemExit(f"only {len(seeds)} seeds generate act {args.act}; too few to pair")
    seed_sha = hashlib.sha256(",".join(str(s) for s in seeds).encode()).hexdigest()
    print(f"{args.act}/{ACT_NAMES[args.act]}: {len(seeds)} shared seeds, sha {seed_sha[:16]}…")

    results = {}
    for label, checkpoint in arms.items():
        checkpoint = Path(checkpoint).resolve()
        probe = DummyVecEnv([lambda: factory(seeds[0])])
        model = MaskablePPO.load(str(checkpoint), env=probe, device="cpu")
        metrics, per_seed = _floors_by_seed(model, factory, seeds, stage.max_episode_steps)
        missing = [s for s in seeds if s not in per_seed]
        if missing:
            raise SystemExit(f"{label}: no per-seed record for {len(missing)} seeds")
        no_floor = [s for s in seeds if per_seed[s][0] is None]
        if no_floor:
            raise SystemExit(
                f"{label}: {len(no_floor)} seed(s) have no final floor; pairing needs "
                "one number per seed and a missing one is an unclassified outcome"
            )
        results[label] = {
            "checkpoint": str(checkpoint),
            "checkpoint_sha256": _sha256(checkpoint),
            "mean_final_floor": metrics["mean_final_floor"],
            "wins": metrics["wins"],
            "episodes": metrics["episodes"],
            "truncation_rate": metrics["truncation_rate"],
            "illegal_actions": metrics["illegal_actions"],
            "unclassified_dead_ends": metrics["unclassified_dead_ends"],
            "floors": [per_seed[s][0] for s in seeds],
        }
        print(f"  {label}: mean_floor={results[label]['mean_final_floor']:.3f} "
              f"wins={results[label]['wins']}/{len(seeds)} "
              f"trunc={results[label]['truncation_rate']:.5f} "
              f"illegal={results[label]['illegal_actions']} "
              f"unclassified={results[label]['unclassified_dead_ends']}")

    left = results[left_label]["floors"]
    right = results[right_label]["floors"]
    diffs = [r - l for l, r in zip(left, right)]
    n = len(diffs)
    observed = sum(diffs) / n
    # Percentile bootstrap over the paired differences; deterministic given the seed.
    import random

    rng = random.Random(20260919)
    stats = []
    for _ in range(args.bootstrap):
        draw = sum(diffs[rng.randrange(n)] for _ in range(n)) / n
        stats.append(draw)
    stats.sort()
    lo = stats[int(0.025 * args.bootstrap)]
    hi = stats[int(0.975 * args.bootstrap)]
    two_sided = 2 * min(sum(1 for s in stats if s <= 0), sum(1 for s in stats if s >= 0)) / args.bootstrap
    verdict = (
        "readable signal" if abs(observed) >= DECISIVE_THRESHOLD and (lo > 0 or hi < 0)
        else "too small to read" if abs(observed) < DECISIVE_THRESHOLD
        else "above threshold, interval crosses zero"
    )

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": f"simulator_act{args.act}",
        "decisive_metric": "mean_final_floor on shared seeds (paired)",
        "pre_registered_threshold_floors": DECISIVE_THRESHOLD,
        "shared_seeds": n,
        "seed_sha256": seed_sha,
        "arms": {k: {key: val for key, val in v.items() if key != "floors"}
                 for k, v in results.items()},
        "difference": {
            f"{right_label} minus {left_label}": observed,
            "ci95_bootstrap": [lo, hi],
            "p_two_sided_bootstrap": two_sided,
            "verdict": verdict,
        },
        "non_decisive": {
            left_label: {"wins": results[left_label]["wins"]},
            right_label: {"wins": results[right_label]["wins"]},
            "reason": ("504-ish shared seeds at a ~0.0006 act-1 win rate expect "
                       "~0.3 wins; any win-count difference tonight is noise"),
        },
    }
    print(f"Δ mean_floor ({right_label} - {left_label}) = {observed:+.3f} "
          f"[{lo:+.3f}, {hi:+.3f}] p≈{two_sided:.4f} -> {verdict}")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
