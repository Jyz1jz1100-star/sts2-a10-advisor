# Existing-solution review (2026-08-31)

No public project currently demonstrates approximately 50% full-run win rate
for Ironclad A10 without save/load. The best-looking local combat number cannot
be used as evidence for this requirement: one published STS2 RL project reports
about 92% on Act 1 combats while its own known-issues page reports 0% complete
runs after one million full-run steps.

## Reuse matrix

| Area | Existing solution | Decision |
| --- | --- | --- |
| Official mod path | Mega Crit built in a mod loader from day one and enabled Steam Workshop in v0.107.1 | Use a mod/API bridge; do not build OCR as the main input. |
| Live structured state | `Gennadiyev/STS2MCP` (MIT) | Primary bridge. Release advisor uses GET only. Patch-lock and schema-test it. |
| Read-only overlay | `bnipper-creator/sts2-advisor` / Spire Oracle (MIT) | Reused as this repository's UI/polling starting point. It previously omitted combat and used a Claude model; both are replaced for the target product. |
| High-speed simulator | `Zamiell/slay-the-spire-2-emulator` (MIT) | Local training baseline. It has NativeAOT, Gymnasium and full-run scaffolding, but explicitly remains incomplete. |
| Broad RL reference | `zhiyue/sts2-rl-agent` | Reference tests/coverage only. No standard open-source license was present; do not copy code. It documents 0% full-run success after 1M steps. |
| Long-horizon agent harness | AgenticSTS (Apache-2.0 agent, mixed data/mod licenses) | Reuse supervisor/evaluation ideas. Its main evidence is Silent A0, not Ironclad A10. |
| Per-action demonstrations | RunReplays (MIT) | Preferred source for exact card order, target, event, shop and reward labels. |
| Outcome attribution | SpireLens (MIT) | Add damage/block/draw/resource outcomes and parity diagnostics. |
| Entity metadata and run aggregates | Spire Codex (PolyForm Noncommercial) | Fetch locally, version by build, respect rate/redistribution terms. Aggregate correlations are not Q-values. |
| Historical run analysis | SpireScope and `sts2-stats` (MIT) | Reuse patch-aware ingestion and statistics patterns. Completed `.run` files do not reconstruct every combat action. |
| STS1 training engineering | AscensionAI (MIT) | Reference BC->PPO, action masking, parallel rollout and fixed-seed evaluation. Do not reuse game rules. |
| OCR / screen vision | No mature verified STS2 pipeline found | Only an alarm/fallback. Localization, animation, scaling and early-access patches make it too brittle. |

## Primary sources

- https://www.megacrit.com/news/2026-6-19-neowsletter-issue-23/
- https://github.com/Gennadiyev/STS2MCP
- https://github.com/Zamiell/slay-the-spire-2-emulator
- https://github.com/zhiyue/sts2-rl-agent/blob/main/docs/KNOWN_ISSUES.md
- https://github.com/AlayaLab/AgenticSTS
- https://huggingface.co/datasets/AlayaLab/AgenticSTS-trajectories
- https://github.com/boardengineer/RunReplays
- https://github.com/romaine-life/spirelens
- https://github.com/ptrlrd/spire-codex

## Risks that change the plan

- Early Access patches change card values, RNG and bridge schemas. A checkpoint
  is valid only for its locked build.
- Public run data has survivor and player-skill bias. A card correlated with a
  strong deck is not necessarily the best choice in the current state.
- An imperfect emulator can be exploited by RL. Real-game differential replay
  and exploit quarantine are hard gates.
- Bridge data may accidentally reveal hidden future information. The visible-
  state filter must be reviewed before any reported benchmark.
- A10 50% is a research acceptance target, not a result that exists today.
