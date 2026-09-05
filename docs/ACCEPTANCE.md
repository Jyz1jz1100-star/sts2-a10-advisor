# Acceptance protocol

## Current verdict (2026-09-05)

The acceptance machinery is present and its offline fixture ledger is covered by
contract tests, but the real-game target is **not accepted**. The current
system is layered as follows:

- Combat Solver full-auto owns in-combat actions.
- `bridge/autoplay.py` owns out-of-combat actions through the live heuristic
  policy and the visible candidate codec.
- The repository has no deployable out-of-combat trained model and no verified
  end-to-end real-game run from start to terminal victory.

The 2026-09-03 simulator Act 1 bulk run completed 20,000,000 steps and ended at
`1/500 = 0.2%` with mean final floor `7.67`; its checkpoint was not promoted.
This is simulator-only evidence (`scope: simulator_act1`), so it cannot satisfy
the real-game gates below. The artifact is
[`experiment-final.json`](../runs/bulk_training/act1-pretrained-r3-bulk-20260903T0640Z/act1/metrics/experiment-final.json).

The fixture tests validate legal candidate extraction, wire mappings, trace
provenance and fail-closed full-run accounting. They do not exercise the live
game, send POST actions, or establish a real-game win rate. A passing offline
ledger report is therefore a contract result, not an acceptance result.

On 2026-09-05, the small-runtime acceptance collection passed `337/337` tests.
The repository entrypoint `scripts/test.ps1` passed the same `337` tests and
the training-environment unit collection `99/99` (`436/436` total). No game
process, live bridge action, deployment, or production training job was started
by these checks. The full-run ledger's intentionally synthetic negative report
remained `accepted: false` with missing live provenance/identity blockers.

## Exact target

- Game: the version-locked installed public-beta build described by
  [`config/live_version.lock.json`](../config/live_version.lock.json).  The
  acceptance runner must load that lock and verify the installed release,
  Steam manifest, bridge hashes, and allowed-mod inventory before counting any
  run.  Do not copy a Steam build ID, commit, or assembly hash into this
  protocol: the lock is the single source of truth and prevents an old build
  from being accepted accidentally.
- Mode: standard single-player.
- Character: Ironclad.
- Difficulty: Ascension 10.
- Seeds: fresh random seeds disjoint from all training and model-selection
  seeds.
- The user permits save/load, room retry, automated control and replay. These
  runs are reported as `assisted`; they never replace the separate no-SL rate.
- No-SL evaluation still forbids save/load, room retry, future RNG and hidden
  draw order, so the result remains comparable to normal play.
- The policy outputs text; the player performs exactly the advised action. Any
  execution deviation is logged and that run is reported separately.

Two named metrics are mandatory: `assisted A10` (all user-authorized operations
allowed) and `no-SL A10` (normal-play restrictions). The requested 50% target
is evaluated on assisted A10 unless explicitly changed; no-SL is always shown
beside it.

## Gates

1. Illegal recommendation rate: exactly 0 over schema/property tests and the
   final sample.
2. Bridge coverage: every reachable Ironclad/A10 decision screen has a stable
   decision ID and a legal action set.
3. Simulator parity: >=99.9% step-state agreement on the audited core combat
   surface; all remaining mismatches are enumerated and excluded from training
   or fixed.
4. Development evaluation: 200 hidden fixed seeds, used only for checkpoint
   selection.
5. Final evaluation: at least 500 fresh seeds; 1,000 is preferred.
6. Success threshold: point estimate >=50%. The report must also show the 95%
   Wilson interval; the interval is not silently discarded if it crosses 50%.

## Required report

- full-run win rate and Wilson interval;
- Act 1/2/3 survival rates;
- decision coverage and illegal-action count;
- P50/P95 latency for ordinary card plays, first action of combat, and
  strategic decisions;
- policy-only and policy+search results;
- game/mod/emulator/checkpoint/data hashes;
- simulator-to-real mismatch rate;
- crash, timeout and player-execution-deviation counts.

Simulation wins are never presented as real-game wins.

## Offline full-run ledger

Before a game-connected pilot exists, the local fixture-driven ledger can be
planned and audited without sending actions:

```powershell
python scripts/assess_full_run.py --plan --cohort assisted `
  --seed-mode observational --out runs/full_run_acceptance/plan.json
python scripts/assess_full_run.py --trace <completed-trace.jsonl> `
  --cohort assisted --seed-mode observational `
  --out runs/full_run_acceptance/report.json `
  --model-id <trained-model-id> --checkpoint <checkpoint> `
  --data-manifest <data-manifest>
```

The report is keyed by concrete `run_id` and records raw seed, locked game
identity, terminal full-run outcome, Act survival, action/decision coverage,
illegal actions, missing results, latency, deviations, and provenance hashes.
Duplicate traces for one `run_id` are merged. `assisted` and `no-sl`, and
`observational` and `fixed`, are separate cohorts; a fixed-seed report also
requires a registered seed partition. Only an explicit terminal victory is a
full-run win; battle or simulator wins cannot enter that count. Both the
20-run pilot and 500-run formal gates use the Wilson interval and fail closed
on missing identity, provenance, terminal evidence, or execution ownership.

The ledger does not start either batch. A connected run must write session
provenance (lock-verified game/mod inventory, model/checkpoint/data hashes),
machine-verify each fresh Ironclad/A10/standard run, and declare exactly one
execution owner. Valid owner modes are `http_route_executor` (the bridge owns
all player actions) or `combat_solver_full_auto` (the Mod owns combat and the
bridge owns only out-of-combat actions). `fullauto_keeper` is a recovery
watchdog, not a second owner; simultaneous HTTP combat execution and Solver
full-auto are a hard blocker.
