# Acceptance protocol

## Verdict update (2026-09-19)

Still **not accepted**; nothing below changes that verdict. What changed overnight,
in terms of what the gates can now actually check:

- The 0.41.0 journal reader (grammar v2) is merged and registered in
  `scripts/test.ps1`, so a live batch's in-combat evidence is readable and
  judgeable; before this, a real batch could play but not be assessed.  Suite:
  486 contract + 106 training-environment tests.
- The precondition "stop Workshop auto-update" was dropped as un-actionable and
  replaced by **per-batch mod attestation** (`combat_solver/modpin.py`, wired into
  `scripts/supervise_solver_batch.py`): every batch hashes the locked mod binaries
  and manifest versions at both ends of its window.  With
  `config/combat_solver.lock.json` still pinning 0.31.0 while 0.41.0 / RitsuLib
  0.6.2 are on disk, a batch correctly reports `attested: false` — reconciling that
  lock is the operator's call, not the agent's.  The digests and DLL timestamps that
  decision needs are recorded read-only in
  `docs/evidence/modpin_drift_20260919.json`.
- The bridge can no longer report a survived run as a victory (see
  `bridge/outcome.py` and "explicit terminal victory" below): a live clear must be
  corroborated by the run's own trace, not inferred from positive HP.
- Simulator evidence has a measured ceiling and a measured scope.  Split by the act
  the emulator actually generated, the one arm measured per-act so far runs Act 1 at
  **3/5014** (three named, individually re-run seeds); `simulator_act1` labels in
  earlier reports mixed two acts.  The bundled emulator defines exactly two acts
  (`RunConstants.cs:35-36`) and picks one per seed (`RunMapGenerator.cs:9-11`).  It
  has no Act 3, and chaining is not available to a seed in general: the single
  Act 1 -> Act 2 branch is gated on hardcoded string seed `7MS1YN8NWB`
  (`RunEngine.cs:1907-1920`), whose Act 1 map, encounters and boss are themselves
  scripted — so a clear there evidences the harness finishing a two-act flow, not
  policy strength, and that flow's Act 2 side is truncated by the environment even
  at full HP.  Driving 76 campaign checkpoints through it entered Act 2 three times
  with 0 illegal actions and produced 0 two-act victories.  **No simulator result can
  satisfy these gates at any checkpoint quality** — the three-act target is a
  real-game question only.

What still blocks a live attempt is operational, not code: the game is not running,
and the supervisor deliberately never starts it.  `docs/STATUS.md` names the command
to run once the operator has launched the game.

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

## Precondition: pin the Workshop mods (2026-09-19)

`config/combat_solver.lock.json` is only meaningful if the mods it pins stay in
place. They have not: between 2026-09-11 and 2026-09-18 Steam auto-updated
CombatSolver eight times (0.35.5 → 0.41.0, visible in the
`INIT mod=` lines of `%APPDATA%\SlayTheSpire2\logs\CombatSolver\*\process.jsonl`)
and moved STS2-RitsuLib to 0.6.2, and the 0.41.0 log grammar no longer matches
the v1 parser at all (0 snapshots over 13 real combat logs). Evidence and
consequences: [`COMBAT_SOLVER_0_41_DRIFT_2026-09-19.md`](COMBAT_SOLVER_0_41_DRIFT_2026-09-19.md).

No live batch may start until (b) the solver lock is re-reconciled against the
DLLs actually on disk with measured hashes, and (c) the reconciled grammar is
replay-verified over a fresh session log. Until then the comparison child is
expected to fail closed, and any run performed against a moving mod build is
excluded from acceptance regardless of what it returns.

(a) is **not achievable non-invasively and has been dropped as a precondition**:
`steamapps/workshop/appworkshop_2868840.acf` carries no per-item auto-update
flag (keys present: `appid`, `manifest`, `size`, `latest_manifest`,
`latest_timeupdated`, `timeupdated`, `timetouched`, `subscribedby`), and the
alternatives are editing live Steam state on a machine shared with a human user
or moving Workshop files aside — either of which risks the in-game solver
disappearing. The operator's standing instruction is that the solver and
full-auto ownership are core and must not be switched off, so update drift is
treated as an environment property rather than something to prevent.

The replacement is **per-batch attestation**, which is stronger than a promise
that nothing changed: every batch records the mod versions and DLL SHA-256 it
actually observed at start and end, and the batch's evidence is only admissible
against the grammar that was replay-verified for *that* build. A mid-batch
Workshop update then invalidates that batch instead of silently poisoning it,
and no external guarantee about Steam's behaviour is required to trust a run.

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

What "explicit terminal victory" can mean on the live side is narrower than the
sentence suggests, and a candidate run must be read with that in mind. STS2MCP
exposes no win/loss flag; the bridge sees `state_type == "game_over"` plus a free
text `game_over.message`, and `run.act` / `run.floor`. `bridge/outcome.py`
therefore treats the producer's own word as the only basis for a victory claim
and reports `undetermined` otherwise -- surviving the screen is not a win,
because abandoning a mid-Act run also ends with positive HP. Corroboration for an
Act 1-3 clear has to come from the run's own trace: the terminal Act number, and
decision/result records for both final-act bosses. A live clear that cannot show
those is reported, not accepted.

The ledger does not start either batch. A connected run must write session
provenance (lock-verified game/mod inventory, model/checkpoint/data hashes),
machine-verify each fresh Ironclad/A10/standard run, and declare exactly one
execution owner. Valid owner modes are `http_route_executor` (the bridge owns
all player actions) or `combat_solver_full_auto` (the Mod owns combat and the
bridge owns only out-of-combat actions). `fullauto_keeper` is a recovery
watchdog, not a second owner; simultaneous HTTP combat execution and Solver
full-auto are a hard blocker.
