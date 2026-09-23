"""Prove the full-run training *machinery* works, without claiming anything about strength.

Eleven checks, each one a way this project has been burned before: a reset that
does not match the contract, a mask that drifts with act number, a reward that goes
non-finite deep in a run, an episode that never crosses an act boundary, `player_won`
being read as a run clear, an illegal action absorbed silently, a split that leaks
seeds, a checkpoint that cannot be reloaded, an evaluation that is not reproducible,
and a result file that never says which world the numbers came from.

Run it with the training interpreter:

    <third_party>\\slay-the-spire-2-emulator-main\\.venv\\Scripts\\python.exe ^
        scripts/smoke_full_run_pipeline.py --out runtime/full_run_smoke.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "third_party" / "slay-the-spire-2-emulator-main" / "src"))
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import sts2_gym  # noqa: E402

from training.campaign_content import (  # noqa: E402
    CAMPAIGN_CONTENT_COVERAGE,
    CAMPAIGN_ENVIRONMENT_VERSION,
)
from training.evaluation import evaluate_policy  # noqa: E402
from training.metrics import EvaluationMetrics  # noqa: E402

OBS_KEY = "obs"
MAX_STEP_MAGNITUDE = 100.0


class _ArgmaxPolicy:
    """Legal-by-construction actor: it can only pick an unmasked action.

    Using a real network here would test PyTorch, not the environment contract, and
    an illegal action caused by a random actor would be indistinguishable from a
    mask bug.
    """

    def predict(self, observation, *, action_masks, deterministic=True):
        mask = np.asarray(next(iter(action_masks)), dtype=bool).ravel()
        legal = np.flatnonzero(mask)
        action = int(legal[0]) if legal.size else 0
        return np.array([action]), None


def _mask_width(env) -> int:
    """Accept both the raw env's array and a Vec wrapper's list of arrays."""
    masks = np.asarray(env.action_masks())
    return int(masks.shape[-1])


def _env(seed: int, *, steps: int):
    return sts2_gym.Sts2RunEnv(seed=seed, max_episode_steps=steps, max_floors=60)


def _campaign_env(seed: int, *, steps: int):
    env = _env(seed, steps=steps)
    return env, True


def check(name: str, passed: bool, detail: str, results: list) -> None:
    results.append({"check": name, "passed": bool(passed), "detail": detail})
    print(("  PASS " if passed else "  FAIL ") + name + " :: " + detail)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episodes", type=int, default=6)
    parser.add_argument("--max-steps", type=int, default=1600)
    parser.add_argument("--seeds", type=int, default=3)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    results: list[dict] = []
    base = 130000000  # outside every recorded population's ranges

    # 1/2. reset and contract stability, flat vs campaign
    dims: set[tuple[int, int]] = set()
    for campaign in (False, True):
        env = _env(base, steps=args.max_steps)
        obs, info = env.reset(seed=base, options={"campaign": campaign} if campaign else None)
        ok_reset = obs is not None and np.asarray(obs).ndim >= 1
        dims.add((int(np.asarray(obs).shape[-1]), _mask_width(env)))
        check(f"reset_ok[campaign={campaign}]", ok_reset,
              f"obs shape={np.asarray(obs).shape} info keys={sorted(info)}", results)
    check("observation_and_mask_dims_stable", len(dims) == 1,
          f"distinct (obs_width, action_space) pairs={sorted(dims)}", results)

    # 3. rewards finite and bounded across a long campaign walk
    env = _env(base + 1, steps=args.max_steps)
    env.reset(seed=base + 1, options={"campaign": True})
    rewards: list[float] = []
    illegal_seen = 0
    acts_seen: set[int] = set()
    for _ in range(args.max_steps):
        mask = np.asarray(env.action_masks()[0], dtype=bool)
        legal = np.flatnonzero(mask)
        if legal.size == 0:
            break
        obs, reward, term, trunc, info = env.step(int(legal[0]))
        rewards.append(float(reward))
        acts_seen.add(int(info.get("act_index") or 0))
        illegal_seen += int(info.get("illegal_actions") or 0)
        if term or trunc:
            break
    finite = all(math.isfinite(r) for r in rewards)
    bounded = all(abs(r) <= MAX_STEP_MAGNITUDE for r in rewards)
    check("rewards_finite_and_bounded", finite and bounded and rewards,
          f"steps={len(rewards)} min={min(rewards):.3f} max={max(rewards):.3f} "
          f"|finite|={finite} bounded={bounded}", results)

    # 4. One episode must cross an act boundary inside itself, and the training
    # stack must be the thing that carries it.
    #
    # This check used to ask whether seed 130008177 ended at floor >= 34. That is a
    # question about how strong one particular Act-1 checkpoint is, asked of a smoke
    # whose whole purpose is whether the machinery works -- so it has been red since
    # fidelity-v2 removed the invented boss relic (that seed's deepest floor went
    # 50 -> 19, then 15 under v4) while the boundary mechanism it names was, and is,
    # intact. Depth is a strength number and belongs in the report, not in the verdict.
    #
    # What is measured now is the act an episode was actually *in*, which is not the
    # act it was dealt: `act` is read at reset, so a run that beats the Act 1 boss and
    # dies in Hive reported act=1 and looked identical to one that never left. The
    # seeds are the four the v4 gate sweep measured as crossing, and the control is
    # the same walk in single-act mode, where no number of good play can produce a
    # crossing -- so a counter that reads crossings from deep floors fails here.
    ckpt_path = ROOT / ("runtime/fanout/b_terminal-1/v2curriculum-20260918T182830Z/act1/"
                        "checkpoints/step_000004000032.zip")
    crossing_seed_file = ROOT / "data/seeds/act_boundary_crossing_v5.json"
    if ckpt_path.is_file() and crossing_seed_file.is_file():
        crossing_decl = json.loads(crossing_seed_file.read_text(encoding="utf-8"))
        crossing_seeds = [int(seed) for seed in crossing_decl["seeds"]]
        # The checkpoint was trained behind the V2 observation expansion (width
        # 1739), so feeding it the flat 199-wide env would be a shape error, not
        # a result.  Reuse the curriculum's own factory rather than a parallel one.
        from sb3_contrib import MaskablePPO as _Probe
        from training.v2_config import load_v2_training_config
        from training.v2_curriculum import _environment_factory
        v2_config = load_v2_training_config(ROOT / "runtime/fanout/b_terminal-1.toml")
        v2_stage = next(s2 for s2 in v2_config.stages if s2.name == "act1")
        v2_factory = _environment_factory(v2_config, v2_stage, sts2_gym,
                                          max_episode_steps=4800)
        probe_model = _Probe.load(str(ckpt_path), device="cpu")
        probe_metrics = evaluate_policy(
            probe_model,
            env_factory=v2_factory,
            seeds=crossing_seeds, stage="full_run", split="checkpoint",
            scope="simulator_full_run", checkpoint=ckpt_path,
            max_steps_per_episode=4800, campaign=True,
        ).to_dict()
        flat_metrics = evaluate_policy(
            probe_model,
            env_factory=v2_factory,
            seeds=crossing_seeds[:2], stage="full_run", split="checkpoint",
            scope="simulator_full_run", checkpoint=ckpt_path,
            max_steps_per_episode=4800, campaign=False,
        ).to_dict()
        crossed_n = int(probe_metrics.get("episodes_that_crossed_an_act_boundary") or 0)
        control_n = int(flat_metrics.get("episodes_that_crossed_an_act_boundary") or 0)
        # Every one of the four must carry: a check that passed on one of four would
        # be reporting the luckiest seed, and the control must be capable of saying
        # zero, or it is not a control.
        crossed = crossed_n == len(crossing_seeds) and control_n == 0
        reached = int(probe_metrics.get("max_final_floor") or 0)
        detail = (
            f"{crossed_n}/{len(crossing_seeds)} of the four v4-measured crossing seeds "
            f"ended in a deeper act than they started, in the training stack itself; "
            f"single-act control crosses={control_n}; deepest floor reached={reached}, "
            f"campaign_clears={probe_metrics.get('campaign_clears')} -- act-3 reach "
            f"and any win are strength, not this contract"
        )
    else:
        crossed, detail = False, (
            f"missing {ckpt_path.name} or {crossing_seed_file.name}: the crossing "
            "seeds are a measurement, so without them this is unmeasured, not clean"
        )
    if crossed and crossing_decl.get("environment_version") != CAMPAIGN_ENVIRONMENT_VERSION:
        # These seeds were measured as crossings in one environment. A new version
        # has to re-measure them, not inherit a pass from the old one -- which is
        # the same rule the checkpoint split already follows, applied to a seed list.
        crossed = False
        detail = (
            f"crossing seeds are declared for {crossing_decl.get('environment_version')!r} "
            f"but the environment is now {CAMPAIGN_ENVIRONMENT_VERSION!r}; re-measure them "
            f"before this check can pass again. {detail}"
        )
    check("episode_crosses_act_boundaries", crossed, detail, results)

    info_keys = sorted(k for k in info if k in {"player_won", "run_cleared", "act_index"})
    term_flags = (bool(term), bool(trunc), bool(info.get("run_cleared")),
                  bool(info.get("player_won")))
    check("terminal_semantics_are_distinct",
          {"run_cleared", "player_won"} <= set(info_keys),
          f"info exposes {info_keys}; (terminated,truncated,run_cleared,player_won)="
          f"{term_flags} -- player_won alone must never be read as a clear", results)

    # 6. no illegal action while obeying the mask
    check("illegal_actions_zero_under_mask", illegal_seen == 0,
          f"illegal_actions={illegal_seen}", results)

    # 7. split isolation
    import tomllib
    cfg = tomllib.loads((ROOT / "config/training.toml").read_bytes().decode("utf-8"))
    ranges = {name: (int(cfg["seeds"][name]["start"]), int(cfg["seeds"][name]["count"]))
              for name in ("train", "checkpoint", "promotion", "final")}
    pairs = [(a, b) for ia, a in enumerate(sorted(ranges)) for b in sorted(ranges)[ia + 1:]]
    def overlap(x, y):
        xs, xn = ranges[x]; ys, yn = ranges[y]
        return max(xs, ys) < min(xs + xn, ys + yn)
    leaking = [f"{x}/{y}" for x, y in pairs if overlap(x, y)]
    check("seed_splits_disjoint", not leaking, f"ranges={ranges} leaking={leaking}", results)

    # 8/9. checkpoint save -> reload -> independent evaluation reproduces
    from sb3_contrib import MaskablePPO
    from stable_baselines3.common.vec_env import DummyVecEnv

    with tempfile.TemporaryDirectory() as tmp:
        train_seeds = [base + 10 + i for i in range(args.seeds)]
        env_fns = [lambda s=s: _campaign_env(s, steps=args.max_steps)[0]
                    for s in train_seeds[:1]]
        model = MaskablePPO("MlpPolicy", DummyVecEnv(env_fns),
                            n_steps=64, batch_size=32, verbose=0, device="cpu")
        model.learn(total_timesteps=128, progress_bar=False)
        ckpt = Path(tmp) / "smoke.zip"
        model.save(str(ckpt))
        digest = hashlib.sha256(ckpt.read_bytes()).hexdigest()
        reload_model = MaskablePPO.load(str(ckpt), device="cpu")
        eval_seeds = [base + 500 + i for i in range(args.episodes)]
        first = evaluate_policy(reload_model, env_factory=lambda s: _campaign_env(s, steps=args.max_steps)[0],
                               seeds=eval_seeds, stage="full_run", split="checkpoint",
                               scope="simulator_full_run", checkpoint=ckpt,
                               max_steps_per_episode=args.max_steps, campaign=True)
        again = evaluate_policy(reload_model, env_factory=lambda s: _campaign_env(s, steps=args.max_steps)[0],
                               seeds=eval_seeds, stage="full_run", split="checkpoint",
                               scope="simulator_full_run", checkpoint=ckpt,
                               max_steps_per_episode=args.max_steps, campaign=True)
        same = first.to_dict()["campaign_clears"] == again.to_dict()["campaign_clears"] \
            and first.to_dict()["seed_sha256"] == again.to_dict()["seed_sha256"]
        check("checkpoint_round_trips", ckpt.is_file() and bool(digest), f"sha256={digest[:16]}…", results)
        check("checkpoint_to_evaluation_reproducible", same,
              f"campaign_clears={first.to_dict()['campaign_clears']} "
              f"wins={first.to_dict()['wins']} seed_sha256={first.to_dict()['seed_sha256'][:12]}…", results)

        # 10. provenance and environment labelling reach the artifact
        payload = first.to_dict()
        labelled = (payload.get("environment_version") == CAMPAIGN_ENVIRONMENT_VERSION
                    and payload.get("content_coverage", {}).get("verdict") == "approximate")
        check("artifacts_carry_environment_version_and_content_scope", labelled,
              f"environment_version={payload.get('environment_version')!r} "
              f"scope={payload.get('scope')!r} verdict="
              f"{payload.get('content_coverage', {}).get('verdict')!r}", results)

    report = {
        "schema_version": 1,
        "purpose": "training-pipeline availability only; no strength claim",
        "environment_version": CAMPAIGN_ENVIRONMENT_VERSION,
        "content_scope": CAMPAIGN_CONTENT_COVERAGE["result_tier"],
        "trained_on": "simulator campaign env (stages 2 and 3 are Underdocks pools)",
        "not_trained_on": "the real client; real A10 acceptance is a separate evidence chain",
        "checks": results,
        "passed": all(r["passed"] for r in results),
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    print(f"\n{sum(r['passed'] for r in results)}/{len(results)} checks passed")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
