"""Combat Solver integration (Slay the Spire 2 workshop item 3790899961).

This package is the structured, READ-ONLY interface between the advisor and
the external Combat Solver mod. It never scrapes the game UI, never reads
game memory of the mod internals, and never sends actions to the game.

Layers:
- snapshot: normalized record contract (state hash, route JSON, search
  budget, candidate routes) + failure records;
- executed: actual-action inference from read-only STS2MCP state deltas;
- reader: pluggable on-disk sources that produce raw records;
- compare: per-battle prediction/deviation comparison + gated aggregation.

Design record: docs/COMBAT_SOLVER.md. Freeze context: docs/FREEZE_2026-09-02.md.
"""
from combat_solver.snapshot import (
    SnapshotError,
    SolverSnapshot,
    SolverFailure,
    failure_from_json,
    snapshot_from_json,
    snapshot_to_json,
)

__all__ = [
    "SnapshotError",
    "SolverSnapshot",
    "SolverFailure",
    "failure_from_json",
    "snapshot_from_json",
    "snapshot_to_json",
]
