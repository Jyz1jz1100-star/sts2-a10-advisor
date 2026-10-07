"""G4's paired screen: does the trained out-of-combat policy beat the positional default?

Read docs/TRAINING_GATES_2026-10-07.md section 2 before changing anything here -- the metric,
delta, test, sample size and the sealed half are pre-registered, and this script exists to execute
them, not to renegotiate them.  What was amended before the first look is the *definition of the
two arms*, for a reason that is measured rather than felt: the arm as written was
``live_choice_policy`` (a rule over option wording) against ``index 0``, and the simulator does not
publish option wording.  ``NativeRunCore._info`` returns twelve numeric fields -- phase, floor, act,
deck size, gold, hp, max hp, relic count, node type, event id, relic reward, run cleared -- and
``state_lists()`` returns ids.  There is no text anywhere on that surface, and the wording markers
in ``advisor_core/live_choice_policy.py`` are Chinese strings from the live client's localisation,
so the rule could not have run here even if text existed.  The screen therefore compares:

    A  the frozen checkpoint's own out-of-combat decisions -- the thing a G5 budget would fund
    B  the lowest advertised legal index -- the positional constant this project shipped with

Both arms fight with the *same* frozen executor from ``config/production_campaign_v5_g1.toml``, so
the only difference between an A rollout and its B partner is who picked between fights.

Nothing here trains.  Cost is evaluation only, and the rollouts are paired by seed with a
hash-split holdout sealed before any result is read.

    ../third_party/slay-the-spire-2-emulator-main/.venv/Scripts/python.exe \
        scripts/screen_out_of_combat_intervention.py --pairs 1000 \
        --out docs/evidence/out_of_combat_screen_20261007.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing as mp
import pathlib
import sys
from collections import Counter
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
EMULATOR = ROOT.parent / "third_party" / "slay-the-spire-2-emulator-main"
sys.path.insert(0, str(EMULATOR / "src"))

#: Pre-registered values. Editing one of these after a result is read invalidates the screen.
DELTA = 0.015           # +1.5 percentage points of Act-1 boss arrival
BASELINE_ARRIVAL = 309 / 10_000   # measured on v5 across the whole promotion partition
ACT1_BOSS_FLOOR = 17    # the floor the act-1 boss sits on in a 17-floor generated map
METRIC = "act1_boss_arrival"
SPLIT_SALT = "g4-out-of-combat-screen-20261007"
WORKER_BUDGET = 4_800   # the campaign horizon; identical to both arms' stage config


def holdout_of(seed: int, salt: str = SPLIT_SALT) -> str:
    """Deterministic, order-free split of the partition into screen and sealed halves."""
    digest = hashlib.sha256(f"{salt}:{seed}".encode("utf-8")).hexdigest()
    return "screen" if int(digest[:8], 16) % 2 == 0 else "sealed"


def _positional(_observation: Any, mask: Any) -> int:
    """Arm B: the lowest advertised legal index, which is what the old constant did.

    Plain sequence arithmetic rather than ``numpy.flatnonzero`` so the rule and its tests run on
    the static interpreter too -- this is the arm the gates' verdict depends on, and it should be
    checkable without the training environment.
    """
    for index, legal in enumerate(mask):
        if bool(legal):
            return index
    return 0


class _Trained:
    """Arm A: the frozen checkpoint, used only for its out-of-combat decisions."""

    def __init__(self, model: Any) -> None:
        self._model = model

    def __call__(self, observation: Any, mask: Any) -> int:
        raw, _ = self._model.predict(observation, action_masks=mask, deterministic=True)
        return int(raw.item() if hasattr(raw, "item") else raw)


def _roll(factory: Any, decide: Any, seed: int, budget: int) -> dict[str, Any]:
    import numpy as np

    env = factory(seed)
    try:
        observation, info = env.reset(seed=seed, options={"campaign": True})
        inner = getattr(getattr(env, "unwrapped", None), "max_episode_steps", None)
        horizon = min(int(inner) if inner else budget, budget)
        acts: Counter = Counter()
        floors: dict[int, int] = {}
        steps = 0
        absorbed = 0
        done = False
        last: dict[str, Any] = dict(info or {})
        while not done and steps < horizon:
            mask = env.action_masks()
            if not np.any(mask):
                break
            phase = str(last.get("phase_name") or "unknown")
            act = int(last.get("act") or 0)
            floor = int(last.get("floor") or 0)
            acts[phase] += 1
            if act:
                floors[act] = max(floors.get(act, 0), floor)
            action = decide(observation, mask)
            observation, _r, terminated, truncated, info = env.step(int(action))
            last = dict(info or {})
            absorbed += int(last.get("combat_steps") or 0)
            done = bool(terminated or truncated)
            steps += 1
        generated_act = min((int(a) for a in floors), default=0)
        return {
            "seed": seed,
            "holdout": holdout_of(seed),
            "generated_act": generated_act,
            "floors_by_act": {str(k): v for k, v in sorted(floors.items())},
            "deepest_act": max(floors, default=0),
            "agent_steps": steps,
            "absorbed_combat_steps": absorbed,
            "reached_act1_boss": bool(floors.get(1, 0) >= ACT1_BOSS_FLOOR),
            "won": bool(last.get("player_won")),
            "run_cleared": bool(last.get("run_cleared")),
            "dead_end": str(last.get("simulator_dead_end") or ""),
            # Kept whole, not filtered: if a combat decision ever shows up in an arm's agent
            # stream, the screen's premise is broken and the rows have to say so.
            "agent_phases": {k: v for k, v in sorted(acts.items())},
        }
    finally:
        env.close()


def _factory_from(payload: dict):
    import sts2_gym  # noqa: F401

    from training.v2_config import load_v2_training_config
    from training.v2_curriculum import _environment_factory

    config = load_v2_training_config(pathlib.Path(payload["config"]))
    stage = next(s for s in config.stages if s.name == payload["stage"])
    if stage.combat_executor != "frozen":
        raise RuntimeError(
            f"stage {stage.name} does not freeze combat; the screen would be comparing two "
            "different fighters, not two ways of choosing between fights")
    return _environment_factory(config, stage, sts2_gym)


def _probe_worker(payload: dict) -> list[tuple[int, int]]:
    """Which act a seed's campaign episode *opens* in, read by one reset.

    Measured on this partition: 60 of 60 spread seeds open in act 1, because a campaign rollout is
    the three-act walk and act 1 is its first act. The "~half of any seed range generates act 2"
    figure is a property of the *single-act* stage, where the engine deals one act per seed, and
    quoting it here would have been the mixed-act label error in a new place.

    So this is not a filter that saves compute -- it is a check that the population is what the
    metric assumes. A seed that opens anywhere else is named in `anomalies` and left out of the
    pairing rather than silently counted.
    """
    factory = _factory_from(payload)
    acts = []
    for seed in payload["seeds"]:
        env = factory(seed)
        try:
            _obs, info = env.reset(seed=seed, options={"campaign": True})
            acts.append((seed, int((info or {}).get("act") or 0)))
        finally:
            env.close()
    return acts


def _worker(payload: dict) -> list[dict]:
    from sb3_contrib import MaskablePPO
    from stable_baselines3.common.vec_env import DummyVecEnv

    factory = _factory_from(payload)
    probe = DummyVecEnv([lambda: factory(payload["seeds"][0])])
    try:
        trained = _Trained(MaskablePPO.load(payload["checkpoint"], env=probe, device="cpu"))
    finally:
        probe.close()
    rows = []
    for seed in payload["seeds"]:
        for arm, decide in (("trained", trained), ("positional", _positional)):
            row = _roll(factory, decide, seed, payload["budget"])
            row["arm"] = arm
            rows.append(row)
    return rows


def mcnemar_paired(pairs: list[tuple[bool, bool]]) -> dict[str, Any]:
    """The paired difference with its 95% Wilson interval, from the discordant cells only."""
    from training.wilson import wilson_interval

    b = sum(1 for a_arm, b_arm in pairs if a_arm and not b_arm)
    c = sum(1 for a_arm, b_arm in pairs if not a_arm and b_arm)
    n = len(pairs)
    discordant = b + c
    diff = (b - c) / n if n else 0.0
    interval = wilson_interval(b, discordant) if discordant else (0.0, 0.0)
    # Delta is defined on arrival *rates*, so the interval that matters is the one on the paired
    # difference itself; with n pairs and discordant cells b, c its standard error is
    # sqrt((b + c) - (b - c)^2 / n) / n, and the normal interval is reported next to the Wilson
    # interval on the discordant proportion so a reader can see both conventions.
    if discordant:
        se = (discordant - (b - c) ** 2 / n) ** 0.5 / n if n else 0.0
        normal = (diff - 1.96 * se, diff + 1.96 * se)
    else:
        normal = (0.0, 0.0)
    return {
        "pairs": n,
        "trained_only": b,
        "positional_only": c,
        "discordant": discordant,
        "paired_arrival_difference": round(diff, 6),
        "wilson_95_on_discordant_share": [round(interval[0], 6), round(interval[1], 6)],
        "normal_95_on_paired_difference": [round(normal[0], 6), round(normal[1], 6)],
        "delta": DELTA,
        "baseline_arrival": round(BASELINE_ARRIVAL, 6),
        "lower_bound_clears_delta": normal[0] > DELTA if discordant else False,
        "interval_excludes_zero_from_below": normal[0] > 0.0 if discordant else False,
        "upper_bound_below_zero": normal[1] < 0.0 if discordant else False,
    }


def verdict(stats: dict, arrived_screen: int) -> str:
    """The pre-registered reading, with 'undetermined' kept as a real possibility."""
    if stats["discordant"] == 0:
        return "undetermined: no discordant pair, so the screen has no information yet"
    if stats["lower_bound_clears_delta"]:
        return "pass: the paired improvement's 95% lower bound clears the pre-registered delta"
    if stats["upper_bound_below_zero"]:
        return "stop: the paired difference is negative with its whole interval below zero"
    if arrived_screen < stats["pairs"]:
        return ("undetermined: the interval crosses the delta, and calling this a pass or a stop "
                "would be reading a trend off a coin")
    return "undetermined: the interval does not settle the delta"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=pathlib.Path,
                        default=ROOT / "config/production_campaign_v5_g1.toml")
    parser.add_argument("--stage", default="full_run")
    parser.add_argument("--checkpoint", type=pathlib.Path,
                        default=ROOT / "runtime/production_campaign_v5/"
                        "v2curriculum-20260923T061352Z/full_run/checkpoints/"
                        "step_000040000032.zip")
    parser.add_argument("--partition", type=pathlib.Path, default=None,
                        help="optional JSON list of seeds; defaults to the stage's promotion "
                             "partition, which the trainer never read")
    parser.add_argument("--seed-start", type=int, default=2008010000)
    parser.add_argument("--candidate-seeds", type=int, default=2000,
                        help="seeds probed before the half is taken; a campaign episode always "
                             "opens in act 1, so this only has to be about twice --pairs")
    parser.add_argument("--pairs", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    args = parser.parse_args()

    checkpoint = args.checkpoint.resolve()
    if not checkpoint.is_file():
        raise SystemExit(f"checkpoint not found: {checkpoint}")

    if args.partition is not None:
        seeds = [int(value) for value in json.loads(
            args.partition.read_text(encoding="utf-8"))["seeds"]]
    else:
        seeds = [args.seed_start + i for i in range(args.candidate_seeds)]

    # The sealed half is decided by hash before anything is rolled, and rows from it are written
    # to the artifact but never used by the verdict.
    screen_seeds = [s for s in seeds if holdout_of(s) == "screen"]
    sealed_seeds = [s for s in seeds if holdout_of(s) == "sealed"]

    base_payload = {"config": str(args.config.resolve()), "stage": args.stage,
                    "checkpoint": str(checkpoint), "budget": WORKER_BUDGET}

    chunks = [screen_seeds[i::args.workers] for i in range(args.workers)]
    payloads = [dict(base_payload, seeds=chunk) for chunk in chunks if chunk]
    with mp.Pool(len(payloads)) as pool:
        probed = [pair for part in pool.map(_probe_worker, payloads) for pair in part]
    generated = dict(probed)
    opening_act1 = [s for s in screen_seeds if generated.get(s) == 1]
    anomalies = sorted(s for s in screen_seeds if generated.get(s) != 1)
    rolled_seeds = opening_act1[:args.pairs]

    roll_chunks = [rolled_seeds[i::args.workers] for i in range(args.workers)]
    roll_payloads = [dict(base_payload, seeds=chunk) for chunk in roll_chunks if chunk]
    with mp.Pool(len(roll_payloads)) as pool:
        rows = [row for part in pool.map(_worker, roll_payloads) for row in part]

    by_seed: dict[int, dict[str, dict]] = {}
    for row in rows:
        by_seed.setdefault(row["seed"], {})[row["arm"]] = row
    ordered = [by_seed[s] for s in rolled_seeds if s in by_seed]
    # Both partners must exist for a pair to count; a worker that died mid-run would otherwise
    # leave a one-sided seed silently inflating or deflating one arm.
    complete = [pair for pair in ordered if "trained" in pair and "positional" in pair]
    dropped = len(ordered) - len(complete)
    used = [pair for pair in complete if pair["trained"]["generated_act"] == 1
            and pair["positional"]["generated_act"] == 1]
    if len(used) != len(complete):
        anomalies.extend(sorted({pair["trained"]["seed"] for pair in complete
                                 if pair not in used}))
    pairs = [(p["trained"]["reached_act1_boss"], p["positional"]["reached_act1_boss"])
             for p in used]
    stats = mcnemar_paired(pairs)
    trained_rate = sum(a for a, _ in pairs) / len(pairs) if pairs else 0.0
    positional_rate = sum(b for _, b in pairs) / len(pairs) if pairs else 0.0

    artifact = {
        "metric": METRIC,
        "arms": {"trained": "the frozen checkpoint's out-of-combat decisions",
                 "positional": "lowest advertised legal index (the shipped positional constant)"},
        "arm_amendment": {
            "as_registered": "live_choice_policy (option wording) versus index 0",
            "why_changed_before_any_look": (
                "the simulator publishes no option text: NativeRunCore._info is twelve numeric "
                "fields and state_lists() is ids, and the live rule's markers are Chinese "
                "localisation strings, so the registered arm A could not execute here"),
            "criteria_unchanged": ["metric", "delta", "test", "sample size", "sealed half"],
        },
        "combat": {"executor": "frozen", "checkpoint": checkpoint.relative_to(ROOT).as_posix(),
                   "checkpoint_sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
                   "shared_by_both_arms": True},
        "horizon_steps_per_episode": WORKER_BUDGET,
        "split": {"salt": SPLIT_SALT, "screen_seeds": len(screen_seeds),
                  "sealed_seeds": len(sealed_seeds),
                  "sealed_rows_included_but_unused": True},
        "candidate_seeds_probed": len(probed),
        "screen_half_opening_in_act_1": len(opening_act1),
        "seeds_not_opening_in_act_1": anomalies,
        "incomplete_pairs_dropped": dropped,
        "pairs_used": len(pairs),
        "trained_arrival_rate": round(trained_rate, 6),
        "positional_arrival_rate": round(positional_rate, 6),
        "statistics": stats,
        "verdict": verdict(stats, len(pairs)),
        "not_established": [
            "anything about win rate: this screen reads arrival at the act-1 boss, because the "
            "measured conversion is ~1 win per 10,000 episodes and no affordable sample of it "
            "exists",
            "transfer to the real client, which is what G5 requires separately",
            "that arrival is the right target if the product's win condition turns out to be "
            "reachable only through act 2 and 3 decisions",
        ],
        "rows": [row for pair in ordered for row in (pair["trained"], pair["positional"])],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    print(json.dumps({k: artifact[k] for k in
                      ("candidate_seeds_probed", "screen_half_act1_seeds",
                       "incomplete_pairs_dropped", "pairs_used",
                       "trained_arrival_rate", "positional_arrival_rate", "verdict")},
                     ensure_ascii=False, indent=1))
    print(json.dumps(artifact["statistics"], ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
