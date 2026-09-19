"""Turn "there were 51 truncations at the boss" into a list of seeds you can re-run.

Campaign metrics summarize: `episodes` is a count and `truncations` is a count, so
the 51 floor-17 truncations established in the campaign report cannot be replayed,
classified, or argued about individually. That blocks the goal's "no unclassified
dead ends" clause, because a class you cannot name cannot be audited.

This walks a partition one seed at a time through `evaluate_policy` -- the same
entry point the campaign numbers were measured under, deliberately not a
reimplementation -- and records each seed's terminal category. It writes no metrics
files and changes no schema; it is a read-only census over an existing checkpoint.

    ../third_party/slay-the-spire-2-emulator-main/.venv/Scripts/python.exe \\
        scripts/enumerate_act1_terminals.py \\
        --checkpoint runtime/fanout/b_terminal-1/v2curriculum-20260918T182051Z/act1/checkpoints/step_000002000016.zip \\
        --split promotion --limit 500 --out runtime/act1_terminals.json

Cost: every seed is a full rollout, so a 500-seed partition takes minutes, not
seconds. Use --limit and --start-offset to split the work across processes.
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

# Terminal categories, checked in this order. The point of the ordering is that
# "won" and "locked" both end at floor 17, and only the flags say which happened.
BOSS_FLOOR = 17


def classify(metrics: dict) -> str:
    if int(metrics.get("unclassified_dead_ends") or 0) > 0:
        return "unclassified_dead_end"
    if int(metrics.get("illegal_actions") or 0) > 0:
        return "illegal_action"
    if int(metrics.get("wins") or 0) > 0:
        return "boss_win" if metrics.get("mean_final_floor") == BOSS_FLOOR else "win_earlier"
    if int(metrics.get("truncations") or 0) > 0:
        return (
            "boss_truncation"
            if metrics.get("mean_final_floor") == BOSS_FLOOR
            else "mid_run_truncation"
        )
    return "death"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config/training_v2.toml")
    parser.add_argument("--stage", default="act1")
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--split", default="promotion",
                        help="partition name from the stage's seeds table")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--start-offset", type=int, default=0)
    parser.add_argument("--max-steps", type=int, default=None,
                        help="override the stage's episode cap; default keeps campaign "
                             "semantics so truncations mean the same thing they do in metrics")
    # Replaying a named list instead of a partition slice. The campaign's "N wins" rows were
    # enumerated by this script, so the honest re-check of "they still win" is this same code path over
    # exactly those seeds -- which until now needed a hand-assembled loop, i.e. the thing that cannot
    # be re-run is the sentence, not the number.
    parser.add_argument("--seeds", default=None,
                        help="comma list of seeds to roll instead of a partition slice")
    parser.add_argument("--expect-checkpoint-sha256", default=None,
                        help="refuse to roll unless the checkpoint bytes hash to this digest")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    import sts2_gym
    from sb3_contrib import MaskablePPO
    from stable_baselines3.common.vec_env import DummyVecEnv

    from training.evaluation import evaluate_policy
    from training.v2_config import load_v2_training_config
    from training.v2_curriculum import _environment_factory

    config = load_v2_training_config(args.config.resolve())
    stage = next((item for item in config.stages if item.name == args.stage), None)
    if stage is None:
        raise SystemExit(f"stage {args.stage!r} not in {args.config}")

    # Read the partition the way the trainer reads it, so this census enumerates
    # exactly the seeds a promotion decision was taken over.
    seeds = config.partition(stage.name, args.split).seeds()
    if not seeds:
        raise SystemExit(f"stage {stage.name} has no '{args.split}' seeds")
    if args.seeds:
        window = [int(token) for token in args.seeds.split(",") if token.strip()]
        if len(window) != len(set(window)):
            raise SystemExit("the --seeds list repeats a seed; a win could be counted twice")
        seed_source = f"explicit --seeds list ({len(window)} seeds)"
        outside = [seed for seed in window if seed not in set(seeds)]
    else:
        window = seeds[args.start_offset:args.start_offset + args.limit]
        seed_source = f"{stage.name}/{args.split} partition slice " \
                      f"[{args.start_offset}:{args.start_offset + len(window)}] of {len(seeds)}"
        outside = []
    if not window:
        raise SystemExit(
            f"--start-offset {args.start_offset} is past the end of the "
            f"{args.split} partition ({len(seeds)} seeds)"
        )

    checkpoint = args.checkpoint.resolve()
    checkpoint_digest = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    if args.expect_checkpoint_sha256 and checkpoint_digest != args.expect_checkpoint_sha256:
        raise SystemExit(
            f"refusing to roll: {checkpoint} hashes to {checkpoint_digest[:16]}..., not the "
            f"{args.expect_checkpoint_sha256[:16]}... the named-seed list was enumerated from"
        )
    max_steps = args.max_steps or stage.max_episode_steps
    factory = _environment_factory(config, stage, sts2_gym)
    probe = DummyVecEnv([lambda: factory(window[0])])
    model = MaskablePPO.load(str(checkpoint), env=probe, device="cpu")

    # Which act a seed generates is a reset-only question, and mixing the two acts
    # is the single easiest way to read a wrong conclusion off this census: the
    # emulator picks the act per seed, and the Act 2 boss converts very differently.
    act_env = factory(window[0])
    acts: dict[int, int] = {}
    for seed in window:
        _obs, _info = act_env.reset(seed=int(seed))
        acts[int(seed)] = int(act_env.unwrapped.state_info().get("act") or 0)
    act_env.close()

    rows = []
    counts: dict[str, int] = {}
    for index, seed in enumerate(window, start=1):
        metrics = evaluate_policy(
            model,
            env_factory=factory,
            seeds=[int(seed)],
            stage=stage.name,
            split=args.split,
            scope="simulator_act1",
            checkpoint=str(checkpoint),
            max_steps_per_episode=max_steps,
        ).to_dict()
        category = classify(metrics)
        counts[category] = counts.get(category, 0) + 1
        rows.append({
            "seed": int(seed),
            "category": category,
            "final_floor": metrics.get("mean_final_floor"),
            "wins": int(metrics.get("wins") or 0),
            "truncations": int(metrics.get("truncations") or 0),
            "illegal_actions": int(metrics.get("illegal_actions") or 0),
            "unclassified_dead_ends": int(metrics.get("unclassified_dead_ends") or 0),
            "steps": metrics.get("mean_steps"),
            # HP at termination is what separates "arrived at the boss already dying"
            # from "lost the boss fight on its own terms".
            "generated_act": acts[int(seed)],
            "final_hp_fraction": metrics.get("mean_final_hp_fraction"),
            "final_floor_histogram": metrics.get("final_floor_histogram"),
            # A truncation at the boss floor is two different findings depending on
            # why it stopped: step_cap means the fight never ended, empty_action_mask
            # means the run had nothing left to do.  Only the second can be a boss
            # already cleared and never judged a win (see docs/evidence/
            # act2_boss_completion_fork_20260919.json), and that distinction is lost
            # if the census records the category alone.
            "dead_end_reasons": metrics.get("dead_end_reasons"),
        })
        if index % 50 == 0 or index == len(window):
            print(f"{index}/{len(window)} {counts}", flush=True)

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": "simulator_act1",
        "checkpoint": str(checkpoint),
        "checkpoint_sha256": checkpoint_digest,
        "stage": stage.name,
        "split": args.split,
        "seed_source": seed_source,
        "seeds_outside_the_declared_partition": outside,
        "max_steps_per_episode": max_steps,
        "partition_size": len(seeds),
        "enumerated": len(window),
        "start_offset": args.start_offset,
        "counts": counts,
        "counts_by_generated_act": {
            str(act): {
                category: sum(1 for row in rows if row["generated_act"] == act
                              and row["category"] == category)
                for category in sorted({row["category"] for row in rows
                                        if row["generated_act"] == act})
            } for act in sorted({row["generated_act"] for row in rows})
        },
        "boss_arrival_seeds_by_act": {
            str(act): [row["seed"] for row in rows
                       if row["generated_act"] == act and row["final_floor"] == BOSS_FLOOR]
            for act in sorted({row["generated_act"] for row in rows})
        },
        # Named so the next step is a command, not an archaeology exercise.
        "boss_truncation_seeds": [row["seed"] for row in rows
                                  if row["category"] == "boss_truncation"],
        "rows": rows,
    }
    print(json.dumps({key: payload[key] for key in
                      ("enumerated", "counts", "boss_truncation_seeds")},
                     ensure_ascii=False))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
