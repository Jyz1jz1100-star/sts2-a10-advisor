"""Summarize the live status of a supervised curriculum stage.

Reads only run artifacts (manifest/heartbeat/stdout.log + curriculum metrics)
and prints a one-screen report: supervisor status, latest optimizer progress,
ETA to the timestep budget, every checkpoint/promotion evaluation emitted so
far, and the promotion decision when present.

    python scripts/report_training_progress.py [--stage act1]
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TIMESTEP_RE = re.compile(r"total_timesteps\s*\|\s*(\d+)")
FPS_RE = re.compile(r"fps\s*\|\s*(\d+)")


def load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def latest_supervised_run(stage: str) -> Path | None:
    base = ROOT / "runs" / f"supervised-{stage}"
    if not base.is_dir():
        return None
    candidates = [d for d in base.iterdir() if (d / "manifest.json").is_file()]
    return max(candidates, key=lambda d: d.stat().st_mtime, default=None)


def parse_log(path: Path) -> tuple[int | None, int | None]:
    timesteps = fps = None
    if path.is_file():
        text = path.read_text(encoding="utf-8", errors="replace")
        steps = TIMESTEP_RE.findall(text)
        rates = FPS_RE.findall(text)
        if steps:
            timesteps = int(steps[-1])
        if rates:
            fps = int(rates[-1])
    return timesteps, fps


def matching_curriculum_run(stage: str) -> Path | None:
    base = ROOT / "runs" / "curriculum"
    if not base.is_dir():
        return None
    for run in sorted((d for d in base.iterdir() if d.is_dir()), reverse=True):
        if (run / stage).is_dir():
            return run
    return None


def fmt_age(iso: str | None) -> str:
    if not iso:
        return "n/a"
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    seconds = (datetime.now(UTC) - moment).total_seconds()
    if seconds < 120:
        return f"{int(seconds)}s ago"
    if seconds < 7200:
        return f"{seconds / 60:.1f}min ago"
    return f"{seconds / 3600:.1f}h ago"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", default="act1", choices=("combat", "act1", "full_run"))
    args = parser.parse_args()

    run_dir = latest_supervised_run(args.stage)
    if run_dir is None:
        print(f"no supervised-{args.stage} runs found")
        return 1
    manifest = load_json(run_dir / "manifest.json") or {}
    heartbeat = load_json(run_dir / "heartbeat.json") or {}
    timesteps, fps = parse_log(run_dir / "stdout.log")

    print(f"== supervised {args.stage} run {run_dir.name} ==")
    print(f"  manifest.status={manifest.get('status')} exit={manifest.get('exit_code')}")
    print(f"  heartbeat: {heartbeat.get('status')} pid={heartbeat.get('pid')} ({fmt_age(heartbeat.get('time'))})")
    if timesteps is not None:
        line = f"  progress: {timesteps:,} steps"
        if fps:
            line += f" @ ~{fps} fps"
        print(line)

    curriculum = matching_curriculum_run(args.stage)
    budget = None
    if curriculum is not None:
        plan = load_json(curriculum / "plan.json") or {}
        for stage in plan.get("stages", []):
            if stage.get("name") == args.stage:
                budget = stage.get("timesteps")
        print(f"  curriculum dir: {curriculum.name}"
              + (f"  budget={budget:,}" if budget else ""))
        if budget and timesteps and fps:
            hours = (budget - timesteps) / (fps * 3600)
            print(f"  ETA at current fps: ~{hours:.1f}h")
        stage_dir = curriculum / args.stage
        metrics = sorted((stage_dir / "metrics").glob("*.json")) if (stage_dir / "metrics").is_dir() else []
        if not metrics:
            print("  checkpoint metrics: none yet (first one lands at the first checkpoint interval)")
        for metric_path in metrics:
            data = load_json(metric_path) or {}
            floor = data.get("mean_final_floor")
            floor_text = f"{floor:.1f}" if isinstance(floor, (int, float)) else "-"
            print(
                f"  [{data.get('split','?'):10}] {metric_path.name:34} "
                f"win={data.get('win_rate', 0):.3f} "
                f"wilson_low={data.get('wilson_95_low', 0):.3f} "
                f"trunc={data.get('truncation_rate', 0):.3f} "
                f"illegal={data.get('illegal_actions', 0)} "
                f"floor={floor_text} eps={data.get('episodes', 0)}"
            )
        for decision_name in ("early-promotion-decision.json", "promotion_decision.json"):
            decision = load_json(stage_dir / decision_name)
            if decision:
                print(f"  {decision_name}: promoted={decision.get('promoted')} reasons={decision.get('reasons')}")
    print(f"  game_build: {manifest.get('game_build')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
