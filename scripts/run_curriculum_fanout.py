"""Fan out independent curriculum runs on one machine.

Motivation (docs/TRAINING_THROUGHPUT_2026-09-19.md): inside a single run the
environment count buys ~6-11%, but independent runs scale — 12 concurrent
measured 3445 steps/s against ~370 for one, with the GPU at 11%. This tool is
that fan-out, with the seed discipline the measurement harness did not need.

Each job gets its own ``train`` partition and its own ``run_dir``; the
checkpoint/promotion/final partitions are deliberately left shared so the arms
stay comparable on identical evaluation seeds. Every allocated train seed is
checked against the frozen teacher lineages and the reserved holdout before
anything launches, and the train ranges must be pairwise disjoint.

    python scripts/run_curriculum_fanout.py --config config/training_v2.toml \
        --jobs 4 --seed-base 1700000000 --stride 100000 --stage floor10 \
        [--steps 8000000] [--dry-run] [--python <interpreter>]
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from training.teacher_v3 import assert_seeds_outside_frozen_lineages  # noqa: E402

TRAIN_HEADER = re.compile(r"^\[(?P<section>stages\.(?P<stage>[A-Za-z0-9_]+)\.seeds\.train)\]\s*$")
START_LINE = re.compile(r"^start\s*=\s*(?P<value>\d+)\s*$")
RUN_DIR_LINE = re.compile(r'^run_dir\s*=\s*"(?P<value>[^"]*)"')
TIMESTEPS_LINE = re.compile(r"^timesteps\s*=\s*(?P<value>\d+)\s*$")


def _rewrite(text: str, stage: str, train_base: int, run_dir: str, steps: int | None) -> str:
    """Repoint one job's train seed bases, run dir and step budget.

    Each stage keeps its original offset from the template's lowest train base,
    so a template whose stages are pairwise disjoint stays pairwise disjoint
    after fan-out (``v2_config`` validates that invariant at load). Only
    ``train`` blocks are touched; evaluation partitions are left as the
    template has them, which keeps the arms comparable on identical eval seeds.
    """
    originals = [int(match.group(1)) for match in
                 re.finditer(r"^start\s*=\s*(\d+)\s*$", text, re.MULTILINE)
                 if _is_train_line(text, match.start())]
    if not originals:
        raise ValueError("template has no train partition start lines")
    floor_start = min(originals)
    out: list[str] = []
    section: str | None = None
    section_stage: str | None = None
    replaced: list[str] = []
    for line in text.splitlines():
        header = TRAIN_HEADER.match(line)
        if header:
            section = header.group("section")
            section_stage = header.group("stage")
        elif line.startswith("["):
            section = section_stage = None
        start_match = START_LINE.match(line) if section is not None else None
        if start_match is not None and (stage == "all" or section_stage == stage):
            offset = int(start_match.group("value")) - floor_start
            line = f"start = {train_base + offset}"
            replaced.append(section or "")
        elif RUN_DIR_LINE.match(line):
            line = RUN_DIR_LINE.sub(f'run_dir = "{run_dir}"', line, count=1)
        elif steps is not None and TIMESTEPS_LINE.match(line):
            line = TIMESTEPS_LINE.sub(f"timesteps = {steps}", line, count=1)
        out.append(line)
    if stage != "all" and stage not in " ".join(replaced):
        raise ValueError(f"template has no [stages.{stage}.seeds.train] block")
    if not replaced:
        raise ValueError("no train partition block was rewritten; check the template")
    return "\n".join(out) + "\n"


def _is_train_line(text: str, position: int) -> bool:
    """True when a ``start =`` line sits inside a ``.seeds.train]`` section."""
    header = text.rfind("[", 0, position)
    return header != -1 and text[header:position].split("]")[0].endswith(".seeds.train")


def _train_seeds(config_path: Path) -> dict[str, tuple[int, int]]:
    """Train (start, count) for the stages this run actually executes.

    A template keeps every stage table on disk even when ``curriculum.stages``
    selects one, so collecting all of them would report collisions between
    stages that never run.
    """
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    executed = set(data.get("curriculum", {}).get("stages", []))
    result: dict[str, tuple[int, int]] = {}
    for name, table in data.get("stages", {}).items():
        if executed and name not in executed:
            continue
        train = ((table or {}).get("seeds") or {}).get("train")
        if isinstance(train, dict) and "start" in train:
            result[name] = (int(train["start"]), int(train.get("count", 1)))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="template TOML")
    parser.add_argument("--jobs", type=int, required=True)
    parser.add_argument("--seed-base", type=int, required=True)
    parser.add_argument("--stride", type=int, required=True, help="gap between job train bases")
    parser.add_argument("--stage", default="all", help="only rewrite this stage's train block")
    parser.add_argument("--steps", type=int, help="override per-stage timesteps for every job")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "runtime" / "fanout")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--module", default="training.v2_curriculum")
    parser.add_argument("--exclude", action="append", default=[],
                        help="repeat; train ranges already in use elsewhere as "
                             "start:count, refused if they would overlap")
    parser.add_argument("--allow-reserved-test-corpus", action="store_true",
                        help="permit seeds at or above the reserved teacher holdout start")
    parser.add_argument("job_args", nargs=argparse.REMAINDER,
                        help="everything after `--` is forwarded verbatim to each job, "
                             "e.g. `-- --initial-checkpoint <zip>`. Flags cannot be "
                             "passed with --extra-arg because argparse rejects a value "
                             "that starts with a dash.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.jobs < 1 or args.stride < 1:
        raise SystemExit("--jobs and --stride must both be >= 1")
    job_args = [token for token in args.job_args if token != "--"]
    template = args.config.read_text(encoding="utf-8")
    args.out_dir.mkdir(parents=True, exist_ok=True)

    bases = []
    for index in range(args.jobs):
        # run_dir must land under --out-dir; hardcoding runtime/fanout made six
        # live arms invisible to anyone looking where they had been told the
        # campaign would write.
        run_dir = args.out_dir.resolve().relative_to(ROOT).as_posix() + f"/{args.config.stem}-{index}"
        path = args.out_dir / f"{args.config.stem}-{index}.toml"
        path.write_text(
            _rewrite(template, args.stage, args.seed_base + index * args.stride,
                     run_dir, args.steps),
            encoding="utf-8", newline="\n",
        )
        bases.append((path, _train_seeds(path)))

    ranges: list[tuple[int, int, str]] = []
    for path, seeds in bases:
        for stage_name, (start, count) in seeds.items():
            ranges.append((start, start + count, f"{path.name}/{stage_name}"))
    assert_seeds_outside_frozen_lineages(
        [start for start, _end, _label in ranges],
        allow_reserved_test_corpus=args.allow_reserved_test_corpus,
    )
    for spec in args.exclude:
        try:
            ex_start, ex_count = (int(part) for part in spec.split(":", 1))
        except ValueError:
            raise SystemExit(f"--exclude expects start:count, got {spec!r}")
        ranges.append((ex_start, ex_start + ex_count, f"excluded({spec})"))
    ranges.sort()
    for (a_start, a_end, a_label), (b_start, b_end, b_label) in zip(ranges, ranges[1:]):
        if b_start < a_end:
            raise SystemExit(
                f"train partitions overlap: {a_label} [{a_start}, {a_end}) and "
                f"{b_label} [{b_start}, {b_end}) -- raise --stride above the "
                f"largest train count"
            )

    print(f"{len(bases)} jobs, train ranges: "
          f"{', '.join(f'{start}-{end}' for start, end, _ in ranges)}")
    for path, _ in bases:
        print(f"  {path}")
    if args.dry_run:
        print("dry run: nothing launched")
        return 0

    launched = time.monotonic()
    procs = []
    for path, _ in bases:
        log = path.with_suffix(".log")
        handle = log.open("w", encoding="utf-8")
        procs.append((path.name, subprocess.Popen(
            [args.python, "-m", args.module, "--config", str(path), *job_args],
            stdout=handle, stderr=subprocess.STDOUT, cwd=str(ROOT), text=True,
            # Children otherwise inherit the console code page and emit UTF-16 or
            # cp936 bytes into these logs, which makes a failed job unreadable
            # exactly when it matters.
            env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8",
                 "PYTHONUNBUFFERED": "1"}), log, handle))
    failures = []
    for name, proc, log, handle in procs:
        code = proc.wait()
        handle.close()
        print(f"{name}: exit {code} (log {log.name})")
        if code != 0:
            failures.append(name)
    print(f"wall clock {time.monotonic() - launched:.0f}s for {len(procs)} jobs")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
