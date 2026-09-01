"""Summarize the floor6 reward ablation and rank arms (review item 4).

Reads ``runs/ablations/<label>/manifest-{A,B,C}.json`` plus each run's full
``joint_metrics.json`` and reports, per arm (pooled over its PPO seeds):

* boundary rate + Wilson 95% interval on pooled hits (the floor6 gate is
  point >= 0.93 AND Wilson low >= 0.90),
* mean final floor, mean final HP fraction, mean steps, true terminal wins,
* mean episode return — reported but explicitly NOT a selection key.

Selection is lexicographic: boundary_rate -> Wilson low -> mean final floor ->
mean final HP fraction -> fewer mean steps -> arm name.  ``mean_return`` is
never compared.  Safe to run while the ablation is still in progress: missing
runs are listed as pending and the ranking covers completed runs only.

Usage::

    python scripts/summarize_floor6_ablation.py --label floor6-20260901-review
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

#: Lexicographic selection keys: (metric, higher_is_better).
SELECT_ORDER: tuple[tuple[str, bool], ...] = (
    ("boundary_rate", True),
    ("boundary_wilson_95_low", True),
    ("mean_final_floor", True),
    ("mean_final_hp_fraction", True),
    ("mean_steps", False),
)

REPORTED_BUT_NOT_SELECTED = ("mean_return", "win_rate")


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default=None,
                        help="ablation run-group under runs/ablations")
    parser.add_argument("--ablation-root", type=Path,
                        default=PROJECT_ROOT / "runs" / "ablations")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    from training.metrics import atomic_write_json  # noqa: PLC0415,E402
    from training.wilson import wilson_interval  # noqa: PLC0415,E402

    root = args.ablation_root
    if args.label:
        root = root / args.label
    if not root.is_dir():
        raise SystemExit(f"ablation directory missing: {root}")

    manifests: dict[str, dict] = {}
    for manifest_path in sorted(root.glob("manifest-*.json")):
        arm = manifest_path.stem.removeprefix("manifest-")
        manifests[arm] = _load_json(manifest_path)
    if not manifests:
        raise SystemExit(f"no manifest-*.json under {root}")

    arms: dict[str, dict] = {}
    pending: list[str] = []
    for arm, manifest in sorted(manifests.items()):
        expected = [str(seed) for seed in manifest.get("ppo_seeds", [])]
        run_payloads: list[dict] = []
        for seed in expected:
            run_dir = root / f"{arm}_seed{seed}"
            metrics_path = run_dir / "joint_metrics.json"
            if metrics_path.is_file():
                run_payloads.append(_load_json(metrics_path))
            else:
                pending.append(f"{arm}/seed{seed}")
        if not run_payloads:
            arms[arm] = {"status": "no_completed_runs", "runs": 0}
            continue
        episodes = sum(int(payload.get("episodes", 0)) for payload in run_payloads)
        hits = sum(int(payload.get("boundary_hits", 0) or 0) for payload in run_payloads)
        boundary_rate = hits / episodes if episodes else None
        wilson = wilson_interval(hits, episodes) if episodes and hits is not None else (None, None)

        def _mean(metric: str) -> float | None:
            values = [payload.get(metric) for payload in run_payloads]
            values = [float(value) for value in values if value is not None]
            return sum(values) / len(values) if values else None

        arms[arm] = {
            "status": "ok" if len(run_payloads) == len(expected) else "partial",
            "runs_completed": len(run_payloads),
            "runs_expected": len(expected),
            "episodes": episodes,
            "boundary_hits": hits,
            "boundary_rate": boundary_rate,
            "boundary_wilson_95_low": wilson[0],
            "boundary_wilson_95_high": wilson[1],
            "mean_final_floor": _mean("mean_final_floor"),
            "mean_final_hp_fraction": _mean("mean_final_hp_fraction"),
            "mean_steps": _mean("mean_steps"),
            "mean_return": _mean("mean_return"),
            "win_rate": _mean("win_rate"),
            "per_run": {
                str(index): {
                    "boundary_rate": payload.get("boundary_rate"),
                    "boundary_wilson_95_low": payload.get("boundary_wilson_95_low"),
                    "mean_final_floor": payload.get("mean_final_floor"),
                    "mean_final_hp_fraction": payload.get("mean_final_hp_fraction"),
                    "mean_steps": payload.get("mean_steps"),
                    "mean_return": payload.get("mean_return"),
                }
                for index, payload in enumerate(run_payloads)
            },
        }

    ranked = sorted(
        (arm for arm in arms if arms[arm].get("boundary_rate") is not None),
        key=lambda arm: _best_first_key(arms[arm]),
    )

    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "ablation_root": str(root),
        "selection_rule": {
            "order": [f"{metric} ({'max' if higher else 'min'})"
                      for metric, higher in SELECT_ORDER],
            "never": REPORTED_BUT_NOT_SELECTED,
            "note": "mean_return is reported for transparency only",
        },
        "floor6_gate": {"point_min": 0.93, "wilson_low_min": 0.90},
        "pending_runs": pending,
        "arms": arms,
        "ranking_best_first": ranked,
    }

    out_path = args.out or (root / "summary.json")
    atomic_write_json(out_path, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _best_first_key(payload: dict) -> tuple:
    """Sort key where the best arm sorts first (ascending tuple order)."""

    key: list = []
    for metric, higher_better in SELECT_ORDER:
        value = payload.get(metric)
        if value is None:
            key.append(0 if higher_better else 1)
            key.append(float("inf") if higher_better else float("-inf"))
        else:
            value = float(value)
            key.append(1)
            key.append(-value if higher_better else value)
    return tuple(key)


if __name__ == "__main__":
    raise SystemExit(main())
