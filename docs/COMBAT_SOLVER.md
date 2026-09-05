# Combat Solver integration (in-combat candidate upper module)

Status: **Phase A live-calibrated (grammar v1, replay-verified). The Phase B
runner and regression gates are implemented, but the formal fixed-seed phase
is currently BLOCKED: this bridge exposes `run.seed=null` and has no seed
injection endpoint. Automated observational smoke is available for lifecycle
and invariant checks, but it is not fixed-seed evidence.** Freeze context:
[docs/FREEZE_2026-09-02.md](FREEZE_2026-09-02.md).

## Pre-smoke regression gates (2026-09-02, mandatory before any batch)

Five regressions are pinned by `tests.test_combat_solver_compare.RegressionTests2026_09_02`:

1. **Same-turn route suffix must not overwrite the initial full route.**
   The mod re-emits a trimmed route package after every played card; the
   tracker binds anchors first-wins and the cross-turn fallback scans
   snapshots in arrival order, so the deviation baseline is always the
   route as first displayed for that turn.
2. **`final_hp` feeds the whole-battle HP error.**
   `BattleRecord.predicted_hp_loss` prefers the snapshot's explicit
   battle-level `predicted.hp_end` (the log adapter's `final_hp`), falling
   back to the final route step's `predicted_hp_end`.
3. **Routes do not mask solver-level failures.** A battle containing a
   `SEARCH_ERROR` / `TIMEOUT` / `CRASH` failure counts as failed even when
   other turns produced usable routes (`SOLVER_LEVEL_FAILURE_REASONS`;
   per-turn NO_ROUTE/STALE remain diagnostics unless the whole battle got
   nothing).
4. **Battles without comparable turns cannot improve the deviation rate.**
   The deviation denominator is battles with ≥1 comparable turn
   (`n_battles_comparable`), not all battles; excluded battles are reported
   in `n_battles_not_comparable`.
5. **Short/Deep latency and process working set are tracked separately.**
   The budget carries `short_elapsed_ms`, `deep_elapsed_ms` (fresh searches
   only; replays excluded) and `process_working_set_mb` (every RESULT);
   aggregation reports `solve_short_ms` / `solve_deep_ms` /
   `process_working_set_mb` with optional gates `p95_short_solve_ms`,
   `p95_deep_solve_ms`, `peak_process_working_set_mb`.

## What the Combat Solver is

Steam Workshop item **3790899961**, author Torch (requires **RitsuLib
0.5.13+** — installed 0.5.18; targets game **v0.111.0** — the same locked
build as the version lock; installed mod version **0.25.3**). Single-player
combat route solver: simulates hand, piles, enemy intents, potions, relics
and **cross-turn state**; displays a per-turn route with a **predicted HP
loss** under an explicit **search budget** (Low/Medium/High/VeryHigh/Custom;
this machine runs VeryHigh: short 20 s / deep 300 s, beam 54/135, nodes
10k/50k, NoGC 16 GiB, 8-way parallel). Its battle-sim core is adapted from
Random Foreseer with the original author's permission.

**License caution**: the public repo (github.com/Torch1230/CombatSolver)
ships *no license* — source-visible but not licensed for copying or
redistribution. This integration therefore only *consumes the installed
mod's own outputs*; no upstream code is copied into this repository, and no
game or mod internals are patched.

## Role in the architecture

```text
                        ┌─ Combat Solver (mod, in-game) ── route/prediction ─┐
game -> STS2MCP (GET) ──┤                                                    ├-> advisor overlay
                        └─ existing out-of-combat policies (unchanged) ──────┘
```

- Combat Solver becomes the **in-combat candidate upper module** only after
  Phase B passes its gates. Out-of-combat decisions (map/event/shop/rest/
  card-reward/Neow) stay with the current advisor stack.
- State recording, the version lock, win-rate statistics, and the 500-run
  acceptance protocol are **unchanged and remain authoritative**.
- Phase D (later): distill a low-latency combat policy from accepted solver
  routes; hard battles may still call the original searcher. Teacher v3 and
  combat PPO stay frozen unless that phase reopens them (see freeze record).

## Phase A — structured read-only interface (implemented)

`combat_solver/` package. Design rules: no UI scraping, no memory reading,
no POST to the game, every normalized record traceable to one exact
advisor-visible state via `state_hash` (the bridge's `decision_id`,
normalized SHA-256 of the raw state).

### Record contract (`combat_solver/snapshot.py`, schema_version 1)

`SolverSnapshot` — one solver answer bound to one state:

| field | content |
| --- | --- |
| `state_hash` | `local-sha256:…` of the raw STS2MCP state (or bare 64-hex) |
| `battle_turn` | solver's own turn label for the state |
| `route` | per-turn steps: ordered actions (`play`+card_id+target_index / `potion` / `end_turn` / `select` / `target`) and per-step `predicted_hp_end` |
| `predicted` | battle prediction: `hp_loss` / `hp_end` / `win` (≥1 populated) |
| `budget` | tier, time/node/memory limits, observed `elapsed_ms`, `nodes_expanded`, `peak_memory_mb` |
| `candidates` | ranked alternates (rank, hp_loss/hp_end, summary, actions) |
| `provenance` | reader name, captured_at_utc, mod_version, source_file |

`SolverFailure` — typed absence: `NO_ROUTE / TIMEOUT / CRASH / PARSE_ERROR /
STALE_STATE / UNSUPPORTED_SCREEN / READER_DOWN`. Raw payloads are preserved
verbatim by readers for audit; the normalized schema is strict (unknown keys
→ error; format evolution bumps schema_version).

### Readers (`combat_solver/reader.py` + `combat_solver/logformat.py`)

Upstream source review (2026-09-02) established that the mod has **no
machine-facing interface** — no HTTP listener, pipe, socket or file watcher.
What it does write to disk (all verified present on this machine):

| source | path | content |
| --- | --- | --- |
| diagnostic logs | `%APPDATA%\SlayTheSpire2\logs\*.log` (`[CombatSolver/Test]` lines) | full search blocks (grammar v1, below) |
| problem package (manual) | Desktop `CombatSolver-BugReports\*.zip` | combat-state.json, current-route.txt, replan-audit.txt, settings.json, forensics checkpoints |
| settings | `%APPDATA%\SlayTheSpire2\combat_solver_settings.json` | tier/budgets (read live by the adapter) |

Readers implemented:

- `JsonlSource` — normalized snapshot/failure lines (offline analysis +
  tests; `runtime/combat_solver/snapshots.jsonl`).
- `LogTailSource` (`logformat.py`, grammar **v1, calibrated against the
  real installed-mod log** and validated by full-session replay: 108
  snapshots, 47 typed failures, 0 empty routes against 246 raw RESULT
  lines). Block structure: `SEARCH_REQUEST generation=N turn=T` opens a
  block; `RESULT … reused= searched_turns= battle_hp_lost_so_far=
  projected_battle_hp_lost= total_elapsed_ms= expanded=
  total_worker_allocated_bytes= final_hp= combat_ended_turn=` carries the
  prediction; `TURN_OUTCOME turn=K hp_lost=` gives per-turn predicted loss;
  `ACTION turn=K kind=PlayCard card_id=… target_index=…` /
  `kind=UsePotion potion_id=…` / `kind=EndTurn` materialize the route; a
  second RESULT closes the previous block (the mod re-emits the full route
  package after every played card without a new REQUEST); `SEARCH_FAILURE`
  → `SEARCH_ERROR`, `SEARCH_STALE` → `STALE_STATE`; blocks flush at the
  next REQUEST, at combat RESET, and at end-of-stream. Semantics: fresh
  searches carry cost stats (elapsed/nodes/worker-bytes→peak memory MB);
  `reused=True` blocks are route replays — zero cost, and their snapshot
  re-binds to the turn of the package's first action (48/48 replayed
  packages re-bound correctly); a repeated identical reused package is
  suppressed as a log echo. Budget tier/limits come from the mod's own
  settings JSON (VeryHigh / 300000 ms / 16384 MB observed).
- `DirectorySource` — hands manually exported problem packages to an
  injected parser; this is also where candidate routes become available
  (from forensics checkpoints), the only disk location that carries them.

Candidate routes are supported by the schema (`candidates`) but produced
only from problem packages; the live log stream exposes the selected route
only. HP-error gates run on battle-level prediction (`final_hp` /
`projected_battle_hp_lost`) plus per-turn loss (TURN_OUTCOME), which the
tracker converts to comparable per-turn HP via the previous actual HP
boundary.

### Executed-action inference (`combat_solver/executed.py`)

Pure read-only reconstruction of what was actually played from consecutive
player-turn states (hand multiset diff → plays; energy arithmetic; potion
diff; round advance → end turn; single-enemy state delta → target). Anything
not uniquely determined flags the turn **ambiguous** — excluded from
deviation statistics, reported separately, never guessed.

### Battle tracking (`combat_solver/session.py`)

`BattleTracker` binds snapshots to turn anchors by **state hash first**,
then by turn label (guarded against stale cross-battle binding), keeps
cross-turn routes available for later turns, and emits one `BattleRecord`
per battle on combat exit. Snapshots that arrive before the first monster
poll are retried from a pending buffer instead of being dropped.

## Phase B — independent comparison, 50–100 fixed battles (implemented; blocked)

Runner: `scripts/run_solver_comparison.py` (GET-only; reuses the version
lock, mod inventory lock and trace recorder). Seeds:
`data/combat_solver/fixed_battle_seeds.json`, partition
`combat_solver_fixed_battles` at **1,600,000,000+** (disjoint from every
existing partition; regenerate with
`scripts/make_solver_comparison_seeds.py`). The file contains **12
pre-registered run seeds**, not 12 battle rows; each run can produce several
combats, so the current estimate is 12 × ~9 = 108 battles and the harness is
capped by `max_battles`.

Seed handling is explicit and fail-closed. In fixed mode, `bridge.autoplay` is
the sole component allowed to start a run: it consumes the pre-registered
allocation in order and stops when the allocation is exhausted. The candidate
bridge accepts the requested seed and the controller reads the authoritative
`compendium.current_run.seed` back before committing it to the batch ledger.
The checked-in allocation records both capability states: the candidate bridge
supports injection, while the currently installed bridge remains unverified
until its DLL hash/capability is updated in the version lock. A fixed run is
therefore claimable only under that candidate installation and after matching
authoritative readback. Canonical decimal strings are normalized only for
partition lookup; alphanumeric save seeds are preserved but are not coerced
into integers. A missing, non-compatible, or out-of-partition seed stops the
batch and writes a non-claiming manifest. When fixed injection is unavailable,
use `--seed-mode observational` (or `--observational`) for diagnostics only.
Observational output has `fixed_seed_claim=false` and can never be accepted as
Phase B fixed-seed evidence. The candidate implementation routes standard
singleplayer through the game's public `NCharacterSelectScreen.BeginRun` and
the controller verifies `current_run.seed`; it is built in staging but is not
the installed/lock-verified bridge yet.

Per battle the batch reports: **predicted vs actual HP loss** (battle level
only when the route horizon reaches battle end; per-turn otherwise),
**route deviation rate** (Wilson 95% interval; trailing end-turn stripped so
winning before end-turn is not a deviation), **solve time** (P50/P95 from
solver-reported `elapsed_ms`), **memory** (solver-reported peak; optional
process-level measurement), **failure rate** (typed failures + battles with
zero solver answers; Wilson upper bound).

Gates live in `config/combat_solver.toml` (`[gates]`); rates bind on Wilson
95% bounds so a small sample cannot pass by luck. The current defaults are:

- Wilson 95% **upper** bounds: failure rate ≤ 0.10, route-deviation rate ≤
  0.15, and no-route-turn rate ≤ 0.10.
- Wilson 95% **lower** bounds: prediction coverage ≥ 0.90, comparable-battle
  coverage ≥ 0.90, and route availability ≥ 0.90.
- HP error: mean absolute error ≤ 3 and P90 absolute error ≤ 8.
- Fresh-search latency: P95 short solve ≤ 10 s and P95 deep solve ≤ 300 s.
- Process gate: peak process working set ≤ 12,288 MiB (12 GiB).

Verdict `accepted` requires ≥ `min_battles` (50) and all gates passing; the summary
is written to `runs/combat_solver_compare/<batch>/summary.json` with a full
manifest (locks, hashes, mod versions).

### Mod-environment lock

`config/combat_solver.lock.json` is a **separate track lock**: allowed mod
ids `STS2_MCP + RitsuLib + CombatSolver`, DLL inventory hash-pinned, and the
same locked game identity. The A10 acceptance track keeps
`config/live_version.lock.json` with `allowed_mod_ids: [STS2_MCP]` — results
obtained under the comparison track's mod set never contaminate that
protocol (the harness fails closed until the inventory is filled at install
time; verified: game identity check passes, inventory check refuses).

### Procedure per battle (fully automated smoke/phase)

1. Start the locked build with the track's mod set and the approved full-auto
   driver. Pass `--automated`; the driver handles all game and route actions
   while the harness GET-polls and records every state (`trace.jsonl`). No
   hand-play step is required.
2. Each solver answer is consumed from the reader and bound to its turn's
   `state_hash`; the automated executor's `deploy_log` supplies the executed
   route, so deviations remain measurable rather than hidden.
3. The automatic invariant audit checks state/route binding, route lengths,
   typed failures, coverage denominators, seed claims, unique battle IDs and
   sample keys, and journal/checkpoint consistency. A human spot-check is
   optional and is not a release prerequisite.
4. On combat exit the battle record closes with actual HP and outcome;
   `battles.jsonl` is appended incrementally and a full-record checkpoint plus
   summary/manifest are atomically refreshed after every battle.
5. `--resume` continues an existing batch explicitly. It starts battle IDs
   after the highest committed sequence, rejects duplicate `battle_id` or
   stable sample keys, and refuses a changed seed-file hash or seed mode.
6. `--dry-run` verifies locks/reader/seeds without touching the game.

## Phase C — adoption (pending Phase B verdict)

If accepted: the Combat Solver becomes the in-combat candidate source in the
advisor overlay; combat advice display switches from policy-ranked
candidates to solver routes; out-of-combat flow unchanged; every advice
session keeps the same trace/lock/statistics plumbing. If rejected: record
the verdict, keep the interface (it is solver-agnostic), and reopen the
freeze decision for teacher v3 / PPO paths.

## Phase D — distillation (later)

Accepted solver routes become expert labels for a low-latency combat policy
(reusing the BC contract stack: hash-verified materialization → masked-CE
training → harness evaluation). High-difficulty battles may retain the
original searcher as fallback. Teacher v3 remains frozen unless this phase
explicitly reopens it with a fresh strength-gate run.

## Open items

- **Automated smoke/invariant audit (blocks formal Phase B):** run the
  `--automated` observational smoke below. It automatically checks route
  binding, HP/error accounting, typed failure accounting, short/deep latency,
  process working-set samples, coverage denominators, and journal/checkpoint
  consistency. Human review may spot-check a record, but no manual item-by-item
  audit or hand-play step is required. Formal Phase B remains blocked until the
  candidate seeded bridge is installed, its DLL capability/hash is added to
  the version lock, and a fixed smoke verifies authoritative seed readback.
- Optional: problem-package adapter for `DirectorySource` to surface
  candidate routes and battle-state replays in comparisons.
- **Seed-input reachability via the bridge (fixed-seed runs):** the candidate
  bridge accepts the raw requested seed and the client reads the authoritative
  `compendium.current_run.seed` before committing the batch-local allocation
  ledger. The currently installed old bridge is not injection-capable/lock-
  verified, so fixed Phase B remains blocked until candidate installation,
  lock update, and a real fixed smoke all succeed. The allocation metadata
  deliberately records `candidate_bridge_supported=true` and
  `installed_bridge_supported=false`.

## Smoke-5 audit record (2026-09-02, automated batch)

The first automated 5-battle smoke (`runs/combat_solver_compare/smoke-5/`,
mod 0.26.0, full-auto deployment) surfaced three systematic defects, each
now fixed with a pinned regression test:

1. **Mid-combat selection screens split battles.** A `hand_select` at round
   7 split one 15-round Exoskeleton fight into two records (rounds 2-5 and
   7-8). Fix: `hand_select`/`card_select` never close an open battle.
2. **Combat-end heal inflated the measured HP loss by exactly +6** on every
   won battle (Burning Blood; the last monster-state poll lands after the
   heal applies while the solver's `final_hp` predicts pre-heal HP). Fix:
   won battles are adjusted back by the relic's combat-end heal
   (`heal_adjustment` recorded per battle).
3. **Mid-batch attach artifact.** The harness attached to a fight already in
   progress (round 2); its first turn had a truncated deploy record and no
   route baseline — one false deviation. Fix: battles attached mid-fight
   (first observed round > 1) are closed but not recorded
   (`skipped_midbattle`).

Corrected post-hoc audit of the same batch (heal-adjusted, split fight
excluded): **3 clean battles, every one a win with |predicted − actual|
HP error = 0 (10/10, 8/8, 9/9), 12/12 comparable turns all
`deploy_log`-sourced, 0 deviations, 0 solver-level failures**; short-search
latency P95 ≈ 1.07 s (deep search never triggered on easy fights), solver
worker memory peak ≈ 978 MB, game process working set max ≈ 9 GB.

Open observation (mod behavior, not ours): 0.26.0 reused packages keep
their ACTION turn labels at the initial turn instead of advancing, and one
fight ran 15 rounds on a 4-turn forecast horizon — our cross-turn binding
absorbed this (routes still matched turns 2-5 with zero deviation), but
turns beyond the solver's own horizon are honestly not comparable.

## Running the comparison batch

```powershell
# 1. verify the environment (no game connection attempted):
.\.tools\python\cpython-3.12-windows-x86_64-none\python.exe `
  scripts\run_solver_comparison.py --batch-id <id> --automated --dry-run --reader-mode logtail

# 2. current-installed-bridge automated smoke: observational mode is explicit
#    and diagnostic only (the installed bridge is not fixed-seed verified):
.\.tools\python\cpython-3.12-windows-x86_64-none\python.exe `
  scripts\run_solver_comparison.py --batch-id smoke-observational --automated `
  --seed-mode observational --reader-mode logtail --max-battles 5

# 3. formal fixed-seed Phase B template (BLOCKED until the candidate bridge is
#    installed, lock-updated, and authoritative readback smoke passes):
.\.tools\python\cpython-3.12-windows-x86_64-none\python.exe `
  scripts\run_solver_comparison.py --batch-id fixed-candidate-smoke --automated `
  --seed-mode fixed --reader-mode logtail

# 4. The lower-level comparison runner can resume its own partial records
#    after a bridge/game interruption; this is NOT supervisor resume support:
.\.tools\python\cpython-3.12-windows-x86_64-none\python.exe `
  scripts\run_solver_comparison.py --batch-id smoke-observational --automated `
  --seed-mode observational --resume --reader-mode logtail --max-battles 5
```

The lower-level comparison runner GET-polls STS2MCP (0.25 s default), tails the game log for
solver snapshots, waits up to 3 minutes for the bridge to come up, and
writes `runs/combat_solver_compare/<batch>/` (trace.jsonl, battles.jsonl,
summary.json, manifest.json, records_checkpoint.json). Bridge loss is retried
for 30 seconds, then the batch is marked `partial`/`bridge_disconnected` with
summary and manifest still present; that runner may restart with `--resume`.
The batch supervisor deliberately creates a new batch and never resumes old
comparison records; only an explicitly supplied old seed ledger may be used
to reconcile an active seeded run reservation. Ctrl+C and SIGTERM are also recorded. Automated smoke/phase runs use the approved
full-auto driver and must carry `--automated`; the harness itself remains
GET-only. Advice-only is reserved for manual diagnostic sessions, not a
prerequisite for the automated comparison path. Note the log file rotates
between game sessions — the tail source picks up new `*.log` files
automatically (sorted by name; the newest file is `godot.log` while a
session is running).
