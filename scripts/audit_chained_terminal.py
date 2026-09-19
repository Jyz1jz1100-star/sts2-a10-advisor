"""Audit the chained Act 1 -> Act 2 path against the objective's own gate clauses.

Every "0 illegal actions / 0 unclassified dead ends" figure in the campaign report was
measured on single-act populations, but the objective's end state is a *multi-act* run,
and the only multi-act path is the retained-trace seed ``7MS1YN8NWB``. This asks the two
questions that are still open on that path, using the code that owns each answer:

1. ``training.evaluation.evaluate_policy`` -- the function that decides whether a dead end
   is classified -- rolled over the checkpoints that were observed to chain into Act 2.
   Its ``unclassified_dead_ends`` and ``dead_end_reasons`` are the gate-relevant answer.
2. The terminal decision state itself, decoded from the native observation: which phase,
   which floor, which node type, and what the four map options hold. Two chained runs end
   on an Act-2 map at floor 19 with full-ish HP after ~230 steps, far below any cap, and
   the frontier artifact left that as an open question ("same empty-mask family as seed
   130012038?"). Whether the map really offers no successor is observable, so observe it
   instead of labelling it by resemblance.
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

DEMO_SEED = "7MS1YN8NWB"

from training.v2_constants import (  # noqa: E402
    COMBAT_OBS_SIZE,
    MAP_CHOICES,
    PHASE_MAP,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, action="append", required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "config/training_v2.toml")
    parser.add_argument("--stage", default="act1")
    parser.add_argument("--seed", default=DEMO_SEED)
    parser.add_argument("--max-steps", type=int, default=4000)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    if args.seed != DEMO_SEED:
        raise SystemExit(
            f"only {DEMO_SEED!r} can chain acts (RunEngine.cs:1909); any other seed makes "
            "this a single-act measurement and belongs in enumerate_act1_terminals.py"
        )

    import sts2_gym
    from sb3_contrib import MaskablePPO
    from stable_baselines3.common.vec_env import DummyVecEnv

    from training.evaluation import evaluate_policy
    from training.v2_config import load_v2_training_config
    from training.v2_curriculum import _environment_factory

    config = load_v2_training_config(args.config.resolve())
    stage = next(s for s in config.stages if s.name == args.stage)
    # The chain would be cut by a curriculum floor boundary, and the configured episode cap
    # was sized for one act, so both are lifted here deliberately and recorded below.
    open_stage = dataclasses.replace(stage, max_floor=None, max_episode_steps=args.max_steps)
    factory = _environment_factory(config, open_stage, sts2_gym)

    rows = []
    for checkpoint in args.checkpoint:
        checkpoint = checkpoint.resolve()
        probe = DummyVecEnv([lambda: factory(args.seed)])
        model = MaskablePPO.load(str(checkpoint), env=probe, device="cpu")

        metrics = evaluate_policy(
            model,
            env_factory=factory,
            seeds=[args.seed],
            stage=stage.name,
            split="retained_trace_demo",
            scope="simulator_chained_demo",
            checkpoint=str(checkpoint),
            max_steps_per_episode=args.max_steps,
        ).to_dict()

        # Second, independent read of the same run: the state the run actually stopped in.
        env = factory(args.seed)
        observation, info = env.reset(seed=args.seed)
        terminal = truncated = False
        steps = 0
        stop = None
        while not (terminal or truncated):
            mask = [index for index, on in enumerate(env.action_masks()) if bool(on)]
            if not mask:
                truncated = True
                stop = "empty_action_mask"
                break
            raw, _ = model.predict(observation, action_masks=env.action_masks(),
                                   deterministic=True)
            action = int(raw.item() if hasattr(raw, "item") else raw)
            if action not in mask:
                truncated = True
                stop = "policy_illegal_action"
                break
            observation, _reward, terminal, truncated, info = env.step(action)
            steps += 1
            if steps >= args.max_steps:
                truncated = True
                stop = "step_cap"
        flat = env.unwrapped
        raw = flat.raw_observation()
        state = flat.state_info()
        phase = int(raw[COMBAT_OBS_SIZE])
        options = [int(raw[COMBAT_OBS_SIZE + 12 + option]) for option in range(MAP_CHOICES)]
        rows.append({
            "checkpoint": str(checkpoint),
            "checkpoint_sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
            "evaluation_path": {
                key: metrics.get(key)
                for key in ("wins", "truncations", "illegal_actions", "unclassified_dead_ends",
                            "dead_end_reasons", "mean_final_floor", "max_final_floor",
                            "mean_final_hp_fraction", "defect_truncation_rate", "steps")
            },
            "state_path": {
                "steps": steps,
                "stop": stop,
                "phase": state.get("phase_name"),
                "phase_is_map": phase == PHASE_MAP,
                "floor": state.get("floor"),
                "act": state.get("act"),
                "current_node_type": state.get("current_node_type"),
                "encounter_id": state.get("encounter_id"),
                "event_id": state.get("event_id"),
                "player_hp": state.get("player_hp"),
                "player_max_hp": state.get("player_max_hp"),
                "player_won_flag": state.get("player_won"),
                "map_option_node_types": options,
                "map_options_available": sum(1 for node in options if node != 0),
            },
        })
        env.close()
        print(f"{checkpoint.name}: metrics={rows[-1]['evaluation_path']} "
              f"state=floor {rows[-1]['state_path']['floor']} "
              f"map_options={rows[-1]['state_path']['map_options_available']}")

    payload = {
        "_comment": [
            "Two independent reads of the same chained runs: the metrics path that owns the",
            "'unclassified dead end' judgement, and the decoded terminal state.",
            "map_options_available == 0 means the engine genuinely offered no successor node,",
            "which is a map-exhaustion dead end; a non-zero count with an empty action mask",
            "would instead mean the mask and the map disagreed.",
            "The step cap and the curriculum floor were both lifted on purpose (recorded in",
            "max_steps_per_run) because the configured 1600-step cap is sized for one act.",
        ],
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "max_steps_per_run": args.max_steps,
        "rows": rows,
        "scope": "simulator_chained_demo (retained-trace seed; evidences the harness path, not policy generalisation)",
        "seed": args.seed,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
