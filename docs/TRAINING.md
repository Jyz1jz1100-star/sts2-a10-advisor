# Staged training

## Boundary

This is a reproducible MaskablePPO baseline on the separate MIT-licensed
Zamiell emulator checkout. It does not copy code from reference projects with
unclear licenses. It is designed to establish measurements and curriculum
plumbing before behaviour cloning, search distillation, or hierarchical RL are
added.

No simulator result is a real-game A10 result. The current local emulator calls
its run wrapper “simplified full-run” and has not demonstrated exact multi-Act,
A10 parity. In particular, its Python `max_floors` field is not evidence that
the native engine implements all requested floors. The configured `full_run`
stage is consequently experimental, opt-in, and tagged
`simulator_full_run_experimental` in every metrics record.

## Inspect the plan

The training stack is optional to the advisor runtime. Its direct dependencies
are pinned in `requirements-training.txt` to the versions declared by the
adjacent upstream emulator's `pyproject.toml` and locked in its `uv.lock`:
Gymnasium 1.2.3, NumPy 2.4.6, PyTorch 2.12.0, Stable-Baselines3 2.8.0, and
sb3-contrib 2.8.0. The recommended reproducible environment is the adjacent
emulator checkout's `.venv`:

```powershell
& ..\third_party\slay-the-spire-2-emulator-main\.venv\Scripts\python.exe `
  -m training.curriculum --config config\training.toml --dry-run
```

To use another Python 3.11+ environment, install `requirements-training.txt`
there and set `STS2_TRAINING_PYTHON` to its interpreter before invoking
`scripts\test.ps1`. The `sts2_gym` package is local source from the adjacent
checkout (and its native DLL is a separate artifact), so it is not installed
from PyPI.

The checked-in production-sized step counts are plans, not smoke-test defaults.
Do not start them until simulator trace parity and the locked game build have
been recorded.

The short test configuration exercises two workers, two checkpoint evaluations,
metrics writing, and promotion without starting a long run:

```powershell
& ..\third_party\slay-the-spire-2-emulator-main\.venv\Scripts\python.exe `
  -m training.curriculum --config config\training.smoke.toml
```

## Run under the supervisor

All formal runs must retain the outer process manifest and heartbeat:

```powershell
& ..\third_party\slay-the-spire-2-emulator-main\.venv\Scripts\python.exe `
  -m training.supervisor `
  --run-dir runs\supervised `
  --game-build LOCKED_BUILD_ID `
  --character IRONCLAD `
  --ascension 10 `
  -- `
  ..\third_party\slay-the-spire-2-emulator-main\.venv\Scripts\python.exe `
  -m training.curriculum --config config\training.toml
```

The runner stops at the first failed promotion gate. The experimental stage is
not entered unless `--allow-experimental-full-run` is supplied. To run only a
stage that loads an earlier policy, pass both `--only-stage full_run` and
`--initial-checkpoint <act1-checkpoint.zip>`.

## Test environments

The base `requirements.txt` contains only the advisor runtime dependencies.
`scripts\test.ps1` runs the pure-contract modules in the lightweight project
Python, then runs the NumPy/PyTorch/Gymnasium modules through
`STS2_TRAINING_PYTHON` (defaulting to the adjacent emulator `.venv`). It checks
the five required training imports first, so a configured but incomplete
environment fails with an actionable dependency error instead of a cascade of
module import failures. If the default emulator environment is absent, the
optional training half is reported as skipped.

For full unittest discovery in one environment:

```powershell
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests
```

Tests that require the emulator checkout or native DLL skip when those
artifacts are unavailable.

## Seed contract

The checked-in ranges are:

| Split | Start | Count | Used for |
| --- | ---: | ---: | --- |
| train | 0 | 10,000,000 | Optimization rollouts only |
| checkpoint | 20,000,000 | 10,000 | Frequent diagnostics |
| promotion | 30,000,000 | 10,000 | Curriculum advancement |
| final | 40,000,000 | 10,000 | Reserved holdout only |

Worker shards are non-overlapping. Within a shard, seeds are deterministically
permuted without replacement; exhaustion is a hard error. Evaluation always
records a SHA-256 digest of the ordered seed list. Do not tune a policy after
examining the final split.

### A range is not an act

`train_seeds_file` on a stage replaces that stage's *training* seeds with an
explicit list, because a contiguous range cannot express "Act 1": the emulator
chooses the act per seed, so censusing 4,000 consecutive seeds of an act1
stage's own train partition gives 1,990 overgrowth / 2,010 underdocks, and
Act 2 clears ~22x more often for the same policy. Produce the list by measuring,
never by reimplementing that choice:

```
scripts/split_win_rate_by_generated_act.py --config <arm.toml> --stage act1 \
  --split train --start-offset <k*10000> --episodes 10000 --act 1 --emit-seed-list <chunk.json>
```

(`--start-offset` exists because a census costs one map generation per seed,
measured at ~18.7 seeds/s per process.) Constraints the loader enforces:

- every seed must lie inside the stage's declared `seeds.train` partition, so a
  filtered list cannot escape the fan-out's disjointness or the teacher-reserved
  range — those are properties of the partitions;
- the file must state `generated_act`, and seeds must be unique and non-empty;
- only the train split may be filtered. Checkpoint, promotion and final stay
  ranges, so a filtered arm is still evaluated on the same population as an
  unfiltered one;
- `plan.json` records the list's digest, count and per-worker budget, and
  exhaustion of a filtered list is still a hard error rather than a re-use.

## Outputs

Each curriculum run creates:

```text
runs/curriculum/curriculum-<UTC>/
  plan.json
  combat/
    checkpoints/step_<N>.zip
    checkpoints/final.zip
    metrics/step_<N>.json
    metrics/promotion.json
    promotion_decision.json
  act1/...
  full_run/...
```

Warm-start lineage is recorded, not remembered. `plan.json` carries a `warm_start`
block (`initial_checkpoint`, `initial_checkpoint_sha256`, `exists`) for the
`--initial-checkpoint` a run was launched with, and each stage directory carries an
`origin.json` naming the checkpoint *that rung* was initialized from, its SHA-256,
which stage produced it, and whether it came from the command line rather than the
previous rung. Without these, "the ladder continued from the promoted checkpoint of
the stage before it" is a sentence about how the run was launched and cannot be
checked afterwards — which is exactly what happened to the 2026-09-19 fan-out
campaign, whose 20 arms have no recorded parent (see
`docs/ACT1_CAMPAIGN_2026-09-19.md`, scope item 3).

Metrics are schema-versioned and contain checkpoint and seed hashes, scope,
experimental flag, win rate, Wilson interval, truncation and illegal-action
counts, mean return/length/floor, and encounter breakdowns. A checkpoint can be
re-evaluated independently:

```powershell
& ..\third_party\slay-the-spire-2-emulator-main\.venv\Scripts\python.exe `
  -m training.evaluate_checkpoint `
  --stage act1 `
  --split checkpoint `
  --episodes 100 `
  --checkpoint runs\curriculum\<run>\act1\checkpoints\final.zip `
  --output runs\curriculum\<run>\act1\metrics\reevaluation.json
```

## Promotion gates

Every requirement must pass: evaluation count, point win rate, Wilson 95% lower
bound, maximum truncation rate, zero illegal actions, and—on run stages—minimum
mean floor. These gates are baseline engineering thresholds, not claims that
passing them predicts 50% real-game A10 performance.
