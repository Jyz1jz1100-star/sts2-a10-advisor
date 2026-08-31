# Current status

Date: 2026-08-31 (handoff update, late session)

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
- started the full act1 stage (100M steps) under the supervisor at 22:22 local
  (run `20260831T142225Z`);
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
