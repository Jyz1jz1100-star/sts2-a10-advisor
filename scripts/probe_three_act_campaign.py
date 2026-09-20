"""Walk named seeds through all three acts and record how far each one gets.

The emulator draws one act per seed, so every number recorded before 2026-09-20 describes a
single-act run.  Campaign mode makes the three-act walk expressible, and this is the instrument
that measures it: per seed it records the final floor (which identifies the act the run died in,
because the act bands are 1-17 / 18-33 / 34-50), the illegal-action count, and whether the engine
itself reported the run cleared -- not whether the last combat happened to be won.

Nothing here is a claim about the real game client; the scope is labelled simulator_three_act.

    python scripts/probe_three_act_campaign.py --config config/training_v2.toml \
        --checkpoint checkpoints/.../best.zip --seeds 20000039,20000102
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

# The engine's own act bands: act A ends at MapBossRow*A + 1, and the final act spends one more
# floor on its paired boss.  Used only to name where a run stopped, never to decide a result.
ACT_BANDS = ((1, 17), (18, 33), (34, 50))


def act_of_floor(floor: int | None) -> int | None:
    if floor is None:
        return None
    for act, (low, high) in enumerate(ACT_BANDS, start=1):
        if low <= floor <= high:
            return act
    return None


def parse_seed_list(spec: str) -> list[int]:
    seeds = [int(token) for token in spec.split(",") if token.strip()]
    duplicates = sorted({seed for seed in seeds if seeds.count(seed) > 1})
    if duplicates:
        raise SystemExit(
            f"--seeds lists {duplicates} more than once; a repeated seed would be counted twice"
        )
    if not seeds:
        raise SystemExit("--seeds is empty")
    return seeds


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=ROOT / "config/training_v2.toml")
    parser.add_argument("--stage", default="act1")
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--seeds", required=True, type=parse_seed_list)
    parser.add_argument(
        "--max-steps",
        type=int,
        help="defaults to three times the stage horizon, since a campaign is three acts long",
    )
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    if not args.checkpoint.is_file():
        raise SystemExit(f"no checkpoint at {args.checkpoint}")

    import sts2_gym

    from sb3_contrib import MaskablePPO
    from stable_baselines3.common.vec_env import DummyVecEnv

    from training.evaluation import evaluate_policy
    from training.v2_config import load_v2_training_config
    from training.v2_curriculum import _environment_factory

    config = load_v2_training_config(args.config.resolve())
    stage = next(s for s in config.stages if s.name == args.stage)
    max_steps = args.max_steps or 3 * stage.max_episode_steps
    factory = _environment_factory(
        config, stage, sts2_gym, max_episode_steps=max_steps
    )
    probe = DummyVecEnv([lambda: factory(args.seeds[0])])
    model = MaskablePPO.load(str(args.checkpoint), env=probe, device="cpu")

    rows = []
    for seed in args.seeds:
        metrics = evaluate_policy(
            model,
            env_factory=factory,
            seeds=[seed],
            stage=stage.name,
            split="three_act_probe",
            scope="simulator_three_act",
            checkpoint=args.checkpoint,
            max_steps_per_episode=max_steps,
            campaign=True,
        ).to_dict()
        floor = metrics["max_final_floor"]
        rows.append({
            "seed": seed,
            "final_floor": floor,
            "act_died_in": act_of_floor(floor),
            "campaign_cleared": int(metrics.get("campaign_clears", 0)),
            "won_last_combat": int(metrics["wins"]),
            "illegal_actions": int(metrics["illegal_actions"]),
            "truncated": int(metrics["truncations"]),
            "steps": metrics["mean_steps"],
        })
        print(f"  seed {seed}: {rows[-1]}", flush=True)

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "invocation": " ".join(sys.argv),
        "scope": "simulator_three_act",
        "config": str(args.config),
        "config_sha256": hashlib.sha256(args.config.read_bytes()).hexdigest(),
        "checkpoint": str(args.checkpoint),
        "checkpoint_sha256": hashlib.sha256(args.checkpoint.read_bytes()).hexdigest(),
        "max_steps_per_episode": max_steps,
        "act_bands": {"-".join(map(str, band)): index for index, band in enumerate(ACT_BANDS, 1)},
        "seeds": args.seeds,
        "rows": rows,
        "campaign_clears": sum(row["campaign_cleared"] for row in rows),
        "deepest_act_reached": max(
            (row["act_died_in"] or 0) for row in rows
        ) if rows else None,
    }
    print(json.dumps({key: payload[key] for key in
                      ("campaign_clears", "deepest_act_reached", "seeds")}, ensure_ascii=False))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
