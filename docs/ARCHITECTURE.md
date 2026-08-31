# Architecture

## Product boundary

The product is a read-only advisor. It may observe the same information shown
to the player and may write text into the overlay. It must not click, send game
actions, read future RNG, inspect the exact hidden draw order, or reload a run.

“Optimal” therefore means the legal action with the highest estimated run-win
value under the current visible information, locked game build, model version,
and search budget. It is an estimate, not a mathematical guarantee.

## Runtime

```text
Slay the Spire 2
  -> STS2MCP GET-only state
  -> visible-state filter + legal-action enumerator
  -> tactical or strategic policy/value model
  -> information-set search
  -> structured recommendation
  -> Chinese template renderer
  -> always-on-top overlay
  -> player performs the action
```

The bridge process must expose only an HTTP GET code path in release mode. A
separate evaluation profile may use action endpoints to run unattended
benchmarks, but its results must be tagged `automated=true` and must not be
mixed with human-executed overlay trials.

## Decision hierarchy

### Tactical model

Used for cards, targets, potions, hand selections, and end-turn decisions.

Input tokens:

- each card instance in hand, with cost, upgrade/enchantment, legal targets;
- public draw/discard/exhaust counts and public pile composition only;
- player HP, block, energy, powers, relic counters and turn history;
- each enemy's HP, block, public intent, powers and action history;
- the ordered actions already taken in the turn.

Output heads:

- legal-action prior;
- battle-win probability;
- expected HP at battle end and immediate HP loss;
- calibrated uncertainty.

The first production baseline is MaskablePPO for reproducibility. The target
policy is a small object-centric Transformer distilled from search. Search uses
PUCT/beam expansion across legal `(card, target)`, potion and end-turn actions.
Unknown draws and random effects are sampled as belief particles; the real
future RNG stream is never read.

### Strategic model

Used for card rewards, paths, events, shops, rest sites, relics and removals.

The state encoder combines a deck/relic/potion set encoder with a map DAG
encoder. Candidate actions are variable-length tokens. Heads estimate run-win
probability, near-term death risk and expected HP at the next checkpoint.

Strategic rollouts stop at the next rest site, boss, or act boundary. They use
the tactical value model for combats and a conservative risk statistic (CVaR)
to avoid fragile routes that have high mean value but excessive death tails.

## Training curriculum

1. Simulator parity: compare fixed-seed real-game traces against emulator
   traces. No learning result is trusted before the reachable Ironclad/A10
   surface is covered.
2. Behaviour cloning: train on high-quality Ironclad/A10 RunReplays and
   SpireLens traces. Split by player and run, never by individual decision.
3. Search teacher: label disagreement/death-adjacent states with information-
   set search and distil visit distributions/value estimates.
4. Offline RL: IQL or advantage-weighted behaviour cloning for strategic
   decisions where action coverage is incomplete.
5. Curriculum RL: combat -> random decks -> one act -> A0 -> A3 -> A6 -> A9 ->
   A10. Keep a flat MaskablePPO run only as a comparison baseline.
6. Real-game calibration: use DAgger-style player overrides and fresh true-game
   replays. Quarantine any strategy that exploits an emulator mismatch.

## Explanation contract

The explanation renderer receives only scored candidates and computed metrics.
It may say which action won, the score gap, expected HP loss, calibrated win
probability, search count and concrete rule facts. It may not invent a reason.
An optional language model may paraphrase these fields, but cannot select or
re-rank actions.

## Version and reproducibility contract

Every run manifest records the exact game build, mod versions, emulator
revision, data hashes, policy checkpoint hash, random seeds, character,
ascension, SL status, command and exit code. Training, development evaluation
and final evaluation seeds live in disjoint stores.

## Implemented MaskablePPO curriculum baseline

The reproducible baseline is implemented in `training/curriculum.py` and
configured by `config/training.toml`. It is intentionally narrower than the
target search-distilled architecture:

1. `combat` trains the native combat Gym environment.
2. `act1` starts a new run policy because the current combat and run spaces are
   incompatible. It evaluates the simplified native run through floor 16.
3. `full_run` may initialize from the Act 1 checkpoint because both use the run
   space. It is marked experimental and requires an explicit command-line
   acknowledgement.

Training, checkpoint, promotion, and final seed ranges do not overlap. Parallel
training workers receive disjoint shards within the training range, then use a
deterministic no-replacement permutation inside each shard. Checkpoint metrics
never select a stage for promotion; promotion uses a second unseen range. The
final range is not touched by the curriculum runner.

Every checkpoint evaluation emits a versioned JSON record containing scope,
experimental status, checkpoint SHA-256, seed-list SHA-256, win rate, Wilson 95%
interval, truncations, illegal actions, length, return, floor reach, and
per-encounter aggregates. A promotion decision is stored separately with every
passed and failed criterion.
