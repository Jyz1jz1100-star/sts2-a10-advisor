# Current status

Date: 2026-08-31 (handoff update, late session)

## Monitoring notes (unattended watch, 2026-09-01)

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
  Decision: let this run finish to its 100M budget (~21 h) so the baseline
  endpoint is documented and the promotion-decision trail is complete;
  reward shaping / curriculum changes are strategy calls to propose to the
  user after the run settles, not unilateral mid-run edits.
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
