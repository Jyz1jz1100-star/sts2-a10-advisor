# Runtime and version audit — 2026-09-03

## Decision

The live game process is healthy at the STS2MCP root and main-menu endpoints, and the loaded game/bridge identity is internally consistent with the current game build. The installed CombatSolver is **0.28.1**, while the comparison-track lock still names **0.27.0**. The bridge DLL loaded by the live process is the staged candidate hash, while `config/live_version.lock.json` still names an older bridge hash.

No lock update is approved by this audit. CombatSolver 0.28.1 parser compatibility is **not verified**: the current session reached the main menu but did not produce a combat/search block, and the public upstream materials do not expose a pinned 0.28.1 diagnostic grammar.

## Scope and safety

This was a bounded read-only audit. The only live calls were safe HTTP `GET`s to the local bridge. There were no game actions, save writes, restarts, stops, mod/DLL writes, or lock edits. The live PID remained 40084, started at 13:55:09 +08:00, and was responding during inspection.

The raw game log and raw compendium response were not copied into the evidence bundle. Profile identifiers, Steam owner identifiers, run seeds, and other account-linked values are redacted or omitted.

## Live health and state

Evidence: [http-get.json](../runs/sts2mcp_staging/read-only-audit-20260903T060533Z/http-get.json).

- `GET http://127.0.0.1:15526/` returned HTTP 200 with `STS2 MCP v0.4.0` and status `ok`.
- `GET /api/v1/singleplayer` returned HTTP 200 and `state_type=menu`, `menu_screen=main`; the returned options included `continue` and `abandon_run`.
- `GET /api/v1/compendium` returned HTTP 200. Its `current_run` field is `null`; the compendium endpoint itself is not null and returned section summaries. Raw profile/run content was omitted.
- A restricted `Get-NetTCPConnection` probe returned access denied. That is non-authoritative; the successful HTTP 200 responses prove the listener was reachable.

## Runtime identity and lock deltas

Evidence: [runtime-version.json](../runs/sts2mcp_staging/read-only-audit-20260903T060533Z/runtime-version.json).

| Component | Loaded/observed | Lock expectation | Result |
| --- | --- | --- | --- |
| Game | app 2868840, Steam build 24724944, `v0.111.0`, commit `41cef1ea`, main assembly hash `222455745` | Same in both lock contexts | Match |
| STS2MCP manifest | `0.4.0`, manifest SHA `F64EE11E...AA3C9` | Same manifest SHA | Match |
| STS2MCP DLL | `CD3EA740...B943A4D` (loaded by PID 40084) | `095EE091...C5B124` in `live_version.lock.json` | **Mismatch** |
| CombatSolver | `0.28.1`, DLL SHA `D58C447D...10F78` | `0.27.0`, DLL SHA `F71F1B47...7C217` in `combat_solver.lock.json` | **Mismatch** |
| RitsuLib | `0.5.18` variant selected for game `0.111.0` | `0.5.18` | Match |
| RegentFX | `0.5.1`, gameplay-affecting flag false | Same hash in comparison lock | Match |

The full hashes and paths are retained in the evidence JSON. The loaded STS2MCP DLL hash matches `artifacts/sts2mcp-seeded/candidate_manifest_provenance.json`, but that provenance explicitly requires manual health smoke and keeps `lock_update_allowed=false`.

## Current-session log selection

Evidence: [current-session-log.json](../runs/sts2mcp_staging/read-only-audit-20260903T060533Z/current-session-log.json).

The authoritative current-session source was the active `%APPDATA%\\SlayTheSpire2\\logs\\godot.log`, read through a shared-read handle. Its length was 29,296 bytes, last write `2026-09-03T14:12:46.7689584+08:00`, and SHA-256 `5CD885DF...27AB41E`; its startup content correlates to PID 40084 and the `v0.111.0` main menu. The file was not treated as stale merely because its creation time is old.

The timestamped `godot2026-09-03T13.55.09.log` is a rotated historical file, not authoritative solely because its filename resembles the process start time. It contains only three solver initialization markers. The active current-session log likewise contains only `SETTINGS_LOADED`, `INIT mod=0.28.1`, and `ENGINE` solver markers; it has no `SEARCH_REQUEST`, `RESULT`, or `TURN_OUTCOME` combat block.

## Parser contract assessment

Evidence: [parser-contract.json](../runs/sts2mcp_staging/read-only-audit-20260903T060533Z/parser-contract.json).

The project `combat_solver/logformat.py` parser is grammar v1. It is calibrated against historical 0.25.3/0.27.x logs and expects a request/action/result/turn-outcome sequence, with explicit failure and deployment markers. Replaying the selected historical 0.27.x logs produced 52 events, 47 snapshots, zero failures, and five deploy records. The current 0.28.1 active log produced zero events because it contains initialization only; this is absence of a combat fixture, not a parser pass.

A read-only ASCII/UTF-16 literal scan of the installed 0.28.1 DLL found a mixed/new event vocabulary, including `SEARCH_REUSED`, `SEARCH_INTERIM_ADOPTED`, `SEARCH_COMPLETION_NOTIFICATION`, `SEARCH_SESSION`, `DEPLOY_END_TURN_CHOICE_PLAN`, `DEPLOY_FAILURE`, and `DEPLOY_FINISH`. `RESET` and `DEPLOY_END` literals remain present, while several v1 markers such as `SEARCH_REQUEST`, `ACTION`, `DEPLOY_START`, `DEPLOY_ACTION`, `RESULT`, `TURN_OUTCOME`, `SEARCH_FAILURE`, and `SEARCH_STALE` were not found as exact `[CombatSolver/Test] <EVENT>` literals. This is a heuristic static scan only: it does not prove runtime emission or field/order compatibility.

Therefore this audit makes no unsupported claim that 0.28.1 is wholly incompatible. It records that compatibility is unverified and should remain blocked until a fresh, process-correlated 0.28.1 combat log is available.

## Primary upstream research

Evidence: [upstream-sources.md](../runs/sts2mcp_staging/read-only-audit-20260903T060533Z/upstream-sources.md).

The [Combat Solver workshop item](https://steamcommunity.com/sharedfiles/filedetails/?id=3790899961) describes a single-player, bounded route solver and lists STS2 `0.111.0` and RitsuLib `>=0.5.13`. Its visible change-note page provides 0.27.x entries but not a pinned 0.28.1 grammar. The linked [Torch1230/CombatSolver repository](https://github.com/Torch1230/CombatSolver) documents the same broad requirements and bounded-search limitations, but its exposed public `CombatSolver.json` currently advertises `0.25.1`; it is not a 0.28.1 source contract.

## Requirements before any lock update

1. Capture a fresh 0.28.1 combat/search session with PID, loaded DLL hash, log hash, and timestamps correlated.
2. Establish the actual 0.28.1 event grammar, field names, ordering, deployment semantics, and failure semantics from that fixture; do not infer them from strings alone.
3. Add a checked-in parser fixture and focused tests covering fresh search, reused route, deployment end-turn choice, cancellation/failure, and turn outcomes.
4. Repeat a bounded comparison smoke under one pinned mod inventory and retain source hashes; keep the A10 acceptance lock separate from the solver comparison lock.
5. Only after those checks pass, obtain an explicit lock-review decision for the two current hash/version deltas. This audit itself did not update either lock.

## Follow-up evidence adapter hardening

Within the separately authorized follow-up scope, `combat_solver/evidence.py` now rejects conflicting explicit `turn.decision_id`, `executed.decision_id`, and `record.decision_id` bindings. It intentionally does not compare `state_hash` values, because reused route snapshots may carry a stale hash; the existing precedence and single-turn record-level fallback are preserved. It also rejects boolean, non-integer, and non-positive `executed.turn` values before comparing them with the turn anchor.

Focused regressions were added to `tests/test_deploy_evidence.py` for conflicting decision IDs, stale snapshot hashes, and invalid executed turns. `\.venv\\Scripts\\python.exe -m unittest -v tests.test_deploy_evidence` passed all 10 tests. The environment has no pytest module, so the equivalent focused unittest module was used.
