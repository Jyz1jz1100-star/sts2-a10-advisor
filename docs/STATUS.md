# Current status

## Handoff environment audit: 2026-09-19

Takeover pass, read-only with respect to the game (no process started, no POST,
no bridge file touched). Two findings an operator needs before any live work:

1. **The mod environment drifted again.** Steam Workshop silently updated
   CombatSolver to **0.41.0** (`41BDB5DA3CD90115965B1CFDA55CB2E899F346F6366680BE589E5E757E6E5783`,
   files timestamped 2026-09-18 16:31) and STS2-RitsuLib to **0.6.2**
   (`E3959F1746FCB7AA404CB9CD861443DC540E8488B50F7D156EACBE79925156B6`).
   `config/combat_solver.lock.json` still pins 0.31.0 / an older RitsuLib hash,
   so the lock's *uncommitted* working-tree edit is itself now stale and was
   deliberately **not** committed. STS2_MCP (`CD3EA740…`) and RegentFX
   (`0F0262B3…`) still match their locks. Game build is unchanged
   (`24724944` / v0.111.0 / `41cef1ea` / `222455745`). The 09-11~09-18 game
   sessions produced no `ssb-*` batch directory, so their solver logs have no
   run binding and remain 无法归属 under
   `scripts/report_solver_log_attribution.py` semantics.
2. **`G:\qoder\sts2-a10-advisor` is now the authoritative copy.** It was not
   self-sufficient: the copy lost uv's `.tools/python/cpython-3.12-*` junction
   (so `scripts/test.ps1` refused to start) and had no sibling `third_party`
   (which every training script resolves as `ROOT.parent/third_party`). Both
   are restored — the junction to the copy's own interpreter, and a
   `G:\qoder\third_party` junction to the existing
   `G:\ds harness\third_party` (no duplicate GB-scale copy, no move of the
   shared emulator checkout). `live_version.lock.json`'s
   `evaluation_environment.disabled_mods_backup_dir` and the
   `ACCEPTANCE_BASELINE_2026-09-08.md` command paths now point at `G:\qoder`;
   frozen provenance records (`data/teacher/*/manifest.json`,
   `models/*.metadata.json`) intentionally keep their historical
   `G:\ds harness\…` `emulator_root` strings and must not be rewritten.
   `scripts/test.ps1` after the repair: **415 contract + 99 training
   environment tests pass (514 total)**, with the training half resolving the
   default adjacent emulator venv. These are contract/simulator checks; no game
   was started and no live acceptance claim is made.

## Overnight landing: 2026-09-19 (session 5)

Read-only with respect to the game throughout: no process started, no POST, no
Steam or Workshop state touched, and the in-game Combat Solver was never asked to
stand down.

- **Grammar v2 is merged and inside the acceptance suite.** The 0.41.0 journal
  reader (`combat_solver/logv2.py` + `loggrammar.py`) landed as `d2b510a`, and
  `tests.test_combat_solver_grammar_v2` (47 tests) is now registered in
  `scripts/test.ps1` -- it never was before, so the "415 contract tests pass"
  figure quoted above did not exercise the container the mod actually writes.
  Suite as of this session: **473 contract + 99 training-environment tests**.
  The codec was re-verified over the 15 real sessions with no drift from its
  documented baseline (72 snapshots, 69 byte-ranged deploys, 22 typed failures,
  0 envelope errors). Evidence-channel semantics are in
  `docs/GRAMMAR_V2_TODO_2026-09-19.md`.
- **A live batch now attests the binaries it ran against**, at both ends of its
  window (`combat_solver/modpin.py`, wired into
  `scripts/supervise_solver_batch.py`), replacing the precondition nobody could
  satisfy -- Workshop has no per-item auto-update switch to turn off. A dry-run
  manifest records `STS2_MCP 0.4.0` and `RegentFX 0.5.1` matching, `CombatSolver
  0.41.0` and `STS2-RitsuLib 0.6.2` drifted from the 0.31.0 lock, and
  `attested: false`.
- **The bridge can no longer call a survived run a victory.** `bridge/outcome.py`
  drops the `hp > 0 means won` fallback; see `docs/ACCEPTANCE.md` for what an
  explicit terminal victory can and cannot mean over this bridge.
- **First non-zero V2 Act 1 terminal wins**, reproduced independently four times:
  `docs/ACT1_CAMPAIGN_2026-09-19.md`. Simulator-only (`scope:
  simulator_act1`) — and that label is qualified by two measurements the same session:
  the emulator defines exactly two acts (`ActOvergrowth`, `ActUnderdocks`) and picks
  one per run by seed (`RunMapGenerator.cs:10`), so a "simulator_act1" win rate is a
  single-act clearance over a ~50/50 two-act population; and splitting 10000 episodes
  by generated act gives **Act 1 = 3/5014 (0.06%)** versus Act 2 = 65/4986 (1.30%),
  so the strictly Act 1 evidence is three named, individually re-run seeds
  (130015189, 130017978, 130019400). Both acts end at floor 17, there is no Act 3,
  and the one Act 1→Act 2 chain (hardcoded seed `7MS1YN8NWB`) was driven and is
  unattainable — four checkpoints died at floors 6-8. No simulator result can
  evidence a three-act clear.
- **What still stands between here and a real three-act clear** is not code. The
  supervisor's refusal to launch the game is deliberate and currently
  load-bearing: the profile save tree was written at 00:03-00:04 local, so a
  cold-started run could displace one in progress. An attempt needs the operator
  to start the game; `python scripts/supervise_solver_batch.py --mode
  observational --allow-actions --max-battles 50` then does the rest.

## Offline Step 4 deploy-log binding: 2026-09-14

The Step 4 evidence mechanism is now implemented and fail-closed offline. The
log tailer records LF-aligned half-open byte ranges for each mod deploy,
including SHA-256 and exact `SEARCH_REQUEST`/`DEPLOY_*` marker counts; it
buffers torn tails, resets state on rotation/truncation or any consumed-prefix
rewrite, and keeps legacy deploy records diagnostic-only. Durable comparison
records bind the range to concrete run/battle/decision/turn identity. The
assessor reopens the permitted log source, verifies path/identity/range/hash,
ordered grammar, and action-by-action agreement before accepting an in-memory
trusted deploy event. Missing, tampered, overlapping cross-run, mismatched, or
serialized-only evidence cannot clear the deploy-log blocker, which is also
checked per run so one run cannot lend evidence to another.

DeepSeek implementation/test workers produced the patch candidates and
regression tests; independent DeepSeek adversarial reviews ended at GO after
the identified Debug-marker, LF-boundary, reset, decision-binding, dedupe, and
per-run aggregation defects were fixed. The project small-runtime suite and
all 99 training-environment tests pass. This was offline only: no game was
started, no POST/action was sent, and no lock file was changed. The remaining
Step 4 work is a controlled live batch proving that real current-version log
grammar and rotation behavior produce these bindings end to end; until then,
no live acceptance or win-rate claim is made.

## Acceptance baseline for the next live phase: 2026-09-08

Step 1 of the agreed plan is done offline (no game was started, no HTTP was
sent): code/workspace/test state recorded, game and bridge verified **equal**
to `live_version.lock.json` (v0.111.0 / build 24724944 / STS2MCP DLL+manifest
hashes match), while the Workshop-updated RitsuLib/CombatSolver DLLs **drift**
from the user's `combat_solver.lock.json` (drift documented, verification not
relaxed — the comparison child therefore refuses to start until the operator
re-reconciles that lock). The godot log-count differences across reports are
point-in-time snapshots of a log directory the game itself rewrites; the
authoritative 2026-09-08 inventory is
[`runs/evidence_baseline_20260908/attribution.json`](../runs/evidence_baseline_20260908/attribution.json)
(5 logs, all 无法归属). Precise commands, stop conditions, and acceptance
checklists for the stop-rehearsal (step 2) and the first fixed-seed full game
(step 3) are in
[`ACCEPTANCE_BASELINE_2026-09-08.md`](ACCEPTANCE_BASELINE_2026-09-08.md).

## Batch lifecycle fix + evidence attribution: 2026-09-07 (second pass)

Task-scoped fix round after the read-only audit below. The failed batch
`ssb-20260906T102557Z-183d0b05` was root-caused offline (no game was started):
the game served a byte-identical frozen main menu (options
`continue/abandon_run/…` with **no** `singleplayer`) for 72 s while the
compendium proved no saved run, so autoplay correctly refused the unverifiable
Continue but crash-looped 61 identical start attempts and died with exit 1;
the supervisor's CTRL_BREAK stop killed the comparison child before its final
summary, leaving `status=running` residue. Fixes: `BridgeConnectionError`
classification, bounded retries with a re-read of fresh state, autoplay
classified stop (trace `session_end` + exit code 3 with the recorded reason),
comparison SIGBREAK cooperative shutdown, supervisor-side final verification
that records residual comparison state honestly (never promotes it) on every
terminal path including comparison-driven completion, autoplay exit-0
completion path, torn-tail-tolerant trace summary reading, and
`combat_solver_logs` inventory snapshots (observation semantics:
`pre_existing` vs `created_during_window`) in every new batch manifest. New offline tooling:
`scripts/report_solver_log_attribution.py` marks all five historical solver
logs **无法归属** with explicit reasons (no run binding, no snapshot window);
time proximity is never attribution evidence, and missing evidence keeps
`assess_full_run.py` at `accepted=false`. Full details and evidence:
[`BATCH_LIFECYCLE_FIX_2026-09-07.md`](BATCH_LIFECYCLE_FIX_2026-09-07.md).
No version lock, seed allocation, route policy, or acceptance whitelist was
changed; `config/combat_solver.lock.json` keeps its pre-existing user edits.

## Read-only audit update: 2026-09-07

The dated live-session section below is historical; the latest audit found the
supervisor batch `ssb-20260906T102557Z-183d0b05` failed with the bridge
unavailable, and no game process or bridge listener was available during the
read-only check. The layered direction remains valid, but the joint system has
not reached an accepted full-run result. See
[`LIVE_PROGRESS_2026-09-07.md`](LIVE_PROGRESS_2026-09-07.md) for the evidence.

The new route planner is an offline map-only prototype. A real historical
`ssb` autoplay trace was read line by line and compared without game I/O:
7,450 input lines produced 75 map events, 30 unique map states, 20 successful
two-policy comparisons, 3 legal action divergences, 45 duplicate drops, and
10 excluded map states with no legal candidates. The report is at
[`comparison.jsonl`](../runs/route_comparison/ssb-20260905T193903Z-4e8b9eb1-20260907/comparison.jsonl),
with provenance and input SHA-256 in its summary. These divergences are an
observational policy difference and are not a win-rate or full-run result.

The 2026-09-07 `scripts/test.ps1` run passed 371 small-runtime tests and 99
training-environment tests (470 total). It did not start the game, send POST
actions, or run a production training job.

Date: 2026-09-06 (first live seeded A10 run; lock drift reconciled; batch automation hardened)

## Live session 2026-09-06: real-game fixed-seed A10 run in progress

With Steam healthy (the 09-03 launcher blocker was cleared by the manual
Steam update), the machine now runs its first end-to-end automated A10 run:

- **Seed injection verified live.** `start` POST with pre-registered
  allocation seed `1600000000` produced an active standard/Ironclad/A10 run
  whose authoritative `compendium.current_run.seed` read back `1600000000`
  exactly. The installed seeded candidate bridge (CD3EA740...) works; the
  fixed-seed Phase B blocker is cleared for this installation.
- **Lock drift reconciled (2026-09-06).** `live_version.lock.json` now records
  the installed seeded DLL; `combat_solver.lock.json` records CombatSolver
  0.29.1 (Workshop auto-update; grammar v1 replay-verified over the
  2026-09-05/06 logs: 1960 events, 548 snapshots, 0 empty routes) and the
  STS2MCP seeded hash. GET-only probe passes against the solver lock.
- **Supervisor integration fixes found by first live use:** autoplay now
  receives the solver lock (the comparison track loads 4 mods), the runner's
  identity-read window is 120s (VeryHigh searches block the mod listener
  longer than 15s), the runner tolerates the save-write seed window (60s
  grace), and the ledger can adopt an orphan run whose verified seed is
  exactly the next allocation entry.
- **Keeper click path fixed and recalibrated for 0.29.1:** Unicode
  marshalling + `[NullString]::Value` for FindWindowW (PS binds `$null` as
  empty string), nested `W.U+RECT`/`W.U+POINT` refs, and a live-recalibrated
  toggle position with a vertical offset walk for panel-height variance.
- Batch supervisor `ssb-*` drives run starts (fixed allocation), out-of-combat
  autoplay, full-auto keeper recovery, and the comparison runner. Monitoring
  continues until the run reaches an explicit terminal victory.

### Live run results (updated 2026-09-06 05:40 UTC+8)

| run | seed | result |
|-----|------|--------|
| 1 | 1600000000 | death ~floor 8 (Act 1) |
| 2 | 1600000001 | **Act 1 boss (WATERFALL_GIANT 250hp) killed after a 15-turn fight**; death early Act 2 at ~hp 41 |
| 3 | 1600000002 | in progress (live seed readback verified) |

Automation fixes found during these runs (all committed): keeper Unicode
decode crash on cp936 PowerShell output, catch-all around keeper clicks,
reconcile order for already-consumed runs, `--seed-ledger` reuse across
batches, identical-action loop breaker, and a covered-window guard so the
keeper never clicks into a human's foreground windows (the machine is shared
with an active user; the game was observed fullscreen-behind a browser).
Known mod-side gap: Combat Solver 0.29.1 routes occasionally end without an
EndTurn action, stalling the battle until the toggle is re-armed; the keeper's
offset walk covers the two panel layouts seen so far.

## Current acceptance snapshot

The project direction is now a layered execution design with one declared
execution mode per run. In `combat_solver_full_auto` mode, the mod owns combat
and `bridge/autoplay.py` owns out-of-combat decisions; concurrent combat
owners are forbidden. The live policy is still a heuristic baseline over the
visible candidate codec; there is no deployable out-of-combat trained model.
The codec and policy tests are offline fixture tests and do not prove a
real-game full run. Event/Neow candidates are enumerable, but the current
heuristic has no validated event utility model and autoplay retains a
conservative fallback for those screens.

The 2026-09-03 Act 1 simulator bulk experiment completed 20,000,000 steps. Its
authoritative final metrics are `wins=1`, `episodes=500`, `win_rate=0.002`
(`1/500 = 0.2%`), mean final floor `7.67`, and `promoted=false`. The metrics
scope is `simulator_act1`; this number is not a real-game or Combat Solver
joint-system result. See
[`experiment-final.json`](../runs/bulk_training/act1-pretrained-r3-bulk-20260903T0640Z/act1/metrics/experiment-final.json).

The offline full-run ledger and candidate contract are useful acceptance
infrastructure, but no verified real-game end-to-end run has yet supplied all
of: locked game/mod provenance, a fresh Ironclad/A10/standard identity, one
execution owner, complete decision/result evidence, and an explicit terminal
victory or loss. Therefore the 20-run pilot and 500-run formal gates remain
unstarted/unaccepted. The local suite must not be reported as a live-game
win-rate result.

The current reproducible offline validation command is the repository test
entrypoint:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\test.ps1
```

This command is read-only with respect to the game: it uses fixtures and fake
controllers. It is a contract/integration check, not a live acceptance run.
On 2026-09-05 this entrypoint passed **337/337** small-runtime tests plus
**99/99** training-environment unit tests (**436/436** total); these are
unit/simulator checks and did not start the game, send POST actions, or run a
production training job.
The dated sections below retain prior experiments and conclusions for history;
their old test counts and manual-operation wording are historical, not current
acceptance claims.

## Comparison batch status corrected (2026-09-02)

The earlier `phase1-50` attempt is **INVALID / ARCHIVED**: it stopped after
17 battles, its observed `run.seed` values were all null, and the resulting
rows are polluted observational data rather than fixed-seed evidence. That
batch is no longer running and must not be resumed or reused as a valid
comparison result.

This review round completed the lifecycle/journal recovery, seed-claim
fail-closed, automated smoke/invariant-audit, and documentation corrections.
The review suite reports **299 tests passing**.

The seed-read plumbing is now explicit: `BridgeClient` keeps the raw
`singleplayer.run.seed` when present and otherwise reads
`compendium.current_run.seed` from `current_run.save`, with provenance in the
state. This is read-only and does not alter the installed bridge DLL. STS2MCP
still rejects a requested seed on standard singleplayer character select, and
the checked-in allocation is numeric; an alphanumeric save seed therefore
remains observational until a compatible fixed-seed contract is reviewed.

## Fully autonomous comparison loop implemented (no valid batch running)

**Zero-human-operation automation is implemented, but no valid comparison
batch is currently running.** New `bridge/autoplay.py`
drives every out-of-combat screen over STS2MCP POST actions (authorized by
the explicit `--allow-actions` flag; all POSTs traced): menu/start-run
(continue-resume for auto-saved runs), Neow (prefers the card-removal
option), card rewards, shop, campfire (rest < 75% HP), map pathing,
treasure/relic/bundle two-step flows, rewards-loot (claims gold/potion/
relic, skips cards), event first-option fallback, and fresh-run starts
after deaths. The wire contract was extracted from the STS2MCP dispatch
source and cross-checked against real recorded POSTs: internal
`map_choose_node` → wire `choose_map_node`, `rewards_pick_card` →
`select_card_reward` (card_index), `shop_buy` → `shop_purchase`,
`rest_choose_option` → option list index, removal flows are two-step
(`select_card` toggle then `confirm_selection`, state-driven via
`can_confirm`).

**Full-auto keeper** (`bridge/fullauto_keeper.py`): the mod's full-auto
mode can drop to off (per-battle resets, divergence exits), and a battle may
enter `control_mode=manual_plus_solver` without any
`FULL_AUTO enabled=false` line. The keeper watches each
`SEARCH_REQUEST ... turn=1` plus `UI_STATE state=ready`; if
`FULL_AUTO_DEPLOY turn=1` does not arrive before the bounded timeout, it
requests recovery. Explicit `FULL_AUTO enabled=false` is only an auxiliary
trigger. Clicks convert client coordinates with `ClientToScreen`, use the
persisted/runtime overlay position, fail closed when that position is
unavailable, and require post-click log confirmation.

**Card-reward rule liberalized after user review**: pick the best offered
card whenever any non-basic, non-curse card appears (the old skip-by-
default rule starved the deck); skip only when the reward is literally
only basics/curses.

The prior autonomous trial verified predictions exact on 6/7 early battles
(the one miss, err 14, is an elite fight with mid-fight replans); deploy_log
source was 100%. Those observations belong to the invalid archived attempt
above and are not fixed-seed evidence.

## Out-of-combat advisor v1 live (2026-09-02, session 6)

**The advisor now actually advises out-of-combat.** `advisor_core/live.py`
defaults to `--policy heuristic`: the new `LiveHeuristicPolicy`
(`advisor_core/policy_live.py`, model id `heuristic-out-of-combat-v1`)
covers **card reward / shop / rest site / map pathing** on live STS2MCP
states with explicit A10 rules (deck thinning with skip-by-default,
shop priority 删卡 > 遗物 > 高稀有卡 > 药水, rest < 75% HP else smith,
path preference by HP band incl. elite avoidance when hurt), every
recommendation carrying its facts in Chinese. Combat screens deliberately
raise (the Combat Solver overlay owns in-combat). The historical statement
that event/Neow had no exposed option text is superseded by the live candidate
codec: visible options can now be enumerated, but no event/Neow utility policy
has been validated yet. 12 fixture-driven tests
(`tests.test_policy_live`); full suite **132/132** green.

Architecture note: wiring the trained BC/PPO models into live play is NOT a
drop-in — the V2 1739-int observation consumes the **emulator's native
state array**, so live states would require a full state-parity adapter.
The heuristic advisor is the honest first deliverable; a trained live
replacement needs its own dataset + training effort on live-shaped
features.

The archived `runs/combat_solver_compare/phase1-50/` attempt stopped at 17
battles after re-locking Combat Solver **0.27.0**. The harness has restart,
bridge-retry, and read-only compendium seed enrichment, but no new phase1-50
run is authorized until a compatible fixed `run.seed` is verifiable.

## Combat Solver pivot (2026-09-02, session 4)

In-combat candidate generation pivots to the external **Combat Solver**
mod (workshop 3790899961; Torch123; RitsuLib 0.5.13+ required; targets the
same locked v0.111.0 build). **Teacher v3 and combat PPO expansion are
frozen, not deleted** — markers: `data/teacher/FREEZE-TEACHER-V3.txt`,
`runs/curriculum_v2/FREEZE-PPO-EXPANSION.txt`; decision record:
[docs/FREEZE_2026-09-02.md](FREEZE_2026-09-02.md). State recording, the
version lock, win-rate statistics, out-of-combat decisions, and the 500-run
acceptance protocol are unchanged and stay authoritative. Phased plan and
the structured read-only interface contract live in
[docs/COMBAT_SOLVER.md](COMBAT_SOLVER.md); Phase B is the 50–100 fixed-battle
independent comparison (predicted vs actual HP loss, route deviation rate,
solve time, memory, failure rate) gated before any adoption. A separate mod
environment lock (`config/combat_solver.lock.json`) keeps the A10
acceptance whitelist (`allowed_mod_ids: [STS2_MCP]`) intact.

**Phase A live-calibrated; formal fixed-seed Phase B BLOCKED (2026-09-02,
session 5-6).** The raw state endpoint is GET-only and may report
`run.seed=null`; the harness now reads `compendium.current_run.seed` from the
active save when available. This is observation, not seed injection, and the
current numeric allocation cannot verify alphanumeric save seeds, so only an
explicit `--automated --seed-mode observational` smoke is honest until a
compatible fixed seed is exposed. The mods are installed and hash-locked (CombatSolver 0.25.3,
RitsuLib 0.5.18, RegentFX tolerated as cosmetic `affects_gameplay=false`).
The live log adapter (`combat_solver/logformat.py`, grammar **v1**) was
calibrated against the real installed-mod session log and verified by
full-session replay (108 snapshots, 47 typed failures, 0 empty routes; all
48 route-replay packages re-bound to the correct turn). Per review, five
regression gates are now pinned by
`tests.test_combat_solver_compare.RegressionTests2026_09_02`: (1) same-turn
route suffixes cannot overwrite the initial full route (first-wins anchor
binding + arrival-order fallback), (2) `final_hp` flows into the battle HP
error via `predicted.hp_end`, (3) battles with routes but a
SEARCH_ERROR/TIMEOUT/CRASH still count as failed, (4) the deviation-rate
denominator counts only battles with comparable turns, (5) short/deep
latency and process working set are aggregated separately with their own
optional gates. The automated invariant audit covers route binding, HP/error
accounting, typed failures, coverage, latency, process working set, and
journal/checkpoint consistency. Human spot-check is optional; no manual
item-by-item audit or hand-play step is required. The archived 17-battle
artifact remains invalid; formal Phase B waits for bridge support for a
verifiable fixed `run.seed` — procedure in `docs/COMBAT_SOLVER.md`.

## Review-fix stack (2026-09-01, session 3)

The 2026-09-01 review froze the R2 teacher lineage (`data/teacher/FREEZE-R2.txt`)
and required six fixes. All are implemented and tested; the emulator build was
**not** modified (native hash `bcd623ce…34bc4` unchanged).

1. **BC run-grouped split — seed leakage eliminated.** The round-2 "0.603"
   holdout was computed on a prefix-hash split in which **85.9% of holdout
   seeds also appeared in train**; that number is now historical only.
   `training/behavior_clone_v2.py` splits by whole run `(source, seed)` with a
   hard `shared_runs == 0` assertion, and excludes every sample with
   `seed >= RESERVED_TEACHER_TEST_SEED_START (1_410_000_000)` from training —
   this quarantines the dagger0 seeds (1,500,100,000+, which sit inside the
   reserved holdout range) by design. Retrained on the same frozen R2 corpus:
   **holdout top-1 0.5725 / top-3 0.7905** (2,110 samples, 844 runs,
   `holdout_run_leakage_rate 0.0`), best epoch 15
   (`models/bc_v2_r3_leakfree.pt` + metadata sidecar).
2. **Shaping gamma == PPO gamma.** `load_v2_training_config` rejects any
   config where `[reward].gamma != [algorithm].gamma` (both 0.995 in the
   checked-in configs; regression tests cover the loader and both files).
3. **Masked-CE pretraining into a `MaskablePPO.load`-able actor.**
   `training/bc_pretrain.py` distills the verified BC checkpoint into a
   MaskablePPO policy (masked cross-entropy on the leak-free sample split,
   BC sidecar hash chain verified before training), saves via
   `model.save(..., exclude=["env"])` and verifies `MaskablePPO.load` +
   `action_masks`-aware prediction on the saved artifact, with a sidecar hash
   chain (dataset files+SHA, BC ckpt SHA, artifact SHA, obs contract).
   Evaluated on the same 100-seed stage checkpoint partitions as the BC
   student (`runs/pretrain_eval/comparison-*.json`): the pretrained actor
   **beats the BC student on every stage** — floor3 boundary 0.79 vs 0.70
   (mean final HP 0.333 vs 0.201), floor6 0.22 vs 0.09 (HP 0.064 vs 0.015),
   act1 mean floor 4.22 vs 3.35 — with zero illegal actions and zero
   unclassified dead ends for both. PPO may now initialize from
   `models/bc_pretrain_r3/pretrained_actor.zip`.
4. **New reward components + floor6 ablation.** `training/v2_run_wrapper.py`
   implements `first_floor_advance_reward` (once per floor>1 advance),
   `boundary_success_reward` (once per floor>1 boundary node), `step_cost`
   (ongoing steps only), all logged separately in info and validated
   non-negative; metrics schema v3 adds `boundary_hits`,
   `boundary_wilson_95_low/high`, `mean_final_hp_fraction`,
   `final_hp_fraction` per episode. The floor6 promotion gate now requires
   **point >= 0.93 AND Wilson 95% low >= 0.90** (Wilson binds at small n:
   n=500 needs 465/500). The ablation (3 arms × 3 PPO seeds × 250k steps,
   warm-started from the promoted floor3 checkpoint, identical training
   episode seeds across arms, joint metric on the fixed 100 floor6
   checkpoint-partition seeds, lexicographic selection
   boundary→Wilson→floor→HP→steps — **never mean_return**) is implemented in
   `scripts/run_floor6_ablation.py` + `scripts/summarize_floor6_ablation.py`
   and was launched with early readings at 62.5k steps: boundary A 0.89 /
   B 0.91 / C 0.89.
5. **Teacher v3 (beam search + long rollouts).** `training/teacher_v3.py`:
   combat decisions scored by per-candidate beam search
   (`BeamSearchConfig`, beam 64–256, horizon = labelled turn + 1–2 enemy
   rounds; each end-turn transition folds one enemy round; terminal lines
   rank by win/loss bonus, HP potential bounded ≤ 1.0), out-of-run decisions
   scored by long rollouts stopped at the next combat end / curriculum
   boundary / terminal / step cap, averaged over multiple seeded
   continuations. Records keep the v2 contract (replayable prefix, capture +
   re-verify hashes, `score_gap`) with `record_version: 3` and a `teacher`
   provenance block. `scripts/evaluate_teacher_strength.py` is the
   expert-label gate: teacher vs heuristic/BC/PPO on independent seeds
   (default 1.55e9+, reserved-holdout-safe, evaluation-only) with the joint
   wins/floor/HP/steps gate; records generated by
   `scripts/generate_teacher_batch_v3.py` carry
   `label_status: "candidate"` until that gate passes.
   `scripts/generate_teacher_batch_v3.py` also enforces the freeze guard
   (`assert_seeds_outside_frozen_lineages`): the frozen R2 ranges and —
   unless explicitly overridden — the reserved holdout range refuse seeds.
   The batch0/1/dagger0 datasets remain frozen and unexpanded.
6. **Docs**: this section; `docs/V2_TRAINING_PLAN.md` marks the 0.603 number
   historical (leakage-contaminated).

Test suite: **147 + 18 teacher-v3/freeze tests green** under the training
venv (two-phase `scripts/test.ps1`: pure-Python contract modules + torch
modules).

## Teacher v3 strength gate + PPO comparison (2026-09-02, session 3 follow-up)

- **Teacher v3 strength gate executed** (`runs/teacher_v3/strength-*.json`,
  independent seeds 1,550,000,000+, floor6-capped, bounded teacher beam 64 /
  256 expansions per root / ≤6 combat decisions per episode). The harness was
  fixed first so raw-env policies mirror the V2 wrapper's floor-cap
  truncation (the raw `Sts2RunEnv` plays past `max_floors` and would die with
  hp 0 otherwise, making boundary/HP incomparable to the flat stack) and the
  teacher's beam-decision budget is per episode, not global.
  **Verdict: `expert_labels_approved: false`** — on n=4 the bounded teacher
  ties heuristic on boundary (0.50) but loses on floor (5.25 vs 5.50), and
  loses to the promoted floor6 PPO on every joint metric (boundary 0.50 vs
  0.75, HP 0.172 vs 0.406). The whole gate made only 6 real beam decisions
  (most combat states are forced moves or the run dies early), so this is a
  *bounded-teacher* NO, not a final expert verdict. **Teacher v3 records
  therefore stay `label_status: "candidate"`** (the generator already refuses
  expert labels without a passing report). A meaningful YES would need a
  fuller beam budget and more seeds. Note: the `mean_steps` check is
  misleading against bc (bc dies earlier → shorter episodes → "fewer steps"
  rewards dying fast); the joint signal should be boundary/floor/HP first.
- **Pretrained-PPO vs ordinary-PPO, fixed seed 91001, floor6, 250k steps**
  (`runs/ppo_compare/ppo-pretrain-vs-vanilla/`), joint eval on the same 100
  floor6 checkpoint-partition seeds:

  | arm | init | boundary | Wilson | floor | HP | steps |
  |-----|------|----------|--------|-------|-----|-------|
  | warm | floor3 ckpt ([64,64]) | **0.88** | **0.802** | **5.88** | **0.434** | 64.1 |
  | vanilla | random ([256,256]) | 0.87 | 0.790 | 5.86 | 0.391 | 64.3 |
  | pretrained | actor ([256,256]) | 0.83 | 0.745 | 5.80 | 0.384 | 61.8 |

  Ramps: warm flat at 0.89; vanilla 0.77→0.89; pretrained 0.45→0.80 (slowest).
  **The pretrained actor init was the worst of the three** — below random-init
  vanilla on every joint metric. Plausible cause: the actor encodes the weak
  BC student's floor3-era prior (it only reached floor6 boundary 0.22
  standalone), so PPO spends the budget un-learning it, while random init
  explores freely and the shaped reward/boundary structure is learnable fast.
  Caveats: single PPO seed, 250k budget, n=100 eval (the pretrained-vs-vanilla
  Wilson gap ~0.05 is within noise at n=100). This is a small-scale screening
  signal against the pretrain-as-PPO-init path, not a final verdict; all arms
  are contract-clean (0 illegal, 0 unclassified dead ends), and mean_return is
  confirmed non-discriminative (pretrained −1.42 / vanilla −0.24 / warm +0.11).

## V2 contract + curriculum integration (2026-09-01, session 2)

The plan's near-term delivery point — complete observation + V2 curriculum
integration, then the first search-teacher batch — is now implemented, tested,
and smoke-run against the real emulator. All of it lives in this repo; the
locked emulator build was **not** modified (native hash
`bcd623ce…34bc4` unchanged), and no V1 artifact under `runs/curriculum` was
touched.

- **Expanded observation (`training/v2_observation.py`, schema v1, 1739 ints)**:
  native combat/run passthrough plus fixed, versioned blocks for the full deck
  with upgrade status, relic presence, potion slots, all seven shop cards and
  all fourteen shop prices, map candidate coordinates, Neow/reward lists,
  phase/node one-hots, alive-enemy count, and hand single-target flags. The
  layout is pinned by `observation_contract()`, whose SHA-256 is recorded in
  every V2 metric file. Two gaps are recorded rather than papered over: relic
  counters and enemy definition identities are **not exposed by any native
  API in run-API v8**; unknown ids are counted, never collided.
- **(action, target) is now the training action space
  (`training/v2_flat_env.py`)**: `Discrete(225)` = 32 base actions ×
  (no-target + 6 enemies) + 1 empty-mask sentinel. Per-enemy aliases exist
  only where they change the transition — single-target cards (static table
  generated from `CardEffects.cs` by
  `scripts/regenerate_card_targeting_table.py`: 158 single-target / 20
  excluded AoE, drift-checkable via `--check`) while ≥2 enemies live; AoE,
  end-turn, and every non-combat action keep exactly one candidate (codec
  no-alias contract, asserted against the flat mask in tests).
- **Contract defects fixed at the layer boundary
  (`training/v2_native_env.py` + wrapper stack)**: the native
  `Sts2Run_Step` status is now surfaced, so a mask-legal-but-native-rejected
  action (the `event_id=31` / shop-mask class) becomes a **one-step, zero-
  reward, labelled `native_rejection` truncation** instead of V1's silent
  -1-reward 1200-step spin; all-zero masks remain the labelled
  `empty_action_mask` sentinel truncation; and `max_floor` is enforced
  solely by the V2 wrapper (the native `max_floors` knob stays unused, as it
  demonstrably never fired in V1).
- **Attribution metrics (schema v2)**: `boundary_rate` (curriculum stage
  completion), `defect_truncation_rate` (truncations excluding successful
  boundary hits — the V1 gate semantics stay intact), per-reason
  `dead_end_reasons`, and `unclassified_dead_ends`, which the promotion gate
  requires to be **0**.
- **Independent V2 curriculum (`training/v2_curriculum.py`,
  `config/training_v2.toml`)**: floor 3 → 6 → 10 → 13 → Act 1 complete.
  Twenty seed partitions (train/checkpoint/promotion/final per stage) are
  pairwise disjoint across all stages (validated at load; `final` is never
  read by the trainer). Intermediate stages promote on boundary rate
  (0.90/0.80/0.70/0.60), the terminal Act 1 stage on the plan's 35% true-win /
  Wilson ≥31%. Output goes to `runs/curriculum_v2/`, metrics are scoped
  `simulator_act1`, and V1 artifacts are structurally unreachable.
- **Real-emulator smoke run passed** (`config/training_v2_smoke.toml` →
  `runs/curriculum_v2_smoke/v2curriculum-20260901T050230Z`): floor3 → floor6
  with checkpointing, evaluation, cross-stage model load, and promotion
  plumbing; 20-seed floor-3 checkpoint eval: boundary 0.95, mean floor 2.95,
  illegal 0, unclassified dead ends 0, one live `native_rejection` correctly
  classified and counted as a defect truncation.
- **Teacher batch pipeline (`training/teacher_batch.py`,
  `scripts/generate_teacher_batch.py`)**: behavioural traversal over the real
  emulator, `is_interesting` selection (elite/boss, low HP, multi-target,
  every run-level non-combat decision), full root-candidate scoring via the
  prefix-replay teacher, streaming JSONL + manifest with emulator hash,
  search budget, score gap, and capture/re-verify replay hashes. Records are
  schema v2 and embed the *replayable prefix* itself, so any later process
  can rebuild the exact state (`training/teacher_bc_dataset.py` does this to
  emit BC samples with verified hashes, refusing tampered records).
- **Teacher batch 0 (regenerated, triple-audited: DONE)** — the first
  generation completed at 9,036 records with a clean *offline* audit and 80/80
  live replay, **but** the BC materializer caught a deeper defect the audit
  could not see: batch-era `score_candidates` rollouts added the raw engine's
  silent-rejection `-1` to candidate scores, so labels were computed under
  semantics that differ from the contract stack.  A targeted rescore audit
  (120 sampled records re-scored under the corrected rollout semantics)
  flipped the best action in **11/120 (9.2%)** — the contaminated batch was
  quarantined (`data/teacher/batch0_pre-rejection-fix/README-WARNING.txt`,
  never to be trained on), the traversal and scorer were fixed (rejections can
  no longer enter a prefix or a rollout; `rejection_mode="noop"` verified
  200/200 zero-drift state replays of old records).
  **Regenerated batch 0: 8,733 records** (4,500 seeds × 10 shards from
  1,400,100,000, outside every V1/V2 partition), triple audit all clean:
  offline provenance **0 violations**, **60-record live replay 0 failures**,
  **150-record rescore 0 best-action flips** — labels now provably match the
  training contract semantics.  Phase balance: combat 4,914 / reward 1,598 /
  Neow 682 / route 504 / event 450 / card-reward 427 / shop 133 / rest 17 /
  transform 8; 14.7% explicit enemy-target decisions; gap median 1.50,
  max 5.99 (the pre-fix 25.4 outlier class — rejection-score artifacts — is
  gone).  A 60-record contract-stack materialization run emitted 60/60
  hash-verified BC samples.  Expansion to 50k–200k is now purely a budget
  decision.  Method lesson recorded: *state* replayability is necessary but
  not sufficient — label *semantics* must match the training contract, and
  the converter is the tripwire.
- **Tests: 126/126** (was 78): +27 contract tests, +8 teacher tests,
  +3 BC-converter tests, +5 V2-BC-trainer tests, +3 DAgger tests, plus the
  metrics regression for mixed win/boundary accounting. The metrics schema
  assertion moved 1 → 2 with the new fields.
- **BC distillation (plan step 4) — first cycle complete.** All 8,733 batch-0
  records materialized through the *real contract stack* with per-state hash
  verification (8,733/8,733 zero drift) into samples carrying the exact
  1739-int expanded observation, the flat legal mask, and the teacher label
  (`training/teacher_bc_dataset.py`). The phase-split masked scorer
  (`training/behavior_clone_v2.py`, combat vs non-combat heads, deterministic
  prefix-hash holdout split) reached **holdout top-1 0.560 / top-3 0.782**
  (combat 0.513, non-combat 0.612) with checkpoint hash sidecar + tamper
  detection. Evaluated through the standard V2 harness
  (`scripts/evaluate_bc_student.py`) on untouched floor3 checkpoint seeds:
  **boundary 0.80, illegal 0, unclassified dead ends 0**; floor6 boundary
  0.12 and Act 1 mean floor 2.92 confirm the plan's expectation that pure
  imitation plateaus early — the curriculum/RL ladder and DAgger corrections
  are the binding next steps, not more BC.
- **DAgger (plan step 4, second half) — module complete.**
  `training/dagger_batch.py`: student-driven rollouts on the contract stack
  (noop-rejection mode) label states that are danger-relevant *or* where the
  BC margin says the student is unsure; engine-rejected actions are excluded
  from prefixes (the batch-0 rule) while the disagreement state is still
  labelled; records add `student` provenance + `teacher_agreement`. A real
  6-seed probe emitted 8 records with **4 teacher/student disagreements**,
  triple-audited clean (0 violations, 8/8 live replay). Formal DAgger batches
  run after batch-1 quality review.
- **Mask-vs-execution contract upgrade (plan step 1, final item).** The
  floor6 checkpoint's `defect_truncation_rate=0.44` turned out to be an
  *evaluation-protocol* amplifier, not 44% dead episodes: wide event masks
  offer options the engine's `StepEvent` rejects, and single-greedy-pick
  evaluation died on the first disagreement (diagnostic traversals with
  retry: 0/30 episodes actually trapped). The contract now **filters**
  rejected actions per state (monotone; the engine is deterministic), so the
  policy re-decides among honoured actions and cannot re-reject; only
  `rejected_to_exhaustion` or a truly empty engine mask truncates, and
  absorbed rejections are counted separately (`rejection_events`). Effect
  measured: student floor6 defect **0.44 → 0.00**, boundary 0.12 → 0.16,
  reruns byte-identical. floor3's promotion (zero rejections observed) is
  unaffected by the upgrade; floor6 restarted under filter semantics
  (pre-filter attempt archived as `aborted-pre-filter-floor6-095635Z`).
  Suite 129/129.
- **floor6 under filter semantics: checkpoint boundary 0.87 → PROMOTED at the
  1M probe (0.86/500 fresh promotion seeds)**
  (`v2curriculum-20260901T115142Z`, warm-started from floor3's promoted
  checkpoint): defect truncation **0.0** (was 0.44 under truncate semantics),
  illegal 0, unclassified dead ends 0, mean floor 5.85. Hash chain verified
  (file == metrics == decision, 100-seed checkpoint split ≠ 500-seed
  promotion split). Early-stop fired exactly as designed at 983k steps of a
  4M budget.
  **Gate raised 0.80 → 0.90 for subsequent ladder runs** from this evidence:
  a 6-point margin at 25% of budget is thin, and floor6's boundary quality
  bootstraps the harder stages. The accepted promotion stands (it satisfied
  the gate in force when it ran); floor10 now runs from this checkpoint with
  the raised standard recorded in config. If floor10 stalls, the documented
  fallback is a fresh floor6 continuation targeting ≥0.90, not extra budget
  on a different stage.
- **floor10 stage running** (8M budget, boundary-0.70 gate, probes every 2M)
  from the promoted floor6 checkpoint — the third warm-start chain link
  (floor3→floor6→floor10) exercising `--initial-checkpoint` on official seeds.
- **Official V2 curriculum: floor3 PROMOTED (first stage-level gate passed
  end to end).** `runs/curriculum_v2/v2curriculum-20260901T091856Z/floor3`:
  the 500k probe evaluated the promotion partition (500 untouched seeds) and
  the policy reached the floor-3 boundary in **100% of episodes** with
  **illegal 0, unclassified dead ends 0, defect truncations 0**, mean final
  floor exactly 3.0 — promotion recorded with checkpoint
  `step_000000500016.zip` and a hash chain verified file == metrics ==
  decision (69c6c32d…). Training stopped early exactly as designed
  (early-promotion). The stage's promotion evidence is the first artifact in
  this project produced under the corrected contract (target encoding,
  expanded observation, boundary semantics) rather than the retired V1 stack.
  floor6 now runs warm-started from that checkpoint (`--initial-checkpoint`,
  exercising the cross-stage load path on official seeds).
  A probe-naming collision found during review was fixed (sub-1M probe
  intervals shared one "M"-rounded metric stem); regression test added.
- **DAgger batch 0 complete (interrupted-but-audited): 3,824 records** over
  1,157 student seeds (the generator was killed by an environment-wide
  process termination before its manifest; all streamed lines are
  capture-time replay-verified). Triple audit passed: 0 provenance
  violations, 50/50 live replay, 60/60 rescore zero flips; **2,000
  teacher-student disagreements** — direct proof the student visits states
  the teacher corrects. Manifest flags completeness `partial` with the
  surviving dataset hash.
- **Teacher batch 1 COMPLETE and triple-audited: 35,268 records** (18,000
  seeds × 10 shards from 1,400,200,000). Offline provenance **0 violations**,
  **60/60 live replay zero failures**, **150/150 rescore zero best-action
  flips** — all three shards-complete manifests report capture==reverify.
  Phase balance combat 19,892 / relic-reward 6,521 / Neow 2,600 / route
  2,111 / event 1,780 / card-reward 1,723 / shop 513 / rest 85 / transform
  43; 15.8% explicit enemy-target decisions; gap median 1.50, max 7.02.
  **Cumulative audited teacher dataset: 44,001 records** (batch 0 8,733 +
  batch 1 35,268), far past the ~10k first-batch goal and into the planned
  50k–200k expansion range with the quality bar held. Batch-1 BC
  materialization completed: 35,268/35,268 samples with per-record replay
  verification (0 drift).
- **Merged corpus + BC round 2.** `combined_bc_r2.jsonl` = **47,297
  samples** (43,509 teacher + 3,788 DAgger; 528 cross-source duplicate
  states resolved by the confidence rule with **0 label conflicts** — an
  independent determinism check on shared states). Round-2 trainer: holdout
  top-1 **0.603** (was 0.560), top-3 0.819 on a 2,359-sample holdout
  (`models/bc_v2_r2.pt` + metadata sidecar). Harness student eval (50
  seeds/stage): floor3 boundary 0.76, floor6 0.14, floor10 0.00 — illegal
  0, unclassified 0 everywhere. Imitation improves with corpus size but the
  traversal distribution (most teacher runs die by floor 5–8) keeps
  high-floor BC weak by construction; the plan's "BC plateaus,
  curriculum/DAgger carries" line holds.
- **floor10 RL finding (step-5 ablation signal, contract-clean).** The
  official floor10 stage degraded: checkpoint boundary 0.26 (1M) → 0.22 →
  0.18 → 0.15 (4M) against a pre-training floor6-chain baseline of 0.20
  (measured post-hoc), with mean return drifting more negative while
  truncations (deaths) *fell* — the flat PPO policy learned to survive
  without advancing, i.e. potential-reward exploitation. The run was
  stopped (also caught by the environment-wide kill) and its 8M budget was
  NOT resumed on this policy; metrics remain hash-chained at
  `runs/curriculum_v2/v2curriculum-20260901T122903Z/floor10` for the
  ablation. Contract quality was never implicated: defect 0.0, illegal 0,
  unclassified 0 at every checkpoint. Next-step direction this evidence
  supports: terminal-dominant reward tuning (larger win/advance bonus or
  floor-potential discount) and/or PPO fine-tuning warm-started from the
  round-2 BC policy instead of the previous RL checkpoint.

- **Known contract gaps (tracked, not blockers):** relic counters and enemy
  DefIds are absent from native API v8 (relic *presence* and enemy
  HP/intent/block/buffs are covered); event option *text* is only inferable
  from event id + mask (id known, options enumerable, labels deferred to the
  live-adapter phase).

## Monitoring notes (unattended watch, 2026-09-01)

- **V2 foundation implemented after the stop decision**: independent modules
  now cover the normalized run contract/reward/floor curriculum
  (`training/v2_run_wrapper.py`), stable `(action,target)` candidate encoding
  (`advisor_core/action_codec_v2.py`), and deterministic seed+prefix replay
  candidate scoring (`training/prefix_replay_teacher.py`). Prefix replay was
  smoke-tested against the actual local Sts2RunEnv and emits only
  `simulator_act1`-scoped results with a real-A10 disclaimer. Full suite:
  **78/78 tests pass**. No new large training or experimental full-run process
  was started; the next step is integration plus a bounded teacher-data
  ablation, not another blind PPO budget.

- **Flat Act1 PPO baseline intentionally stopped at 16M** (09:57 local):
  eight identical-split checkpoint evaluations from 2M through 16M originally
  appeared to remain in a 2–5% simulator win band. A subsequent contract audit
  found that `player_won` is the previous *combat* result and stays true on
  later map/shop states. Re-evaluation requiring terminal run completion gives
  only 0–1% per checkpoint and **0/500 true wins** at the 10M promotion probe
  (the original 10/500 were all stale combat-win flags). Continuing the
  same collapsed policy to 100M was no longer a useful comparison for the
  user's delivery target, so the last complete checkpoint was preserved and
  the run was stopped before spending the remaining ~84M steps. The exact
  evidence and claim boundary are in `act1/plateau-stop-decision.json`.
  A self-healing wrapper restarted it once at 09:54; that recovery was also
  stopped before a new checkpoint and marked `plateau_stopped`. No Act1
  training process remains. The next run must use expert demonstrations or a
  hierarchical/shaped curriculum; experimental `full_run` remains disabled.
- **Empty-mask accounting fixed**: deterministic replay of the 16M checkpoint
  again isolated seed 20000039 at floor 3 / map phase with zero legal actions.
  Evaluation now counts this as an environment truncation rather than a policy
  illegal action, and training converts the dead-end to one synthetic terminal
  transition instead of allowing 1,200 rejected actions. Regression test
  added; the full 54-test suite passes. The same fix also requires terminal
  completion before counting a run win: corrected 16M result is 1/100, not
  2/100. These changes only make attribution honest; they do not improve play.
- **Version metadata corrected**: `game_branch` now records the actual locked
  target `public-beta-v0.111.0`, not the stale `main` label. Historical build
  `24724944/public-beta-v0.111.0/222455745` remains unchanged.
- **Second-resume step accounting fixed**: post-resume SB3 archives retain the
  current process's timestep delta, not the global total. The 16M filename was
  authoritative while its internal counter was 5,999,988. The automatic
  restart briefly emitted an unchanged `step_000006000000` alias before it was
  stopped; that checkpoint and metric were moved to
  `act1/auto-restart-artifacts/` and excluded from the official series.
  Future resumes derive the global base from `step_<global>.zip`, with a
  regression test, so a second recovery cannot rewind numbering or budget.
- **Metric compatibility note**: checkpoint/probe win percentages in older
  bullets below are preserved as incident history but use the retired stale
  `player_won` interpretation. The authoritative corrected files end in
  `-reevaluated-run-win-fix.json`; use their 0–1% checkpoint and 0/500
  promotion results for every current decision.

- **14M checkpoint** (09:12, resumed-run numbering `step_000014000004` ✓):
  win 0.030, floor 7.72, truncation 0.020, **illegal_actions 0 — first time**.
  On the identical 100 checkpoint seeds, the deterministic policy no longer
  picks an action outside the mask (the seed-20000039 NODE_SHOP empty-mask
  episode is gone — either route or action preference shifted enough to
  dodge it; it could reappear on later checkpoints, so track rather than
  declare victory). Truncation is down from the 0.05 baseline too: the two
  mask defects no longer automatically block the ≤0.03 + illegal=0 gates.
  Win rate remains the binding gate (plateau 2–5%).

- **Resume numbering verified in production**: first post-crash checkpoint
  landed as `step_000012000000` — global-step alignment across the
  resume-base (10,000,020) + learned delta worked exactly as designed.
  12M metrics: win 0.020 (100 eps), floor 7.97, **truncation 0.020 ↓** (the
  event-loop trap episodes are declining as the policy learns to prefer
  options 1–3), illegal 1 (NODE_SHOP persists), mean 114 steps. Win rate
  stays in the 2–5% plateau band; per the documented decision the run
  completes to 100M as the comparison baseline before any strategy change.

- **Native emulator FailFast at 07:32 (11.45M/100M steps)**: access violation
  inside C# `RunMapGenerator.FindAllPaths` during a training `run_reset` —
  uncatchable by Python, process killed by the OS. Zero data loss (five
  checkpoints + six metrics intact; 10M checkpoint verified loadable).
  Countermeasure: curriculum now supports `--resume-run` (globally aligned
  checkpoint/probe numbering via verified SB3 load/learn semantics,
  regression-tested) and `scripts/run_act1_self_healing.ps1` resumes the
  same run dir on native-crash exits only, with crash-storm guard. Live:
  job `pwsh-27` resumed from `step_000010000020.zip` at 08:18 (py-spy shows
  `resume_base_steps: 10000020`). Full post-mortem:
  [NATIVE_CRASH_2026-09-01.md](NATIVE_CRASH_2026-09-01.md). Third distinct
  emulator defect for the parity/upstream report (after the two mask bugs).
- **10M promotion probe executed and FAILED as expected** (07:12, first live
  run of the new `_probe_promotion` code): 500 promotion seeds → **10 wins
  (2.0%, Wilson low 1.01%)**, floor 7.55, truncation 0.020, illegal 1.
  Gate result: fails win-rate/Wilson/floor (by design a failed probe keeps
  only its metrics JSON and training continues; `promotion_decision.json`
  appears on pass or at stage end). The probe's
  seed_sha256 matches the combat stage's promotion draw (same 30M range),
  confirming partition plumbing. 10M checkpoint eval: win 0.040, floor 7.82.
- **Plateau confirmed on a 500-episode sample**: win stuck 2–5%, floor
  oscillating 7.2–8.0 of 16 across 2M→10M; policy entropy collapsed from
  1.26 to ~0.28 nats (near-deterministic). This matches the published
  sts2-rl-agent pattern (92% combat but ~0% full runs) and the architecture
  doc's expectation that the flat MaskablePPO baseline is the *comparison*
  baseline, not the route to A10.
  Earlier decision was to let this run finish to its 100M budget as a baseline.
  The unchanged 12M, 14M, and 16M results supplied enough additional evidence
  to retire that plan; see the 16M stop decision above.
- **8M checkpoint metrics** (06:44:10, written ~21 min after the supervisor
  death and after adoption — confirming adoption lost nothing): win 0.030
  (4th straight in the 3–5% band), mean floor **7.54** (down from 7.97;
  floor now oscillating 7.2–8.0, not yet breaking toward the 16-floor end),
  truncation/illegal unchanged at the emulator-defect baseline (0.030 / 1).
  Plateau diagnosis to watch at the 10M probe (~07:15, first
  `early-promotion-10m.json`): if win rate is still ~3%, the simplified-run
  curriculum may need reward shaping before more compute — a
  training-strategy call, not an infra one.
- **Correction of same-night misread**: right after the supervisor died I
  briefly inferred from timing that the 8M cycle and a 10M probe had already
  completed; the metrics directory showed the 8M eval had been written at
  06:44 (post-adoption) and the probe fires at 10M steps, still ahead. No
  artifacts were lost; watcher jobs plus the metrics directory remain the
  source of truth.
- **Supervisor death, zero training loss (06:23)**: the act1 supervisor exited
  while rewriting `heartbeat.json` — a monitoring `Get-Content` held the file
  open and Windows `os.replace` raised a sharing violation. The training
  child (pid 64704) was healthy and kept advancing (6.6M steps at takeover);
  it was ADOPTED by a managed watchdog (`pwsh-23`: trainer-exit,
  stage-settled, and >55 min metrics-stall detection; existence-only polling
  so the watcher cannot re-create the collision). The manifest records
  `running_unsupervised` with the full audit. Hardened after the fact:
  `training/supervisor.py` and `training/metrics.py` atomic JSON writers now
  retry through reader locks, and a heartbeat failure can never kill
  supervision (regression test added; 51 core tests green).
- **6M checkpoint metrics** (06:17): win 0.030 (same seeds as 4M, so 4M→6M is
  an apples-to-apples tie), mean floor **7.97 ↑** (7.24→7.71→7.97), truncation
  stable 0.030, illegal stable 1 (the NODE_SHOP episode). Floor is grinding
  up while win rate lags on the 16-floor simplified run — expected shape at
  6% of budget; the binding gates remain win-rate/Wilson.
- **Measured cycle cost (corrected)**: checkpoint every 2M steps at ~1180 fps
  plus a ~25 s evaluation ⇒ a full 50-cycle stage ≈ 24–25 h wall clock;
  25 promotion probes (~2 min each) add ~50 min. ETA in the reports (~22 h)
  is now consistent with the 09-02 morning finish; the earlier "~28h" note
  was pessimistic.
- **Promotion-probe math for the 10M checkpoint** (n=500): Wilson ≥0.31 is the
  binding constraint — needs **≥180 wins (36%)**, above the nominal 35%
  win-rate line; plus mean floor ≥10, truncation ≤3%, illegal 0. From 3–5%
  at 4M this is unlikely to pass at 10M; the probes are cheap now (~2 min
  per 500 episodes post-fix), so the cost of waiting is only the 25 probes ×
  ~2 min over the stage. Stage wall clock unchanged (~23h ETA at 1172 fps).
- **Guard validated end to end**: the guarded run's first checkpoint
  evaluation (100 episodes, checkpoint seeds) completed in ~21 s
  (05:23:26 save → 05:23:47 metrics) versus the aborted run's multi-hour
  hang. Mean 141 steps/episode at 2M steps.
- **First act1 checkpoint metrics (2M steps, `curriculum-20260831T205351Z`)**:
  win_rate 0.050 (Wilson 95% 0.021–0.112), mean floor 7.24, truncation_rate
  0.050, illegal_actions 1, mean 141 steps. Promotion gate needs win≥0.35,
  Wilson-low≥0.31, floor≥10, truncation≤0.03, illegal=0 — far from passing,
  as expected at 2% of the 100M budget. Watch: truncation 5% currently
  exceeds the 3% cap; part of it is the `event_id=31` mask/native bug under
  deterministic eval (PPO receives -1/step pressure there and may learn to
  escape; if truncation plateaus, treat as an emulator-parity item, not a
  policy-quality verdict).
- **illegal_actions=1 root-caused the same night** (`scripts/
  find_illegal_episode.py`): seed 20000039 at `phase=map`,
  `current_node_type=NODE_SHOP` exposes an **all-zero action mask** — a
  second emulator mask defect (empty-mask soft-lock, vs `event_id=31`'s
  too-wide mask), documented in EVAL_HANG_2026-09-01.md.
- **2M truncation attribution (scripts/attribute_truncations.py)**: all 5
  truncated episodes are emulator mask defects, not policy weakness — four
  deterministic `native-rejection-loop` stalls in the event phase (floors
  5/6/8/9: seeds 20000043/48/56/67) plus the `NODE_SHOP` empty-mask state
  (seed 20000039). So `truncation_rate=0.05` at 2M is 100% bug-attributable;
  the ≤0.03 promotion cap is currently blocked by the emulator, not the
  policy. Training rollouts receive −1/step pressure at these states, so the
  policy may learn alternatives — recheck this ratio at the 10M probe.
- **4M checkpoint metrics** (05:51): win 0.030 (3/100; vs 0.050 at 2M — noise
  band, Wilson intervals overlap), mean floor **7.71 ↑** (from 7.24), mean
  episode 122 steps ↓, truncation **0.030 ↓** (exactly at the ≤0.03 cap; the
  event-phase native loops dropped from 4 to ~2 episodes — the −1/step
  pressure is teaching avoidance), illegal_actions still 1 (the NODE_SHOP
  empty-mask episode at seed 20000039 is unavoidable *once entered*: every
  action is illegal there, so only route-learning away from that node can
  clear it — watch whether later checkpoints drop it).
- **Cross-process replay determinism flag**: the fresh-process replay of the
  same checkpoint+seeds reproduced the 5 truncations exactly but showed
  0 wins / 95 deaths where the in-training evaluation recorded 5 wins.
  `scripts/probe_determinism.py` then verified the engine is fully
  deterministic **across fresh processes** (same seed → same trajectory hash,
  repeated twice in one process and in two processes). The remaining
  discrepancy is therefore isolated to the long-lived trainer process, where
  12 vectorized training env handles coexist with evaluation envs — the
  likely culprit is process-global state in the native C# engine shared
  across Sts2RunEnv handles. In-training protocol stays canonical for gating
  (identical to the combat stage that promoted 499/500); parity-phase item
  updated accordingly: audit native run-handle isolation.
- **Incident resolved**: the act1 run `20260831T163117Z` was **aborted** — its
  first checkpoint evaluation hit an infinite loop (native mask disagreement
  on seed 20000043, 9.17M steps in one episode). Full root-cause analysis and
  fix in [EVAL_HANG_2026-09-01.md](EVAL_HANG_2026-09-01.md); py-spy `--locals`
  evidence archived at `docs/forensics/eval_hang_py-spy_2026-09-01.txt`.
  Evaluation now enforces its own per-episode step cap (regression-tested),
  and verification on the real hang seed completes in 5.5 s.
- act1 relaunched as supervised run `20260831T205351Z` with guards (managed
  job `pwsh-20`). With evaluations now costing ~1 min instead of hours, the
  stage wall clock projects back to the ~28 h plan (50 × ~34 min).
- Replica diagnostics learned: single-process evaluation at this checkpoint
  runs ~90 steps/episode at ~4.6 ms/step GPU; the earlier "evaluations are
  structurally 60–100 minutes" numbers were hang artifacts.

## Completed

- surveyed official mod support, state bridges, simulators, action loggers,
  datasets, overlays and prior RL/LLM agents;
- selected MIT STS2MCP + read-only overlay + MIT NativeAOT simulator route;
- isolated the project from unrelated workspace projects;
- installed project-local uv 0.12.7, CPython 3.12.14 and .NET SDK 9.0.317 on G:;
- installed the simulator's locked Python dependencies;
- replaced the CPU PyTorch wheel with the official CUDA 13.0 build and verified
  the RTX 4070 is visible;
- fixed a local NativeAOT linker failure caused by an ampersand-containing
  third-party PATH entry and built `Sts2Emulator.dll`;
- passed the Gymnasium environment check;
- trained and saved CPU (5,120 steps) and CUDA (6,144 steps, 12 environments,
  about 839 FPS) MaskablePPO full-run smoke checkpoints; both are integration
  artifacts, not candidate A10 policies;
- measured the current baselines: the narrow starter encounter heuristic won
  100/100, while the complete-run `first-valid` policy won 0/20. The 100% value
  is not evidence for the A10 target.
- added legal-action enumeration, structured Chinese explanation, a GET-only
  live loop, a training supervisor, Wilson statistics and unit tests.
- compiled STS2MCP v0.4.0 against the installed public-beta after a small
  backwards-compatible multiplayer-lobby reflection fix, installed it, and
  verified the localhost health endpoint and main-menu single-player state;
- verified the local advisor performs GET-only polling and stays silent on a
  non-decision menu screen;
- captured version-locked Ironclad/A10 live traces covering Neow, card removal,
  map pathing and combat bash/defend/end-turn round trips; the whitelist probe
  confirmed evaluation mods are exactly `STS2_MCP`;
- promoted the combat curriculum stage at 2,000,016 steps: 499/500 wins
  (Wilson lower 98.9%), zero illegal actions, zero truncations, on unseen
  promotion seeds (`runs/curriculum/curriculum-20260831T140450Z`);
- the first act1 full-stage attempt (100M steps, run `20260831T142225Z`) lost
  its parent shell at ~958k steps and died before any checkpoint existed;
  its manifest is audited as `orphaned`. The stage was relaunched at
  2026-08-31T16:31Z as run `20260831T163117Z` under a managed background job;
- `training/curriculum.py` now supports `promotion_probe_every_steps`: a full
  promotion evaluation on unseen seeds runs periodically and stops the stage
  early on success. This restores code/Artifact provenance for the combat
  stage's pre-existing `early-promotion-decision.json` (previously emitted by
  an out-of-tree script) and prevents burning the remaining timestep budget.
- initialized the repository under git with a baseline snapshot commit;
- added `bridge/convert_traces.py`: raw recorder JSONL -> contract-valid
  decision traces with a deterministic visible-information filter (ordered
  draw/discard/exhaust piles collapse to public count + sorted composition),
  wire-exact legal-action ids verified against the STS2MCP action dispatch
  source, result attribution from POST response + next-state arrival, and
  hard gates for contamination, Ironclad-A10 verification and seed surrogates;
- added 19 screen-schema fixtures under `tests/fixtures/screens/` (6 captured
  from locked-build live traces: menu, monster, map, event, card_select, plus
  the Neow removal flow; 13 synthetic from the documented v0.4.0 protocol for
  elite/boss/hand_select/rewards/card_reward/rest_site/shop/fake_merchant/
  treasure/bundle_select/relic_select/crystal_sphere/game_over/overlay);
- end-to-end data-chain smoke passed: real clean traces -> converter ->
  8 contract-valid records -> `training.validate_traces` PASS ->
  `training.behavior_clone` trained with declining loss and full hash
  provenance (`models/bc_pipeline_smoke.pt`);
- full test suite green: 46 core tests + 3 behavior-clone tests.

## Not completed

- the target is now locked to the installed public-beta `v0.111.0`, Steam build
  `24724944`, commit `41cef1ea`, assembly hash `222455745`; Steam reports a
  newer build pending, so results must be invalidated if the game updates;
  (STATUS previously recorded build `24489008`; LIVE_BRIDGE and the version
  lock agree on `24724944`, which the live sessions confirm);
- no full real-game Ironclad/A10 run has been recorded end to end yet; the
  captured sessions are decision round trips, and none of them contains a
  `run_identity`/compendium event, so the converter currently falls back to
  `RUNID:` surrogate seeds (explicitly gated behind `--allow-seed-less`,
  never acceptable for the evaluation corpus);
- no parity traces (real game vs emulator, same seed) have been captured;
- only a smoke checkpoint has been trained; simulator coverage and
  reward/curriculum work must be extended before meaningful full-run training;
- the checked-in `training/curriculum.py` cannot itself emit the combat stage's
  `early-promotion-decision.json` / `promoted_early` manifest (it evaluates
  only at stage end), so that artifact predates the current file and the
  combat stage must be re-run to restore provenance;
- no checkpoint has passed A0, A5 or A10 full-run gates;
- the requested 50% real-game win rate has not been achieved or claimed.

## Immediate next gates

1. Start the game, then run `bridge.trace_controller start-ironclad-a10` and a
   continuous `record` session so the converter receives real seeds and
   `run_identity` (closes the seed-surrogate gap).
2. Play/record complete A10 runs (player-executed, or `--allow-actions` tagged
   `automated=true`), convert with `python -m bridge.convert_traces
   runs/live_traces --split ... --out data/local/...`, validate, and train the
   first real BC checkpoint.
3. Re-run the combat curriculum stage from the checked-in code to restore
   early-promotion provenance, and monitor the running act1 stage at its 2M
   checkpoint evaluations.
4. Capture fixed-seed real-game traces and begin simulator parity closure.
5. Only then consider the experimental `full_run` stage; A10 acceptance remains
   a real-game measurement.

## Second overnight phase: 2026-09-19 (session 6)

Read-only with respect to the game: no process started, no POST, no bridge or
Steam state touched. This phase settled what the simulator can and cannot answer
about the operator's three-act target, and found one failure class worth naming.

1. **"Act 1-3" is not expressible in the simulator, by construction.**
   `RunConstants.cs:35-36` enumerates exactly two acts; `RunMapGenerator.cs:9-11`
   picks one per seed; the only act-chaining branch
   (`RunEngine.cs:1907-1920`) is gated on hardcoded seed `7MS1YN8NWB` and chains
   Act 1 -> Act 2 only. So no simulator artifact, however clean, can satisfy the
   literal target, and the goal stays open. Details and citations:
   `docs/ACT1_CAMPAIGN_2026-09-19.md`.
2. **The chained two-act flow is reachable from the existing stack** —
   `Sts2Run_Reset` takes a UTF-8 seed string (`native.py:402`), so
   `training/v2_native_env.py:62`'s `str(seed)` accepts `"7MS1YN8NWB"` directly.
   New tool `scripts/probe_chained_act_flow.py` ran all 76 campaign act1
   checkpoints through it: **3 cleared the scripted Act 1 boss and entered Act 2**,
   0 illegal actions, deepest Act 2 floor 22 of a possible 33. Two of those three
   then stopped on an *environment-side* truncation while alive at 37/77 HP with
   identical Act 2 traces, so Act-2 depth on this seed measures the retained trace,
   not the policy. Combined-rate arithmetic (~0.04 x 0.013) puts one expected
   two-act sweep at ~2000 checkpoints; this is not a brute-force target.
   `scripts/audit_chained_terminal.py` then checked this path against the objective's own
   two gate clauses, on the 3 checkpoints that chain: `evaluate_policy` reports
   **0 illegal actions and 0 unclassified dead ends**, the two alive truncations carry
   `dead_end_reasons={empty_action_mask:1}`, and their terminal states advertise **zero**
   successor nodes at floor 19 (`docs/evidence/chained_terminal_gates_20260919.json`).
   That corrects a guess recorded here earlier: those two are *not* the boss-exit family --
   the chained Act-2 map simply runs out of nodes past row 18, whereas the boss-exit class
   dead-ends at floor 17 immediately after clearing a NodeBoss node. Same label, two
   mechanisms; and the route's own shape is now read out of source: for the demo seed Act 2 is a
   four-floor scripted strip (floors 18-21 only, `RunEngine.cs:1953-1979`, teleporting the cursor
   to column 3 rows 1-4), after which the run is handed to a freshly generated 17-row map whose
   boss row 16 sits at floor 33 -- so `terminalFloor = 33` is that map's real boss floor and a
   two-act clear is expressible in principle, just never walked (frontier floor 22 over 84
   checkpoints). What is still unexplained is only why the generated connectivity stops there.
3. **A third outcome exists at floor 17: boss stalemate.** One checkpoint spent
   60000 steps in the Act 1 boss fight without dying or winning — every action
   mask-legal, HP pinned at 6/77, flat reward, and **59930 distinct combat
   observations with no repeat**, so cycle detection cannot break it. Pooled over
   the campaign, 50 of 29000 act1-scope episodes (0.17%) truncate, and every file
   containing one has `max_final_floor = 17`, across all arm families. This is a
   second boss-side target beyond "the boss kills us".
4. **Six-arm Act-1-only ranking: 0/504 in every arm** on the shared
   `act1.promotion` partition (verified identical to each arm's own `plan.json`).
   At the measured Act 1 rate the expected win count for 504 seeds is 0.30, so this
   ranks nothing — the sizing requirement (~1700 Act-1 seeds per arm) is recorded in
   the campaign doc.
5. **Anti-misreading audit**: a repo-wide scan of all 274 metrics files found the
   highest act1-scope rates on the Aug-31 *V1* run (5/100). That run cannot be
   compared: different contract (V2 verified at `OBS_SIZE = 1739`, `Discrete(225)`),
   schema 1 records neither act nor raw seeds, and its own file carries
   `illegal_actions = 1`.

6. **The headline table's last unreproduced row is now reproduced**, so all five
   records in it are independently re-run (`a_base`, checkpoint @1M, 1/100:
   checkpoint hash, seed hash, `win_rate 0.01`, `illegal 0`, `unclassified 0`,
   `max_final_floor 17` all matched — `runtime/a_base_reeval.log`). The "four
   records" count in the session-5 section above is that moment's state and was
   left as written; the current count is five.
7. **The act-filtered A/B is running** (`runtime/act1_ab/`, arms `a1filt` /
   `a1mix`, both warm-started from `b_terminal-1` act1 step 4M over the same
   80,000-seed window). The premise is now runtime-measured rather than inferred:
   12 workers x 6 starts give 31 Act-1 / 41 Act-2 on the control and 72/72 Act-1
   on the filtered arm; the window's census holds 40,010 Act-1 seeds (50.01%).
   The decisive metric was fixed in advance to `mean_final_floor` over 504 shared
   Act-1 seeds, with win counts declared non-decisive.
   Their parent link is attestable, unlike the 2026-09-18 campaign arms: both
   `plan.json`'s `warm_start` block and `act1/origin.json` name the same parent
   checkpoint, and its SHA-256 recomputes from the file on disk
   (`docs/evidence/ladder_lineage_20260919.json`, claim `ladder_lineage`). Of the 13
   campaign arms, 0 have either mechanism -- they were launched before the launcher
   recorded a parent -- so "warm-start lineage is narrative" is true for the 09-18
   campaign and false for these two. Note what the attestation does *not* fix: the
   parent is another arm's act1 checkpoint, so floor13 still never ran and the
   floor6 -> act1 jump is still a jump. The one attested *stage->stage* edge in the
   repository is a two-rung smoke run (`runs/curriculum_v2_smoke/...233201Z`,
   floor6 <- floor3 with `initialized_from_stage` set and launcher-chained), which proves
   the mechanism works without showing any real ladder was walked -- its floor3 stage was
   itself seeded from a deeper promoted checkpoint.

Live side unchanged: `127.0.0.1:15526/health` refused at 23:42 with no game
process, so the three-act real-game batch remains an operator action
(`scripts/supervise_solver_batch.py --mode observational --allow-actions
--max-battles 50`); the supervisor still never starts the game.

## Governance conflict I created: new PPO budget against the 2026-09-02 freeze

Flagged by me, not by a check — I ran the work first and found the constraint
afterwards, which is the wrong order.

`docs/FREEZE_2026-09-02.md` freezes the "Combat PPO expansion (V2 curriculum
ladder, floor6 ablation, pretrain-init screening)" stream, with the explicit
non-goal "**No new PPO budgets** … during Phases A–B", and marker
`runs/curriculum_v2/FREEZE-PPO-EXPANSION.txt`. Its unfreeze conditions are
(1) the Combat Solver Phase B comparison verdict recorded, **and** (2) a written
decision that PPO is still the path, with the floor10 reward-exploitation fix
documented first.

As of 2026-09-19 02:19 UTC condition (1) is **not** met:
`docs/COMBAT_SOLVER.md` still states the formal fixed-seed Phase B is **BLOCKED**
(this bridge exposes `run.seed = null` and has no seed-injection endpoint), so the
freeze is in force.

**Condition (2) rests on a premise that measurement just contradicted.** It asks for
"the floor10 reward-exploitation fix documented first", i.e. that the policy learned
to survive without advancing. Dissecting one boss stalemate decision-by-decision
(`docs/ACT1_CAMPAIGN_2026-09-19.md`, "僵持的机制") found the opposite: the policy
passed the turn with a playable card in hand **zero** times out of 59,681 end_turns,
and end_turn was the *only* legal action in 99.995% of them. The boss in that
fight is Vantom (`EnemyAI.cs:259-263` deals it three unplayable Wounds into the
player's deck every fourth move), confirmed by `RunEngine.cs:459-471`, which gates a
retained-trace script on exactly this seed/floor/encounter/HP/gold. **That same script
hardcodes the fight's opening hand and a 10-card draw pile, so this instance cannot
carry a general claim about Vantom fights** -- it needs a non-demo-seed re-run. What it
does establish is the direction of the error: the policy was never refusing to act. Read literally,
condition (2) asks for a fix to a behaviour that is not happening. Two things do
remain worth addressing, and they are different. A persistent single-legal-action
combat state could be recognised early instead of running to the step cap -- though
note it is already counted, since act 1 has no `max_floor` every such truncation lands
in `defect_truncation_rate`, which the 3% promotion gate caps (verified by re-running
one such episode through `evaluate_policy`: `dead_end_reasons={'empty_action_mask': 1}`,
`defect_truncation_rate=1.0`, `unclassified_dead_ends=0`). So the gap is that the label
cannot distinguish "cannot act" from "acts too slowly", not that the gate misses it.
The second is trainable: kill before the deck saturates. Recorded for the decision,
**not acted on** -- the freeze still stands and no new PPO budget was spent.

A per-seed census then sharpened where that budget should go
(`docs/evidence/act1_arrivals_by_act_20260919.json`, 3,500 ordinary promotion seeds
of one checkpoint, run under the stage's own 1600-step cap): boss **arrival** rates
are the same for both acts (83 Act 1 vs 79 Act 2), but conversion is not -- the same
policy kills the Act 2 boss in 25/79 arrivals (31.6%, CI 22.5-42.6) and the Act 1
boss in **0/83** (CI 0-4.4%). So the deficit is the first act's boss fight itself,
not attrition before it, which is a different and much cheaper thing to aim at than
"deepen Act 1". Dissecting what happens inside those fights narrows it once more
(`docs/evidence/act1_boss_arrival_anatomy_20260919.json` plus
`act1_boss_win_anatomy_20260919.json`): damage per decision barely separates a lost Act 1
fight from a won Act 2 one (3.45 vs 3.79), so "deal more damage" is not the lever.
What separates them is surviving the boss node -- Act 2 winners last 66 boss decisions,
losers 37, and Act 1 arrivals only 22 against bosses of 183 to 324 HP. Act 1 winners enter the
boss at a median 80 HP versus 61 for losers, which looked like a second condition --
but that comparison used winners selected for winning, and testing it against the 83
losers' own records refuted it: 14 of 83 losers also entered at >=80 HP, so arrival
health does not predict victory. What it predicts is longevity (correlation 0.74;
high-HP losers last 31 boss decisions against 19 for low-HP ones), and winners need
33-63. So the model is one condition, not two: **outlast the boss fight**, with arrival
health as an upstream contributor. That is also why deepening Act 1 alone and raising
damage alone each failed to move the win rate. Checking the behaviour itself removed a
third candidate: plays per decision is the same to within 0.06 across lost act-1
fights, lost act-2 fights, won act-2 fights and the nine won act-1 fights
(0.727 / 0.730 / 0.667 / 0.686), so the policy does not play differently when it
wins. At roughly four decisions per player turn, and with Vantom averaging ~13
unmitigated damage per turn, act-1 losers absorb about 5.5 boss turns and winners
about 12.8. Measuring block at every decision then settled which lever it is
(`docs/evidence/act1_boss_block_economy_20260919.json`): **both groups enter the boss
with exactly zero block** (100% of winners, 96% of losers), so arrival defence is not
it -- the winners lose 1.24 HP-plus-block per boss decision against the losers'
2.00-2.18, about 1.6-1.76x slower, while doubling peak block. Going from 61 HP to 80
buys roughly 30% more decisions, which cannot close a 2.3x gap, so the trainable
target is damage-mitigation rate inside the fight, not arrival condition and not play
frequency. That is a reward-design question and waits for the unfreeze call. The act-1 wall is also not one enemy: arrivals face three boss tiers
(183x25, 262x30, 324x28), so Vantom -- the Wound-dealing boss dissected earlier -- is
25 of 83 arrivals, and no Wound hand appeared in any of the nine winning fights. The nine ledgered Act 1 victories are unaffected: all nine seeds were
re-censused as overgrowth-generating, and this checkpoint's three of them simply sit
outside the enumerated seed window. One honest method note: the census first read as
"15% of boss arrivals convert", and every one of those wins turned out to be an Act 2
seed -- `simulator_act1` is a mixed-act population, so any conversion figure that
does not split by generated act is meaningless.

**Act-2 completion is not a function of the boss fight.** The single truncation among
the census' 162 boss arrivals was forked, and it was the act boss (same
`current_node_type=6`, same `encounter_id=84`, same floor 17, boss emptied). The
engine checks `CurrentNodeType == NodeBoss` on only *one* of the two exits from a
boss node -- `AdvanceAfterRelicReward` (`RunEngine.cs:1905-1925`); the other exit is
`AdvanceAfterNode` (`:1983-1997`), which compares `Floor >= terminalFloor`, and that
is 17 for overgrowth but 33 for underdocks, a floor no generated act-2 map reaches.
Replaying seed `130012038` to its floor-17 relic-reward decision and substituting
every legal action (`scripts/probe_boss_reward_order.py`,
`docs/evidence/act2_boss_completion_fork_20260919.json`): 2 of 4 end
`phase=complete won=true`, 2 fall back to `map` and dead-end on an empty mask; the
card-reward screen is the negative control (all 4 actions identical). Consequences,
bounded: that run is a **false negative** in the win ledger, so "34 named clears"
reads as a floor rather than a count; and act 1 is **structurally immune** (both
exits complete at floor 17), so this cannot explain the Act-1 conversion deficit.
**The rate is now measured for this checkpoint** (`docs/evidence/
act2_boss_misexit_rate_20260919.json`, the full 10,000-seed promotion partition swept
in disjoint shards): generated act 2 kills the boss in 86/241 arrivals (35.7%) but is
judged a win only 65 times, so **21/86 = 24.4% [16.6,34.5] of act-2 boss kills are lost
to that exit**; generated act 1 loses zero of its 248 arrivals that way, and 245 of them
die inside the fight. Within the promotion partition the 21 are unevenly spread -- 1 in
seeds 130010000-130013499 versus 20 in 130013500-130019999 -- so an **independent,
disjoint window was measured to arbitrate**: the `checkpoint` split's first 3,500 seeds
(130000000-130003499) give act-2 kills 27 with 6 lost (22.2%) and act 1 again 0 of 89.
Determinism was checked before trusting any of it (3,500 promotion-head seeds
re-measured with the current code: zero category and zero step-count differences; 3 tail
truncations re-run sequentially truncate at the same step; shard counts 1/2/4/5 agree).
So the quotable figure is the two-split pool, **27/113 = 23.9% [17.0,32.5]**, and the
promotion head window is a low outlier whose cause was **not** found. A second, different
arm's checkpoint on the same 3,500 seeds loses 7 of its 28 act-2 boss kills (25.0%) and
none of its 73 act-1 arrivals, so the class is engine-side, not one policy's habit.
**The signature was checked per seed, not sampled**: all 34 lost runs (33 distinct seeds,
both checkpoints) were replayed through the independent rollout loop and every one ends at
`current_node_type=6`, `phase=map`, `floor=17`, `empty_action_mask`, `run_won=false`,
0 illegal (`docs/evidence/act2_boss_misexit_signature_verified_20260919.json`), and the two
evaluation paths agree on step counts seed for seed. Caveat kept in that file: per-run boss
HP was not recorded for all 34, so "boss emptied" is inferred from standing on the Map phase
of a NodeBoss node, with direct HP evidence only for the seven forked runs.
Priced against the live gate (`config/training_v2.toml`, recomputed by the
`gate_math_uses_the_live_config` claim): 21/10,000 episodes = 0.21%, i.e. ~1.05 expected
truncations in a 500-episode promotion draw against a 15-episode allowance -- **no promotion
decision in the campaign flips because of this class**, but the mixed clear rate is
understated by 23.6% (68 judged vs 89 actual boss kills per 10,000 episodes), which does
matter to 1%-level cross-arm comparisons.

**How to read the objective's "0 illegal actions" clause on this stack.** There are three
separate contract channels and only the first is the policy's; recomputed over all 225
current-schema metrics files (39,891 episodes) by `scripts/census_contract_channels.py`
(`docs/evidence/contract_channels_20260919.json`, claim `contract_channels`):
`illegal_actions` **0**, `rejection_events` (the native layer refusing an action the mask
advertised) **18,160 = 0.455 per episode, 0.530 on act1/promotion**, and episodes *ended* by
`native_rejection` **0** under the current schema versus **861** under the legacy one. The
zero on the first channel is partly structural: the default `rejection_mode="filter"`
(`training/v2_flat_env.py:164,276-290`) removes the refused action and hands the decision
back, so the policy never executes an illegal action and the disagreement reappears as a
rejection. **Which phase that happens in is now measured, and it narrows the caveat to
almost nothing where this file's strongest claims live.** `scripts/census_rejection_phases.py`
keyed every refusal to the state before the step, over three disjoint 500-seed slices of the
act1 promotion partition on the census checkpoint (1,500 episodes, 176,293 decisions;
`docs/evidence/rejection_phase_attribution_20260919.json`, claim `rejection_phase_attribution`):
refusals occur **only** in `shop` (641 of 2,866 decisions = 22.4%) and `event` (112 of 4,305 =
2.6%), and **never** in `combat` (0 of 130,993), `relic_reward` (0 of 17,698) or `card_reward`
(0 of 5,459). So "the policy chose base 0 at the boss reward screen" is not a filtered
artefact -- those 23,157 screen decisions contain no refusal at all. There is also no third reward screen to hedge about: `RunPhase.cs` declares
11 phases, none of them a potion reward, and `training/v2_constants.PHASE_NAMES` mirrors those
11 one-for-one -- so an earlier reading here (`potion_reward` is "unobserved rather than clean")
was a category error rather than a sampling gap, and the claim now checks the contract instead of
the sample. What the caveat does reach:
in shop and event, every one of the 503 refusal states ended with an action the policy had
*not* chosen first, so filter mode is re-deciding, not discarding. **The cause is now measured, and my guess was wrong.** `scripts/probe_shop_refusals.py` re-plays each refusal against the engine's own predicates from the state before the step (753 refusals over 176,293 decisions; `docs/evidence/refusal_root_cause_20260919.json`, claim `refusal_root_cause`): all 641 shop refusals are one capacity inconsistency -- `WriteActionMask` tests `hasPotionSlot` across all three `State.PotionSlots` (RunEngine.cs:727) while `AddPotion` fills only `min(2, len)` of them (RunRewardGenerator.cs:1015), so a third potion slot is advertised that cannot be used. **Not one shop refusal was a price problem: every refused action was affordable.** The 112 event refusals split 72 failed preconditions on an option the event does handle and 40 refusals of an option its `StepEvent` arm never handles, because the mask's `default:` arm advertises 0..EventSkipAction for events it does not know (RunEngine.cs:3543-3548) -- `scripts/audit_event_mask_cases.py` finds 48 of 58 declared events in that exposure, and every event that actually bit in these episodes is inside it. Nothing here implicates our own layer: across 176,293 decisions the flat mask never advertised a base the native mask had off. Still not measured: how much deck strength the phantom slot costs (388 of 1,500 episodes carry at least one refusal, concentrated in 326 shops), the individual preconditions of the other events, and how depth would move if the first choice executed.

**The cost question came back self-limiting, not answered.** `scripts/measure_potion_slot_cost.py` re-rolls 3,500 seeds recording usable potions at the first boss decision, split by whether the episode had met a potion-capacity refusal (`docs/evidence/potion_slot_cost_20260919.json`, claim `potion_slot_cost`): the refused group arrives with *more* potions (act 1 1.595 vs 1.239; act 2 1.378 vs 1.071) and reaches a boss 10.6% of the time vs 3.1%. That is not an effect of the refusal -- a potion-capacity refusal can only fire while both usable slots are already full, so the exposure variable is close to a restatement of the outcome variable. The comparison is uninformative by construction and re-analysis cannot repair it; the unconfounded design holds the potion state fixed and varies what the shop offers. What the run does confirm: the phantom slot is occupied in 0 of 162 arrivals, and a terminal `RunPhase.Complete` is **not** a clear -- 161 of 162 arrivals end in it, because RunEngine.cs:1286-1293 also sets that phase on a loss, so only Complete AND `State.LastPlayerWon` reproduces the census's 25 wins. That is the field-level mechanism behind the standing trap that floor 17 cannot distinguish dying at the boss from beating it. The instrument also independently reproduces the committed census: 162 arrivals (83 act-1 / 79 act-2) and 25 clears, all of them act-2 generated.

Sampling six more
census truncations and forking each one's boss relic screen: **7 of 7 lost runs have a
legal action that would have judged them a win, and it is the same action every time --
`proceed` (base 3) -- while the policy chose the leftmost claim (base 0) in all 7.**
That is deliberately *not* proposed as a fix: at a boss the proceeding action skips the
remaining relic, so training toward it would teach the policy to forgo in-game value to
satisfy a simulator exit condition -- a new reward hole, not a repair. Either the engine
makes "boss node cleared" the shared test on both exits (upstream/operator call, and it
invalidates every terminal figure here) or act-2 judged-win rates keep being read as
depressed by this class.

**That is one of four engine-side findings, now consolidated in one place.**
`docs/ACT1_CAMPAIGN_2026-09-19.md` ends with a checklist ("本战役查出的引擎侧问题") listing,
each with its measured magnitude and source lines: the two-exit boss-completion judgement
(21 of 86 act-2 kills unjudged = 24.4%, replicated 22.2% and 25.0%, 0 in act 1);
`RunPhase.Complete` being written on a loss too, which is the field-level reason floor 17
cannot separate dying from clearing (161 of 162 arrivals report Complete, only 25 are clears);
the phantom third potion slot (641 of 753 refusals, none of them unaffordable); and
`WriteEventActionMask`'s `default:` arm advertising every option for 48 of 58 declared events
(112 refusals, 72 precondition and 40 nonexistent options). Claim `engine_findings_checklist`
ties each number in that section back to its artifact and fails if the section loses an item or
cites a file that no longer exists. All four are read-only measurements: nothing in the engine
was modified, no DLL was swapped, and the in-game solver stayed on throughout.

**The refusal classes are not this checkpoint's quirk, and the capacity bug is a pattern.**
`docs/evidence/refusal_class_generality_20260919.json` (claim `refusal_class_generality`) re-runs
the classifier over 4 checkpoints and 2 stages, six runs and 1602 refusals: both
classes appear in every run, and every run reports 0 decisions where our flat mask advertised a
base the native mask had off. The mix is *not* stable -- the act1 census is 641 potion-capacity vs
112 event while the floor6 stage inverts it to 43 vs 292 -- so no per-episode refusal rate quoted
from one stage transfers. And the faulty capacity test turns up in a second place: event 7
(`TheLegendsWereTrue`) option 1's own mask arm uses
`PotionSlots.Any(potion => potion == 0)` (RunEngine.cs:3519) against a step that calls `AddPotion`
(`:2263`), so a mask-*handled* event refuses too (10 rows, all
carrying the two-full-one-empty signature). A whole-tree scan finds the test at exactly
2 mask sites, RunEngine.cs:727, RunEngine.cs:3519, and both contradict `AddPotion` -- the fix is a
predicate pattern, not one line. Unseparated: whether the class mix moves because of the stage or
the checkpoint, since floor6 differs in both.

**What I ran against it:** two new V2 curriculum act1-stage arms
(`runtime/act1_ab/a1filt`, `runtime/act1_ab/a1mix`), 2,000,000 timesteps × 12 envs
each, started 2026-09-19 00:31:39 UTC, both finishing below the promotion gate.
This was to test one hypothesis (whether half the act1 stage's training seeds
generating Act 2 explains Act 1's weakness). **The hypothesis came back refuted**
(Δ mean_final_floor −0.280, paired CI [−0.583, +0.020]), so the budget spent is
not even buying a positive result.

**What was not damaged:** nothing under `runs/curriculum_v2`, `runs/ablations`,
`runs/ppo_compare`, `data/teacher` or `models` was created or modified tonight
(checked by mtime against 2026-09-18 16:00 UTC: 0 files). The new arms live under
gitignored `runtime/` only, and frozen artifacts stay frozen and undeleted. The
overnight 20-arm campaign predates this session (started 2026-09-18 17:57 UTC) and
was inherited state, not something I started; my own additions are the two arms.

**For the operator, concretely:**
- The standing goal text ("以 scripts/run_curriculum_fanout.py 多臂并发跑过夜")
  directs PPO fan-out that this freeze prohibits. The two instructions are in
  conflict, and that is your call to resolve, not mine to work around silently.
- If the freeze stands: no further PPO arms; tonight's evaluation-only results
  still stand (win-rate statistics are explicitly listed as active), and
  `runtime/act1_ab/` can be deleted or archived at your discretion.
- If it is lifted: `docs/FREEZE_2026-09-02.md` and the marker need an appended
  decision record, and the floor10 fix must be documented first — the cause was
  recorded as "survival-without-advance reward exploitation", which is also the
  most plausible explanation for the campaign's flat act1 series (depth saturating
  around floor 8 while survival reward keeps accruing).

## One static contradiction that currently gates two things (2026-09-19 02:27 UTC)

`docs/FIXED_SEED_FEASIBILITY.md` now records this in full. Short version: the
recorded `installed_bridge_supported = false` is contradicted by the binary that
is actually installed — the DLL at the locked path (`CD3EA7409F5AC697…`, equal to
`live_version.lock.json`) contains `seed_requested` / `seed_canonical` /
`seed_injection` / `seed_verified`, `/api/v1/singleplayer`, and five
`Seeded embark requires/failed…` error strings.

I deliberately did **not** flip that flag or its asserting test: compiled strings
show a code path exists, not that the endpoint honours `seed` and that the
authoritative `current_run.seed` reads back equal — and this project's rule is no
live readback, no support claim. What changed is the flag's status: it is now
"unverified, static evidence favourable", which is not the same as "verified false",
and should no longer be quoted as the latter.

Why it is worth one operator-minute: that same gap is the stated reason
`docs/COMBAT_SOLVER.md` keeps formal Phase B BLOCKED, and the Phase B verdict is
condition (1) of the Combat PPO unfreeze. One live call
(`scripts/run_solver_comparison.py --max-battles 1`, checks listed in the note)
decides both. It starts a run, so it is not something to do unattended.

**One clause in the campaign report was an over-correction, now fixed.** It had said the dead-end labelling "cannot tell 'cannot act' from 'acts too slowly'". A census over all 282 committed metrics files (`scripts/census_dead_end_vocabulary.py`, `docs/evidence/dead_end_vocabulary_20260919.json`, claim `dead_end_vocabulary`) gives a three-label vocabulary -- `native_rejection` 861 (legacy schema only), `empty_action_mask` 50, `step_cap` 3 -- so both notions are labelled and `step_cap` has fired, all three times in the act1 stage. The residual gap is narrower and different: `dead_end_reason` holds one value per episode, so a co-occurring pair would record only `empty_action_mask`, and whether that ever happens cannot be seen from these files. `unclassified_dead_ends` remains 0 across the whole population.

## 2026-09-19 (late) -- the live path's first blocker is not the game being off

`scripts/run_solver_comparison.py --dry-run --max-battles 1 --seed-mode fixed` reaches
`verify_solver_inventory` (run_solver_comparison.py:146-175) and stops there with
`VersionLockError: ... inventory incomplete/failing: STS2-RitsuLib, CombatSolver`. That gate
re-hashes each required mod DLL against
`evaluation_environment.mod_dll_inventory`, and two of the three required entries no longer match
the files on disk (`docs/evidence/solver_inventory_drift_20260919.json`, captured by
`scripts/capture_solver_inventory_drift.py`, read-only):

| mod | locked sha256 | observed | observed version | DLL mtime |
|---|---|---|---|---|
| STS2-RitsuLib | `189DC61B...` | `E3959F17...` | 0.6.2 | 2026-09-16T15:29 |
| CombatSolver | `E9787918...` | `41BDB5DA...` | 0.41.0 | 2026-09-18T16:31 |

STS2_MCP and the optional RegentFX still match, so the inventory is not wholesale wrong -- these
two are Workshop auto-updates. The lock's own `version_history` ends at **0.31.0**, so the install
has moved twice since even the drift this working copy captured. Restoring git HEAD's lock does
not help: the identical dry-run against a temporary copy of HEAD's version fails the same way, so
this is not an artifact of the uncommitted capture.

**What that means for the authorized live attempt.** Reconciling `config/combat_solver.lock.json`
is the operator's call and the file was left untouched here. Once it is reconciled, the pre-flight
order is: (1) re-validate grammar v2's marker vocabulary against a log from the 0.41.0 install --
the per-batch attestation exists precisely for this; (2) the fixed-seed availability judgement
(`seed_requested == seed_canonical == requested`, `seed_injection` true, authoritative
`compendium.current_run.seed`); (3) only then `supervise_solver_batch.py --mode observational
--allow-actions`. Launching the game stays with the operator; the in-game solver is never turned
off, and no live Steam/Workshop state is touched from here.

## 2026-09-19 (late) -- two defects in the report's own integrity harness

While re-verifying the campaign report, two problems turned out to be about the checker rather
than about the runs. Both are now fixed and pinned; neither changes a measured result.

**1. A tally that conflated "wrong interpreter" with "the report drifted".** Running
`scripts/verify_report_claims.py` on the contract interpreter (`.tools\python\...`, `.venv`, or
system python) fails exactly one claim -- `v2_contract_sizes` imports the environment, which needs
numpy/Gymnasium -- and the old summary printed `39/40 claims match the disk`, which reads like a
report-versus-disk mismatch. `main()` now scores a raising claim separately: it is named in a
trailing "were not scored" line that says to use the training venv
(`G:\qoder\third_party\slay-the-spire-2-emulator-main\.venv\Scripts\python.exe`, the same
interpreter `scripts/test.ps1` uses for the torch half), and it is excluded from the fraction.
The exit code stays 1 in both cases, so a wrong-interpreter run can never be read as a clean one.
`tests/test_report_claim_gate.py` locks that behaviour (6 tests now).

**2. The report quoted a constant its own harness rewrites on every run.** The prose pinned
`bundle_root` to a literal short digest. But `docs/evidence/` contains
`act1_report_expectations.json`, and that file is inside the bundle -- so *every re-pin of an
expectation changes `bundle_root`*, and the quoted literal had been wrong since the previous re-pin
with nothing checking it. The report now points at the manifest instead of quoting it, and a new
claim, `harness_self_description`, re-derives the report's self-descriptions from the tree: the
claim count quoted in prose against `len(CLAIMS)`, the gate-test count against the test methods in
the file, the evidence-file count against the manifest's `evidence_file_count`, and a negative
check that no `bundle_root` hex literal reappears in the prose. Four of those six were
mutation-tested (re-introducing each stale value flips the matching check to false).
`retraction_ledger_integrity` gained a matching check that its closing paragraph's count equals the
row count, and the ledger grew to 15 rows -- this is the first entry that is not about the game.

Registry after that change: **41 claims, all matching the disk (exit 0)**; `scripts/test.ps1`
497 contract + 106 training-environment tests, OK. The attestation gap that the campaign report
described as "13 arms" was recounted from `fanout_attestation_20260919.json` and is **19 of 21**
arm runs lacking a `warm_start` block (17 act1-stage + 2 floor3→floor6 ladder arms); the two 09-19
A/B arms carry both attestation mechanisms.

## 2026-09-19 (late) -- "Act 2 is winnable because those runs grew longer" is now measured, and false

The report's line "arrival rates are the same, the whole difference is the boss fight" compares
two acts' win rates and had never checked that the two groups of arrivals are comparable runs.
`scripts/measure_arrival_state_at_boss.py` (one checkpoint `a1ada27a…`, the first 3,500 promotion
seeds, seven 500-seed shards) reads the run state at the first floor-17 boss decision;
`scripts/build_arrival_state_evidence.py` merges the shards and **refuses** to publish unless the
arrival/clear counts reproduce `potion_slot_cost_20260919.json` (162 arrivals, 83 Act-1 / 79
Act-2, 25 clears all Act-2 -- they do).

* All seven arrival resources are indistinguishable across acts (deck 18.33 vs 18.08,
  relics 3.24 vs 3.33, gold 96 vs 92, max HP 83.7 vs 82.2, usable potions 1.40 vs 1.22,
  shops 1.33 vs 1.32): every bootstrap CI covers zero, every permutation p >= 0.077, and every
  point estimate except relics favours **Act 1**. Structurally there is no extra growth to
  accumulate -- the generated map is 17 floors in *each* act and both acts offer three bosses.
* The stronger test uses the act that actually wins: within Act 2 (25 clears vs 54 losses) no
  arrival resource separates winners from losers either (all seven CIs cover zero, p >= 0.47).
  Power bound stated with the result: 25 vs 54 only excludes effects above ~0.45 relics /
  ~0.75 cards.
* Not addressed, and recorded in `not_established`: **card quality**. `state_info()` exposes
  counts, not strengths, so "equal deck size" is not "equal deck". Act-1-specific prediction is
  also untestable in this window (zero Act-1 clears among the 83 arrivals).

**Correction to that third bullet, same hour.** It rested on one accessor's field list; the deck
list itself is signed (`CombatFactory.cs:243`, `new CardInstance(Math.Abs(id), id < 0)`), so
upgrade count and distinct-definition count are reachable and were added on a second pass -- which
re-ran every shard from scratch and reproduced the first pass's seven differences exactly, a
determinism check that came free. With quality in: upgrades at arrival are 0.63 (Act 1) vs 0.70
(Act 2), p=0.68, and **48 of 83 Act-1 arrivals carry no upgraded card at all in an 18-card deck**;
the only interval that excludes zero among the nine is distinct card definitions (+0.54, p=0.052)
and it points *toward* Act 1. The claim `no_resource_favours_act_two` was rewritten at the same
time to test direction (an interval entirely below zero) instead of "every CI covers zero", which
had quietly encoded "no difference measured" as "nothing favours Act 2".

Consequence for the goal: the Act-1 wall is not an arrival-resource problem, which is the
cheapest thing a reader would have blamed, so the remaining candidates stay where the earlier
sections put them -- longevity in the boss fight itself. Registry: 42 claims, all matching the disk.
One provenance gap surfaced while doing this -- `potion_slot_cost_20260919.json` was assembled by a
merger that kept only per-shard `aggregates`, so it carries no top-level `checkpoint_sha256` and its
rows are attributable only via sibling artifacts; the new merge records the digest and the
encounters, which is what let its reproduction check run at all.

## 2026-09-19 (late) -- combat card rewards never roll an upgrade, and the demo reward branch reaches ordinary seeds

`RunRewardGenerator.cs:1127-1131` is the whole finding:

```csharp
private static bool RollCardUpgrade(RunState state, int cardId, GameRng rng)
{
    _ = rng.NextDouble();
    return false;
}
```

The upgraded-reward pathway is implemented in shape -- `RewardUpgraded[i]` is set from this roll at
`RunRewardGenerator.cs:800`, and `RunEngine.cs:1737` adds the taken card with that flag -- but the
roll is a stub that burns an RNG draw and returns false. `scripts/measure_reward_upgrade_availability.py`
censused it over the campaign checkpoint and 3,500 promotion seeds: **12,677 card-reward decisions,
2 carrying an upgraded card, 0 taken**, and **77.5% of episodes finishing with no upgraded card at
all** (mean deck 15.5 cards, 0.326 upgraded).

Those two exceptions are the second half of the finding: both sat on **floor 5 with all three offers
upgraded**, which is the branch `ApplyRetainedTraceCardReward` wrote for the demo seed -- guarded by
`state.Floor == 5 && state.PlayerHp == 74 && state.Gold == 120`, i.e. by mutable run values and
**not** by the seed string. Any ordinary seed that passes through those numbers is handed a
three-upgraded-card reward the unmodified engine would never offer. (The policy skipped both, n=2,
recorded as anecdote not result.) The leak was then sized rather than assumed: five of the scripted
branches are guarded on mutable values alone (floors 4/5/6/7/9), and across these 3,500 runs they
fire **3 times in 12,677 reward decisions (0.024%)** -- one at floor 4, two at floor 5, none at
6/7/9. So the mechanism is real and unprotected by the seed string, while its effect on any rate
quoted in the campaign is fourth-decimal. Re-running the whole census reproduced every count
bit-for-bit, which is a determinism check on the evaluation path that came free.

Why this matters for the objective: it is a ceiling, not a misjudgement. Nothing trained inside this
simulator can learn or be measured on "preferring upgraded cards", so deck strength reachable here
sits below deck strength reachable in the shipped game, and the Act-1 boss survival numbers inherit
that gap. What is *not* established, and is labelled so in the artifact: what the shipped game rolls
(the emulator source is the only thing in evidence) and whether removing the stub would raise win
rate (this measured availability, not the counterfactual). `engine_findings_checklist` now covers
six numbered findings and re-derives these counts from the artifact, including that both exceptions
are the hard-coded floor-5 branch.

## 2026-09-19 (late) -- the campfire upgrade is open every single time, and the policy takes it 1.5% of the time

Chasing the "why are the decks so old" thread to its end produced the session's most actionable
evaluation-only result. Three sources of an upgraded card, same checkpoint, same 3,500 seeds:

| source | opportunities | upgrades realised |
|---|---|---|
| combat card reward | 12,677 offers | **0** -- `RollCardUpgrade` is a stub returning false |
| events (17 classified: 10 promote a held card, 4 grant an upgraded card, 2 conditional, 1 plain) | 1,003 visits | 512 |
| **campfire** (`StepRest`, `RestUpgradeAction = 1`) | **1,883 visits, upgrade legal in 1,883 of them** | **29** |

`scripts/measure_rest_site_choice.py` reads the native 32-entry mask at each campfire, so "legal"
is the engine's own advertisement, not an assumption: the upgrade option was never missing, and all
29 times the policy took it, an upgraded card actually entered the deck (29/29) -- so there is no
hidden precondition. The other 1,854 visits healed, for a mean of +22.8 HP.

Read it narrowly. This is **not** "the policy plays rest sites wrong": the campaign's own Act-1
finding is that fights are lost by dying too soon, and healing buys exactly that. What the numbers
support is that the deck-strength path is *not* closed -- one lever is fully open, entirely
policy-controlled, and used in 1.54% of opportunities, which makes it the first thing worth
watching if the PPO freeze is lifted, and a reward-shaping decision rather than an engine defect.

Two honesty items shipped with it. The accounting does **not** close: 1,140 upgraded cards exist
across the population, events + campfire explain 541, so **599 (52.5%) are unattributed** --
relic pickups (`RunNonCombatEffects.cs:99-104`, `RelicPomander` / `RelicNeowsTalisman`) are the
plausible candidate and were deliberately not measured, so the residual is reported as a number.
And 2,374 of the 3,500 runs never reached a campfire at all, so 1.54% is a rate over visits, not
per run. Also retracted in place: engine checklist finding 6 had generalised "this class of
improvement is unmeasurable and unlearnable here" from the one blocked path; it is now scoped to
reward granting. Registry 43 claims, all matching the disk; 504 contract + 106 training-environment
tests OK.

**One follow-up turn later: the named candidate for that residual was measured and does not hold.**
`scripts/measure_relic_pickup_upgrades.py` censused relic screens
(`relic_pickup_upgrades_20260919.json`): of 12,417 decisions in the `relic_reward` phase only **193
actually offered a relic** -- the phase is shared with other reward picks, so counting screens
overstates the opportunity set by ~60x, and the correct filter is the offered-relic field. Across
24 distinct offered relics in 180 of 3,500 runs, **zero screens produced a deck upgrade**, and the
two relics that would have done it (`RelicPomander = 201`, `RelicNeowsTalisman = 162`) were never
offered nor acquired at all -- so for this population the relic route contributes exactly nothing,
which is "the route did not exist here", not "the effect does not work". Attribution on the subset
that does exist is sound: 36 of 36 acquired relics matched the id the screen had advertised.

That leaves the 599-card residual unexplained and sharpens where to look next: all three censuses
read `event`, `rest` and `relic_reward` phases and **none of them instrumented the opening screen**
(phase `ancient`, Neow), and a once-per-run effect is the right order of magnitude for 599/3,500.
Recorded as the next candidate, not as a result.

**The candidate was then confirmed and the books close.** `scripts/attribute_upgrade_increments.py`
stops reading one phase per instrument and records every step at which the deck's upgraded count
changes, so the channels sum to the population: 1,140 upgraded cards = 0 at reset + 1,180 added or
promoted − 40 removed, **remainder 0**. By card: the opening `ancient` screen 463 (304 steps, most
granting two at once), event screens 686 (599 steps), `transform_select` -- where a campfire
upgrade lands -- 29, card rewards 2; 38 of the 40 removals happen at event screens, since events can
also take cards away. The `transform_select` 29 matches the campfire census's 29 digit for digit.
Two lower bounds are now labelled as such rather than being read as totals: the event census's 512
covered only the 17 events my source classifier recognised, and that list is incomplete --
`RunEngine.cs:2210` and `:3307` add a card with an `upgraded` variable through neither
`AddEventRewardCard` nor `UpgradeFirstCard`, which is why the step census counts 599 event-phase
steps. Registry 44 claims, all matching the disk.

**And the opening screen turned out to be a relic pick, used better than expected but positionally.**
`scripts/measure_opening_choice.py` (`opening_choice_20260919.json`, 3,500/3,500 resolved) shows
`ApplyAncientChoice(relicId)` is `ApplyRelicPickup` for one of three offered relics, so the run-start
choice is a strength choice. Upgrade relics (`201`, `162`) are offered in only **436 openings
(12.5%)** -- so 87.5% of runs are never given the option -- but when offered the policy takes one
**304 times (69.7%)**, granting exactly the **463** cards the step-attribution census assigns to
`ancient`. The gap is positional, not informational: offered at slot 0 the take rate is
**202/220 = 91.8%**, at slot 1 only **102/216 = 47.2%**, and slot 2 never carried one, so 132
upgrades are left on the table. `neow_options` is copied verbatim into the V2 observation
(`training/v2_observation.py:66`, `:173`), which rules out "the policy cannot see which relic is
which" as the explanation -- this is a learned slot prior.

One scope correction travels with it: the previous section's "the relic route contributes zero" holds
only for `relic_reward` screens, because these same relics are handed out at the opening instead
(retraction ledger row 18). Overall the strength picture is now: combat rewards structurally zero,
events 686 cards, opening choice 463 with a 12.5% availability ceiling and a 47%/92% slot bias,
campfire 29 of 1,883 legal chances. Registry 45 claims, all matching the disk.

## 2026-09-19 (late) -- what the ladder's own gate records say, rung by rung

The warm-start clause had been summarised from missing parent links, which conflated "this rung was
never trained" with "this rung was trained and rejected". `scripts/build_ladder_promotion_ledger.py`
now reads the 500-episode promotion records the trainer wrote under `runs/curriculum_v2*/**/<stage>/
metrics/` and judges each one with the repository's own `decide_promotion` rather than a
re-implementation of the thresholds, producing `ladder_promotion_ledger_20260919.json`
(claim `ladder_promotion_ledger`, 34 recorded checkpoint digests re-hashed here).

Eight promotion evaluations exist; two promoted. floor3 promoted (both evaluations pass:
`defect_truncation_rate` 0.0, `boundary_rate` 1.0). floor6 was evaluated five times and **never**
promoted -- the first three failed on defect truncation 0.33-0.45 against a 0.03 cap, the last two
reached 0.0 truncation and were still rejected for `boundary_rate` 0.86 against a 0.93 requirement
(and its Wilson lower bound). floor10 has one evaluation, rejected at `boundary_rate` 0.238 against
0.70. floor13 has no directory at all. So the ladder's break is at **floor6**, and the act-1 arms
exist because the config started that stage, not because anything promoted into it.

Two mistakes were made and fixed on the way here, both recorded in the campaign's retraction ledger
(row 19) and in the script's docstring: the report had claimed floor10 had *no* promotion decision
(it does, one, negative), and the first version of this ledger compared `truncation_rate` against
the gate instead of `defect_truncation_rate` -- the field `training/promotion.py:39-48` actually
reads -- which reported a clean 8/8 failure that was entirely the tool's own error. The number to
look at first if the freeze is ever lifted is floor6's 0.86-versus-0.93 boundary rate, not truncation.
## 2026-09-19 (later still) -- the previous section's ladder verdict was answering the wrong question

**Supersedes the last three paragraphs of "what the ladder's own gate records say, rung by rung"
below it.** That section reported eight promotion evaluations and said floor6 never promoted. Both
were re-computations of the metrics under `config/training_v2.toml` **as it reads today**, and the
campaign's own decisions say something else: `promotion_decision.json` -- the artifact the trainer
writes when it promotes, which carries the thresholds that were in force that day -- exists for
exactly two campaign rungs, and both say `promoted: true`:

| rung | decided then | required then | observed | re-judged under today's config |
|------|----|----|----|----|
| floor3 | promoted | `min_boundary_rate` 0.90 | boundary 1.00 | still promotes (agree) |
| floor6 | **promoted** | `min_boundary_rate` **0.80** | boundary 0.86 | **rejects**: 0.93 now, plus a new `min_boundary_wilson_lower` 0.90 |
| floor10 | no decision file | -- | boundary 0.238 | rejects at 0.70 |
| floor13 | no directory | -- | -- | -- |

So the warm-start chain was intact for its first two rungs and the campaign skipped floor10/floor13;
the ladder's break is not at floor6. What floor6 does show is **threshold drift**: the bar that let it
promote has since been raised by 13 points and gained a Wilson-bound clause, so "reproduce that
ladder" and "run a ladder under today's config" are two different experiments. The ledger now reports
both verdicts per rung side by side (`live_decision`, `the_two_verdicts_agree`,
`thresholds_that_changed_under_the_ladder`).

Two population errors in the same artifact, both mine, both found while writing this:
`curriculum_v2*` matched the 30-episode smoke ladders, and three of the "five floor6 evaluations"
came from `runs/curriculum_v2/aborted-pre-filter-floor6-095635Z` -- a run the operator stopped and
named. Rows now carry `run_kind` (campaign / aborted / smoke) and the totals count campaign only:
**five promotion evaluations, two promotions**, with the aborted run's three records listed and
excluded.

**And a gate defect worth more than the ladder finding.** Every promotion record the campaign wrote
predates `boundary_wilson_95_low`, and `EvaluationMetrics` is a dataclass with defaults, so
rehydrating an old record and comparing that field to the stage's threshold produced
`boundary_wilson_95_low 0.0000 < required 0.9000` -- a rejection quoting a number nobody ever
computed, five times over. The mirror case is the dangerous one: an absent `defect_truncation_rate`
reads as 0.0, which is a free pass against the 3% truncation cap. `training/promotion.py` now
refuses any clause whose input was not recorded (reason: "was not recorded in these metrics, so its
clause cannot be scored") and skips the numeric comparison for it;
`EvaluationMetrics.from_payload` records which keys a file omitted, and `to_dict()` deliberately drops
that bookkeeping because which keys a file omits describes the file, not the evaluation. **All eight
rung verdicts are unchanged** -- the fix removes fabricated reasons, it never flips a decision, and it
can only ever add a rejection, never remove one. Tests in
`tests/test_promotion_gate_absent_inputs.py` (5), claim `promotion_gate_refuses_unrecorded_inputs` (6
checks), and retraction ledger row 20 in the campaign report.

**Same conflation, one artifact over:** `ladder_rungs` read any `promotion_decision.json` in a
stage directory as "this rung passed". 69 such files exist under `runs/` and `runtime/`, and **42 say
`promoted: true`** -- including throughput-probe decisions whose own `required.min_episodes` is **1**
and smoke-ladder decisions at **30**, against a stage scale of 500. `gate_passed` now counts only
decisions at the stage's promotion scale, and a new per-stage
`promotion_recorded_only_below_scale` says out loud when a rung's only "promotion" came from a probe.
After the filter floor3 and floor6 still pass and act1 still fails, so no published verdict moved;
what moved is the number of ways a future reader can mistake a 1-episode probe for a rung.
