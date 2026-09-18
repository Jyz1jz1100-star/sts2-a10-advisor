"""Re-run a recorded evaluation from its checkpoint and prove the win reproduces.

A metrics file that quotes ``checkpoint_sha256`` and ``seed_sha256`` is only
self-reported evidence. This tool takes a recorded metrics file, re-opens the
checkpoint (re-hashing it), rebuilds the same seed partition from the same
config, verifies the partition digest still matches what the metrics claim, and
then re-evaluates through the *same* ``training.evaluation.evaluate_policy``
path the trainer used - not a reimplementation, which would verify nothing.

Exit code 0 only when the recomputed win_rate, illegal_actions and
unclassified_dead_ends all agree with the record.

    python scripts/reevaluate_checkpoint.py \
        --metrics runtime/fanout/b_terminal-0/.../metrics/promotion.json \
        --config runtime/act1_overnight/fan_term/b_terminal-0.toml
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "third_party" / "slay-the-spire-2-emulator-main" / "src"))
sys.path.insert(0, str(ROOT))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True, help="the arm's TOML")
    parser.add_argument("--python-interpreter-note", default=None)
    args = parser.parse_args()

    import sts2_gym  # noqa: F401  (proves the emulator import path works)

    from sb3_contrib import MaskablePPO
    from stable_baselines3.common.vec_env import DummyVecEnv

    from training.evaluation import evaluate_policy
    from training.v2_config import load_v2_training_config
    from training.v2_curriculum import _environment_factory

    record = json.loads(args.metrics.read_text(encoding="utf-8"))
    checkpoint = Path(record["checkpoint"])
    claimed = str(record["checkpoint_sha256"]).upper()
    actual = _sha256(checkpoint)
    print(f"checkpoint      {checkpoint.name}")
    print(f"checkpoint hash {'OK' if actual == claimed else 'MISMATCH'} ({actual[:16]}…)")
    if actual != claimed:
        return 2

    config = load_v2_training_config(config_path=args.config.resolve())
    stage = next(s for s in config.stages if s.name == record["stage"])
    split = record["split"]
    episodes = int(record["episodes"])
    seeds = list(config.partition(stage.name, split).seeds)[:episodes]

    digest = hashlib.sha256(",".join(str(seed) for seed in seeds).encode()).hexdigest()
    recorded_seed = str(record.get("seed_sha256"))
    print(f"seed partition  {len(seeds)} seeds {seeds[0]}…{seeds[-1]}")
    print(f"seed hash       {'OK' if digest == recorded_seed else 'MISMATCH (recomputed ' + digest[:16] + ')'}")
    if digest != recorded_seed:
        return 2

    env_factory = _environment_factory(config, stage, sts2_gym)
    vector_env = DummyVecEnv([lambda: env_factory(seeds[0])])
    model = MaskablePPO.load(str(checkpoint), env=vector_env, device="cpu")
    metrics = evaluate_policy(
        model,
        env_factory=env_factory,
        seeds=seeds,
        stage=stage.name,
        split=split,
        scope=stage.scope,
        checkpoint=checkpoint,
        max_steps_per_episode=stage.max_episode_steps,
    )
    fresh = metrics.to_dict()

    fields = ("win_rate", "illegal_actions", "unclassified_dead_ends", "max_final_floor")
    agree = True
    for field in fields:
        expected, got = record.get(field), fresh.get(field)
        flag = "OK" if expected == got else "DIFFERS"
        agree = agree and expected == got
        print(f"{field:24} recorded={expected!s:>10}  reevaluated={got!s:>10}  {flag}")
    print("VERDICT:", "reproduced" if agree else "NOT reproduced")
    return 0 if agree else 3


if __name__ == "__main__":
    raise SystemExit(main())
