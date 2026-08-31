# Acceptance protocol

## Exact target

- Game: locked installed public-beta `v0.111.0`, Steam build `24489008`, game
  commit `41cef1ea`, main assembly hash `222455745`.
- Mode: standard single-player.
- Character: Ironclad.
- Difficulty: Ascension 10.
- Seeds: fresh random seeds disjoint from all training and model-selection
  seeds.
- The user permits save/load, room retry, automated control and replay. These
  runs are reported as `assisted`; they never replace the separate no-SL rate.
- No-SL evaluation still forbids save/load, room retry, future RNG and hidden
  draw order, so the result remains comparable to normal play.
- The policy outputs text; the player performs exactly the advised action. Any
  execution deviation is logged and that run is reported separately.

Two named metrics are mandatory: `assisted A10` (all user-authorized operations
allowed) and `no-SL A10` (normal-play restrictions). The requested 50% target
is evaluated on assisted A10 unless explicitly changed; no-SL is always shown
beside it.

## Gates

1. Illegal recommendation rate: exactly 0 over schema/property tests and the
   final sample.
2. Bridge coverage: every reachable Ironclad/A10 decision screen has a stable
   decision ID and a legal action set.
3. Simulator parity: >=99.9% step-state agreement on the audited core combat
   surface; all remaining mismatches are enumerated and excluded from training
   or fixed.
4. Development evaluation: 200 hidden fixed seeds, used only for checkpoint
   selection.
5. Final evaluation: at least 500 fresh seeds; 1,000 is preferred.
6. Success threshold: point estimate >=50%. The report must also show the 95%
   Wilson interval; the interval is not silently discarded if it crosses 50%.

## Required report

- full-run win rate and Wilson interval;
- Act 1/2/3 survival rates;
- decision coverage and illegal-action count;
- P50/P95 latency for ordinary card plays, first action of combat, and
  strategic decisions;
- policy-only and policy+search results;
- game/mod/emulator/checkpoint/data hashes;
- simulator-to-real mismatch rate;
- crash, timeout and player-execution-deviation counts.

Simulation wins are never presented as real-game wins.
