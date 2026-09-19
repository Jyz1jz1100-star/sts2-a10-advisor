"""Roll a checkpoint through the emulator's only multi-act flow.

The simulator has two acts (``RunConstants.cs:35-36``) and exactly one act
chaining path: ``RunEngine.cs:1907-1920`` continues into Act 2 past the Act 1
boss only when ``StringSeed == "7MS1YN8NWB"``. That seed is a retained trace,
so its Act 1 map, encounters and boss are hardcoded
(``RunMapGenerator.cs:89-113`` and ``:792-947``).

So a victory here evidences "the harness can express and finish a two-act
flow", NOT "the policy generalises across acts" — a scripted act cannot
evidence generalisation. Both readings are printed in the output so the
artifact cannot be quoted out of that context.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "third_party" / "slay-the-spire-2-emulator-main" / "src"))
sys.path.insert(0, str(ROOT))

CHAINED_SEED = "7MS1YN8NWB"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config/training_v2.toml")
    parser.add_argument("--stage", default="act1")
    parser.add_argument("--checkpoint", type=Path, action="append", required=True)
    parser.add_argument("--seed", default=CHAINED_SEED)
    parser.add_argument("--max-steps", type=int, default=60_000)
    parser.add_argument("--sampled", action="store_true",
                        help="sample instead of taking the argmax, to test whether the "
                             "argmax policy is what locks a boss fight into a stalemate")
    parser.add_argument("--sample-seed", type=int, default=0)
    parser.add_argument("--repeats", type=int, default=1,
                        help="roll the same seed N times; only meaningful with --sampled")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    if args.seed != CHAINED_SEED:
        raise SystemExit(
            f"{args.seed!r} cannot chain acts: only {CHAINED_SEED!r} reaches Act 2 "
            "(RunEngine.cs:1909). Any other seed ends at the Act 1 or Act 2 boss."
        )

    import sts2_gym
    from sb3_contrib import MaskablePPO
    from stable_baselines3.common.vec_env import DummyVecEnv

    from training.v2_config import load_v2_training_config
    from training.v2_curriculum import _environment_factory

    config = load_v2_training_config(args.config.resolve())
    stage = next((s for s in config.stages if s.name == args.stage), None)
    if stage is None:
        raise SystemExit(f"stage {args.stage!r} not in {args.config}")
    # A curriculum boundary would truncate at its own floor and hide the chain,
    # and the configured episode cap was sized for one act.
    open_stage = dataclasses.replace(stage, max_floor=None, max_episode_steps=args.max_steps)
    factory = _environment_factory(config, open_stage, sts2_gym)

    results = []
    for checkpoint in args.checkpoint:
        checkpoint = checkpoint.resolve()
        probe = DummyVecEnv([lambda: factory(args.seed)])
        model = MaskablePPO.load(str(checkpoint), env=probe, device="cpu")
        for repeat in range(args.repeats):
            if args.sampled:
                import torch

                torch.manual_seed(args.sample_seed + repeat)
            deterministic = not args.sampled
            env = factory(args.seed)
            observation, info = env.reset(seed=args.seed)
            trace: list[dict[str, object]] = []
            last = None
            steps = 0
            terminal = truncated = False
            illegal_actions = 0
            dead_end = None
            # Termination, win and illegal-action semantics are copied from
            # training/evaluation.py so this probe cannot drift from the contract
            # the campaign numbers were measured under.
            while not (terminal or truncated):
                marker = (info.get("act"), info.get("floor"), info.get("phase_name"))
                if marker != last:
                    trace.append({"act": marker[0], "floor": marker[1], "phase": marker[2]})
                    last = marker
                mask = env.action_masks()
                if not any(bool(value) for value in mask):
                    truncated = True
                    dead_end = "empty_action_mask"
                    break
                action_raw, _ = model.predict(
                    observation, action_masks=mask, deterministic=deterministic
                )
                action = int(action_raw.item() if hasattr(action_raw, "item") else action_raw)
                if action < 0 or action >= len(mask) or not bool(mask[action]):
                    truncated = True
                    illegal_actions += 1
                    dead_end = "policy_illegal_action"
                    break
                observation, _reward, terminal, truncated, info = env.step(action)
                steps += 1
                if steps >= args.max_steps:
                    truncated = True
                    dead_end = "step_cap"
            won = bool(terminal and info.get("player_won", False))
            final = {
                "repeat": repeat,
                "policy_mode": "argmax" if deterministic else f"sampled({args.sample_seed}+{repeat})",
                "seed": args.seed,
                "checkpoint": str(checkpoint),
                "checkpoint_sha256": _sha256(checkpoint),
                "steps": steps,
                "illegal_actions": illegal_actions,
                "dead_end": dead_end,
                "final_act": info.get("act"),
                "final_floor": info.get("floor"),
                "final_phase": info.get("phase_name"),
                "run_won": won,
                "run_outcome": info.get("run_outcome"),
                "run_outcome_source": info.get("run_outcome_source"),
                # A run can stop without the policy dying: the retained-trace seed
                # carries an environment-side truncation signal of its own.  Without
                # these four fields "reached Act 2 then stopped" reads as a death.
                "final_player_hp": info.get("player_hp"),
                "final_player_max_hp": info.get("player_max_hp"),
                "run_terminated": info.get("run_terminated"),
                "run_truncated": info.get("run_truncated"),
                "reached_act_two": any(t["act"] and int(t["act"]) >= 2 for t in trace),
                "max_act": max((int(t["act"]) for t in trace if t["act"]), default=0),
                "max_floor": max((int(t["floor"]) for t in trace if t["floor"]), default=0),
                "trace": trace,
            }
            env.close()
            results.append(final)
            print(
                f"{checkpoint.name} r{repeat}: act={final['final_act']} "
                f"floor={final['final_floor']} phase={final['final_phase']} "
                f"hp={final['final_player_hp']}/{final['final_player_max_hp']} won={won} "
                f"illegal={illegal_actions} dead_end={dead_end} steps={steps} "
                f"reached_act2={final['reached_act_two']}"
            )

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": "simulator_chained_demo_seed",
        "evidences": "two-act flow reachable from the V2 harness",
        "does_not_evidence": [
            "policy generalisation across acts (Act 1 of this seed is a hardcoded trace)",
            "a three-act clear (the emulator has no Act 3)",
            "real-game A10 acceptance",
        ],
        "chained_branch_source": "third_party/.../RunEngine.cs:1907-1920",
        "results": results,
    }
    print(
        "\nScope: this seed's Act 1 is scripted (RunMapGenerator.cs:792-947), so a win "
        "here shows the harness can finish a two-act flow, not that the policy transfers."
    )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
