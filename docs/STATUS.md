# Current status

Date: 2026-08-31

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
  non-decision menu screen.

## Not completed

- the target is now locked to the installed public-beta `v0.111.0`, Steam build
  `24489008`, commit `41cef1ea`, assembly hash `222455745`; Steam reports a
  newer build pending, so results must be invalidated if the game updates;
- no real-game parity traces have been captured;
- no Ironclad/A10 expert action dataset is present;
- only a smoke checkpoint has been trained; simulator coverage and
  reward/curriculum work must be extended before meaningful full-run training;
- no checkpoint has passed A0, A5 or A10 full-run gates;
- the requested 50% real-game win rate has not been achieved or claimed.

## Immediate next gates

1. Install/test STS2MCP against the exact local game build and add schema
   fixtures for every screen.
2. Capture fixed-seed real-game traces and begin parity closure.
3. Install a CUDA-enabled PyTorch wheel and run the short reproducibility
   baseline.
4. Add RunReplays/SpireLens local capture and collect Ironclad/A10 decisions.
5. Train BC tactical and strategic heads, then start search distillation.
