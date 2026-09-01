# Incident: native emulator FailFast crash during act1 training (2026-09-01)

## Event

Supervised act1 run `20260831T205351Z` (guarded relaunch, adoption-managed)
died at **07:32:03** at ~11.45M of 100M steps. Exit was an OS-level process
kill: the last stdout lines are a .NET FailFast —

```
Process terminated. Access Violation: Attempted to read or write protected
memory. ... at System.Collections.Generic.List`1.GrowForInsertion
   at Sts2Emulator.Core.Run.RunMapGenerator.FindAllPaths(RunState, RunMapNode)
   at Sts2Emulator.Core.Run.RunMapGenerator.FindMatchingSegments(RunState)
   at Sts2Emulator.Core.Run.RunMapGenerator.PruneDuplicateSegments(...)
   at Sts2Emulator.Core.Run.RunMapGenerator.PruneAndRepair(...)
   at Sts2Emulator.Core.Run.RunMapGenerator.GenerateActMap(RunState)
   at Sts2Emulator.Core.Run.RunEngine.Reset(String)
   at Sts2Emulator.Interop.RunNativeExports.Sts2Run_Reset(...)
```

i.e. heap corruption **inside the C# run engine during map generation on a
`run_reset`**, surfacing as a hard access violation. Python cannot catch a
FailFast; no traceback exists. The managed watchdog (pwsh-23) detected the
dead trainer within its poll interval and reported it.

## Assessment

- Sporadic memory-safety bug in the third-party MIT emulator (native heap),
  triggered by some training rollout's seed during act-map generation.
- Data impact: zero. Five checkpoints (2M…10M) and all six metrics files
  predate the crash and are intact; `step_000010000020.zip` loads cleanly
  (verified `num_timesteps = 10,000,020`); lost work is the 1.45M steps since
  the last checkpoint.
- This is a distinct defect class from the two mask bugs documented in
  [EVAL_HANG_2026-09-01.md](EVAL_HANG_2026-09-01.md) (mask-vs-native
  disagreement). All three go into the parity-phase upstream report.

## Countermeasure (implemented same day)

Resume-capable curriculum runner + self-healing supervisor loop:

1. `training/curriculum.py --resume-run <dir>`: continues an existing run
   directory from its highest `step_*.zip` checkpoint. Verified semantics
   (`scripts/probe_sb3_resume_semantics.py`): SB3 `learn(N)` after
   `MaskablePPO.load` restarts its local counter at zero and treats N as a
   delta, so the callback now numbers checkpoints/probes from
   `resume_base + num_timesteps` (regression test
   `test_resume_alignment_numbers_from_global_steps`). Train-seed streams
   resume under a distinct `SeedStream` namespace (`:resume`) since
   partition exhaustion is checked independently; evaluation splits are
   untouched. A `resume-<utc>.json` marker with the source checkpoint SHA
   records the join. Plan compatibility is enforced against `plan.json`.
2. `scripts/run_act1_self_healing.ps1`: bounded crash-restart loop around the
   supervisor. Resumes only on native-crash exit codes; ends on settlement
   (`promotion_decision.json`), clean exit, non-crash failure, or a
   crash-storm guard (two consecutive restarts that crash within 12 minutes
   without producing a new checkpoint). Max 12 restarts.

Live adoption: managed job `pwsh-27` restarted act1 via the self-healing loop
at 08:18, resuming from `step_000010000020.zip` (py-spy confirms
`resume_base_steps: 10000020`, `stream_suffix: ':resume'`). Lost wall-clock:
46 minutes; expected lost compute: ~1.45M steps (re-learned by continuing).
If the map-generation bug is seed-rare (likely: it survived ~5.5M resets
across combat + two act1 attempts at the same seeds before biting), each
resume crosses the offending seed with fresh stream permutation. Worst case
is the crash-storm guard stopping the loop for a human decision.
