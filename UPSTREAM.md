# Upstream components

This project deliberately reuses existing work instead of rebuilding the game
integration stack from scratch.

| Component | Upstream | License | Use here |
| --- | --- | --- | --- |
| Spire Oracle | https://github.com/bnipper-creator/sts2-advisor | MIT | Read-only overlay and polling skeleton. This repository started as a source copy and retains its `LICENSE`. |
| STS2MCP | https://github.com/Gennadiyev/STS2MCP | MIT | Structured live game state. It is installed separately and is not bundled here. |
| Slay the Spire 2 Emulator | https://github.com/Zamiell/slay-the-spire-2-emulator | MIT | NativeAOT/Gymnasium baseline simulator. It is kept as a separate local checkout. |

The following projects are research references only and are not copied into
the deliverable:

- `zhiyue/sts2-rl-agent`: useful coverage and training reference, but no
  standard open-source license was present when reviewed.
- Spire Codex: useful patch-aware metadata and aggregate run statistics, under
  PolyForm Noncommercial terms. Game data must be fetched locally and must not
  be redistributed.
- AgenticSTS trajectories: CC-BY-4.0 decision traces, mostly Silent/A0, useful
  for schema and supervisor validation rather than the final Ironclad/A10
  policy.

Exact upstream revisions must be recorded in `runs/<run-id>/manifest.json`
before a benchmark result is treated as reproducible.
