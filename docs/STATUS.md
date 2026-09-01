# Current status

Date: 2026-08-31 (handoff update, late session)

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
