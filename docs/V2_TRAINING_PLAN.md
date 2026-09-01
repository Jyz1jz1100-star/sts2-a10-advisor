# V2 training plan: search teacher, distillation, and real-game validation

Date: 2026-09-01

## Status of the delivery stages (updated 2026-09-01, session 2)

1. **Training environment contract — done.** Expanded fixed-width observation
   (`training/v2_observation.py`, layout pinned by
   `observation_contract()`, whose SHA-256 is recorded in every metric),
   native `(action, target)` space as `Discrete(225)` with no-alias splitting
   (`training/v2_flat_env.py` + generated `advisor_core/card_targeting_v2.py`),
   `max_floor` enforced by the wrapper, empty-mask and native-rejection dead
   ends labelled and counted, `unclassified_dead_ends` gate at 0, and property
   tests over synthetic and real-emulator states. Recorded gaps: relic
   counters and enemy DefIds are not exposed by native run-API v8; event
   option text is deferred to the live adapter.
2. **V2 curriculum entry point — done.** `python -m training.v2_curriculum`
   with `config/training_v2.toml`: floor 3→6→10→13→Act1, twenty
   pairwise-disjoint per-stage seed partitions, boundary-rate promotion gates
   (0.90/0.80/0.70/0.60) then the 35%/Wilson-0.31 true-terminal gate, all
   metrics `simulator_act1`, output confined to `runs/curriculum_v2*`. The
   real-emulator smoke run passed (floor3→floor6 including cross-stage model
   load; one live `native_rejection` correctly classified).
3. **First teacher batch — done.** Generation + audit pipeline complete
   (`training/teacher_batch.py`,
   `scripts/generate_teacher_batch.py`, `scripts/audit_teacher_batch.py`,
   `training/teacher_bc_dataset.py` for hash-verified BC conversion). The
   first batch-0 run was **quarantined**: a rescore audit found 9.2% of its
   labels would flip under the corrected native-rejection rollout semantics
   (rejections used to leak a raw -1 into candidate scores). The traversal,
   scorer, and materializer were fixed, and **regenerated batch 0 passed the
   triple audit: 8,733 records, 0 offline violations, 60/60 live replay,
   150/150 rescore with 0 best-action flips** (native hash
   `bcd623ce…34bc4`, scope `simulator_act1`, gap median 1.50, 14.7% explicit
   enemy-target decisions; 60-record BC conversion produced 60/60
   hash-verified samples). Expanding to 50k–200k is now a budget decision.
4. **BC + DAgger — first cycle complete.** batch0 materialized 8,733/8,733
   through the contract stack with per-state hash verification; the
   phase-split masked scorer (combat/non-combat heads, prefix-hash holdout)
   reached holdout top-1 0.560 / top-3 0.782. Harness-evaluated student:
   floor3 boundary 0.80 with zero illegal actions and zero unclassified dead
   ends; floor6 0.12 and Act-1 mean floor 2.92 confirm imitation alone
   plateaus — the curriculum ladder and DAgger corrections carry from here.
   DAgger generator (`training/dagger_batch.py`) reuses the teacher record
   format and adds student provenance + teacher_agreement; a real 6-seed
   probe found 4/8 teacher-student disagreements (all audited clean).
5. Fixed-seed ablations, 6. promotion budget: not started. The official V2
   `floor3` stage (full config budget) is running now, and batch-1 (~35k
   records) is generating for the next distillation/DAgger round.
7. Real-game adapter: not started.

## Why V1 stopped

The flat run-level MaskablePPO baseline was intentionally stopped at cumulative
16,000,008 simulator steps. Corrected terminal-run scoring is 0–1% on the
100-seed checkpoint split and 0/500 on the 10M promotion split. The earlier
2–5% values were stale *last-combat-won* flags, not completed Act 1 runs.

V1 also cannot meet the product contract structurally:

- its observation omits deck identities/upgrades, relic identities/counters,
  and part of the shop inventory/prices;
- its combat action does not encode an enemy target;
- its reward is almost entirely per-combat damage shaping and has no run-level
  terminal objective;
- `max_floors` is not enforced by the checked-in simulator wrapper;
- the simulator contains reachable mask defects and retained-trace special
  cases, and is not evidence of exact current-game A10 parity.

The preserved V1 checkpoint is a negative baseline, not a deliverable model.

## V2 decision stack

1. **Contract wrapper**
   - count a win only on terminal run completion;
   - turn an all-zero legal mask into a labelled environment truncation;
   - enforce floor curriculum boundaries;
   - expose separate raw combat reward, potential delta, terminal reward, and
     simulator-defect counters.
2. **Action codec**
   - represent combat decisions as `(action, target)`;
   - avoid duplicate target aliases for non-targeted cards/actions;
   - keep stable action IDs so the same object can drive search, behavior
     cloning, trace validation, and Chinese textual advice.
3. **Prefix-replay search teacher**
   - rebuild a state from `seed + action/target prefix`;
   - verify observation and legal-mask hashes after every replay;
   - use short combat beam search and root-action run rollouts;
   - label every generated example `simulator_act1` with emulator hash, search
     budget, score gap, and information-set policy.
4. **Distillation and DAgger**
   - warm-start the phase-conditioned legal-action scorer from high-confidence
     teacher examples;
   - query the teacher on student/heuristic disagreements and dangerous states;
   - fine-tune with masked RL only after the teacher policy beats the V1
     baseline on untouched seeds.
5. **Real-game adapter and validation**
   - expand the live observation to the same visible feature contract;
   - record player/mod actions, including repeated-card identity and target;
   - keep three disjoint reports: simulator, real-game assisted SL, and
     real-game no-SL. Simulator wins are never presented as real A10 wins.

## Curriculum and ablations

Use floor boundaries `3 -> 6 -> 10 -> 13 -> Act1 complete`. Each level gets a
fixed 500-seed holdout split. Before any long run, compare on the same 100
checkpoint seeds with three random training seeds:

1. contract fixes only;
2. contract + complete observation/phase heads/target codec;
3. contract + model changes + shaped reward/search warm start.

Initial reward ablation (not a claimed optimum):

`0.1 * combat_reward + delta(0.25 * floor + 0.5 * hp_fraction)`

plus `+10` for true Act 1 completion and `-10` for death. Every component is
logged separately so proxy-reward exploitation is visible.

## Promotion gates

No stage expands its budget unless all of the following hold:

- zero policy-illegal actions;
- zero unclassified empty masks/native rejections;
- no train/checkpoint/promotion/final seed overlap;
- true terminal-run win rate and Wilson interval improve over V1;
- mean final floor improves without a compensating collapse in HP;
- the checkpoint, dataset, simulator, and game-build hashes are recorded.

Before the requested ~50% target can be claimed, run at least 500 current-build
real Ironclad A10 games in the appropriate assisted-SL cohort. Report the point
estimate, 95% Wilson interval, save/load intervention rate, crashes, and excluded
games. The current project has not reached this gate.

## Data boundaries

- The existing BC smoke dataset contains only eight proxy-seed actions and is
  not training evidence.
- Local history includes 106 single-player Ironclad A10 runs (8 wins), but only
  one is from v0.111.0 and the files do not contain card-by-card combat order.
  Older histories may pretrain high-level value/choice heads only; they cannot
  enter current-build evaluation or combat BC.
- The experimental multi-Act `full_run` stage remains disabled until Act 1
  contract, target encoding, search replay, and parity checks pass.
