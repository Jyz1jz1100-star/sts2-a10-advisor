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

Use the emulator environment because it already contains PyTorch,
Stable-Baselines3, and sb3-contrib:

```powershell
& ..\third_party\slay-the-spire-2-emulator-main\.venv\Scripts\python.exe `
  -m training.curriculum --config config\training.toml --dry-run
```

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
