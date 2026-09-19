"""How deep can the chained Act 2 be walked, over *all* map choices instead of one policy roll?

`chained_map_deadend_fork_20260920.json` established that two of the three chains end in an engine
map state with zero legal options at Act 2 floor 19, and left one thing unmeasured: whether an
earlier node choice avoids that dead end. This searches the map-choice tree rather than sampling a
single path through it, so the answer is about reachability under this policy's combat behaviour
rather than about one lucky sequence of map clicks.

What this can claim: which Act-2 floors are reachable when the *map choice* is varied freely while
the trained policy still plays every combat. What it cannot claim: that a two-act win exists --
reachable is not survivable. Every fork is entered by replaying the recorded action prefix into a
fresh environment, and each replay is checked against the state its parent actually observed; a
disagreement is reported (and exits non-zero) rather than explored further.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "third_party" / "slay-the-spire-2-emulator-main" / "src"))

TARGET_SLOTS = 7
MAP_PHASE = "map"
ACT_TWO = 2
NODE_BOSS = 6  # RunConstants.NodeBoss, as exposed by state_info()['current_node_type']


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "config/training_v2.toml")
    parser.add_argument("--stage", default="act1")
    parser.add_argument("--seed", default="7MS1YN8NWB")
    parser.add_argument("--max-steps", type=int, default=4_000)
    parser.add_argument("--max-forks", type=int, default=60,
                        help="how many Act-2 map decisions may be expanded in total")
    parser.add_argument("--max-depth", type=int, default=8,
                        help="how many Act-2 map decisions deep the tree is explored")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    import sts2_gym
    from sb3_contrib import MaskablePPO
    from stable_baselines3.common.vec_env import DummyVecEnv

    from training.v2_config import load_v2_training_config
    from training.v2_curriculum import _environment_factory

    config = load_v2_training_config(args.config.resolve())
    stage = next(s for s in config.stages if s.name == args.stage)
    open_stage = dataclasses.replace(stage, max_floor=None, max_episode_steps=args.max_steps)
    seed = int(args.seed) if str(args.seed).lstrip("-").isdigit() else args.seed
    factory = _environment_factory(config, open_stage, sts2_gym)
    model = MaskablePPO.load(str(args.checkpoint.resolve()),
                             env=DummyVecEnv([lambda: factory(seed)]), device="cpu")

    def marker_of(info) -> tuple:
        return (info.get("act"), info.get("floor"), info.get("phase_name"))

    def replay(prefix: list[int]):
        """Fresh environment with `prefix` replayed; returns (env, observation, info, ended)."""
        env = factory(seed)
        observation, info = env.reset(seed=seed)
        for action in prefix:
            observation, _reward, terminated, truncated, info = env.step(action)
            if terminated or truncated:
                return env, observation, info, True
        return env, observation, info, False

    def policy_walk(env, observation, info):
        """Run the trained policy from `env`'s state until an Act-2 map decision or episode end."""
        actions: list[int] = []
        for _ in range(args.max_steps):
            if info.get("phase_name") == MAP_PHASE and info.get("act") == ACT_TWO:
                return "at_map", info, actions
            flat_mask = env.action_masks()
            if not any(bool(value) for value in flat_mask):
                return "no_legal_action", info, actions
            raw, _ = model.predict(observation, action_masks=flat_mask, deterministic=True)
            action = int(raw.item() if hasattr(raw, "item") else raw)
            actions.append(action)
            observation, _r, terminated, truncated, info = env.step(action)
            if terminated:
                return ("win" if info.get("player_won") else "death"), info, actions
            if truncated:
                return "truncated", info, actions
        return "step_cap", info, actions

    def advance(prefix: list[int]):
        env, observation, info, ended = replay(prefix)
        if ended:
            return "episode_over", info, []
        return policy_walk(env, observation, info)

    def map_options(prefix: list[int]) -> list[int]:
        """Engine-legal map bases at the fork state, paired with a flat action that carries each."""
        env, _observation, _info, ended = replay(prefix)
        if ended:
            return []
        native = env.unwrapped.base_action_mask()
        flat = env.action_masks()
        lowest: dict[int, int] = {}
        for flat_index in range(len(flat)):
            if flat[flat_index]:
                lowest.setdefault(flat_index // TARGET_SLOTS, flat_index)
        return [(base, lowest[base]) for base in sorted(lowest)
                if base < len(native) and bool(native[base])]

    leaves: list[dict] = []
    mismatches: list[dict] = []
    boss_states = 0
    forks_used = 0
    queue: list[tuple[list[int], list[int], int]] = [([], [], 0)]  # prefix, chosen bases, depth
    while queue:
        prefix, path, depth = queue.pop(0)
        state, info, tail = advance(prefix)
        full = prefix + tail
        act, floor = info.get("act") or 0, info.get("floor") or 0
        if act == ACT_TWO and info.get("current_node_type") == NODE_BOSS:
            boss_states += 1
        leaf = {"stop_state": state, "act": act, "floor": floor,
                "hp": info.get("player_hp"), "map_choices": path,
                "steps_in_episode": len(full)}
        if state != "at_map":
            leaves.append(leaf)
            continue
        replayed = marker_of(replay(full)[2])
        if replayed[:2] != (act, floor):
            mismatches.append({"expected": [act, floor], "replayed": list(replayed[:2]),
                               "map_choices": path})
            continue
        if depth >= args.max_depth or forks_used >= args.max_forks:
            leaf["stop_state"] = "search_budget"
            leaves.append(leaf)
            continue
        options = map_options(full)
        forks_used += 1
        if not options:
            leaf["stop_state"] = "engine_map_dead_end"
            leaves.append(leaf)
            continue
        for base, flat in options:
            queue.append((full + [flat], path + [base], depth + 1))

    dead_ends = [leaf for leaf in leaves if leaf["stop_state"] == "engine_map_dead_end"]
    deepest = max(((leaf["act"], leaf["floor"]) for leaf in leaves), default=(0, 0))
    payload = {
        "aggregates": {
            "act2_floors_reached": sorted({leaf["floor"] for leaf in leaves if leaf["act"] == ACT_TWO}),
            "boss_node_states_observed_in_act2": boss_states,
            "deepest_act_floor": list(deepest),
            "engine_map_dead_ends_found": len(dead_ends),
            "floor_reached_at_each_dead_end": dict(sorted(collections.Counter(
                leaf["floor"] for leaf in dead_ends).items())),
            "leaves": len(leaves),
            "map_decisions_expanded": forks_used,
            "replay_mismatches": len(mismatches),
            "stop_states": dict(sorted(collections.Counter(
                leaf["stop_state"] for leaf in leaves).items())),
            "wins": sum(1 for leaf in leaves if leaf["stop_state"] == "win"),
        },
        "checkpoint": str(args.checkpoint.resolve()),
        "checkpoint_sha256": hashlib.sha256(args.checkpoint.resolve().read_bytes()).hexdigest(),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "leaves": sorted(leaves, key=lambda leaf: (-leaf["act"], -leaf["floor"])),
        "limits": {"max_depth": args.max_depth, "max_forks": args.max_forks,
                   "max_steps_per_episode": args.max_steps},
        "not_established": [
            "that a two-act win exists: the trained policy plays every combat, so a reachable floor "
            "is not a survivable floor",
            "completeness beyond max_depth / max_forks: the tree is bounded, and 'search_budget' "
            "leaves are where it was cut, not where the map ends",
            "anything about Act 3 (the emulator defines two acts, RunConstants.cs:35-36)"],
        "replay_mismatches": mismatches,
        "scope": "Act-2 map choices explored breadth-first to a bounded depth, one checkpoint",
        "seed": args.seed,
    }
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8")
    agg = payload["aggregates"]
    print(f"expanded {agg['map_decisions_expanded']} map decisions -> {agg['leaves']} leaves; "
          f"deepest {agg['deepest_act_floor']}; Act-2 floors reached {agg['act2_floors_reached']}; "
          f"dead ends at {agg['floor_reached_at_each_dead_end']}; "
          f"boss states {agg['boss_node_states_observed_in_act2']}; wins {agg['wins']}; "
          f"replay mismatches {agg['replay_mismatches']}")
    print(f"wrote {args.out}")
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
