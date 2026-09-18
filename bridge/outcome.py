"""What the bridge is allowed to claim about a finished run.

Kept separate from ``bridge.main`` so the acceptance suite can test the claim
without importing the polling loop (which needs ``yaml`` and a live config).
"""
from __future__ import annotations

_WIN_WORDS = ("victor", "triumph", " win", "won", "ascend", "cleared", "complete", "escape")
_LOSS_WORDS = ("died", "death", "defeat", "lost", "slain", "fell", "perish")


def run_outcome(state: dict) -> bool | None:
    """``True`` won, ``False`` lost, ``None`` undetermined.

    STS2MCP publishes no win/loss flag, so the producer's own ``game_over``
    message is the only evidence. Reaching the screen with HP left is deliberately
    not treated as a victory: abandoning a run mid-Act also ends alive, and an
    inferred "cleared A10" is exactly the false claim this project cannot take
    back.
    """

    message = str(((state.get("game_over") or {}).get("message")) or "").lower()
    if any(word in message for word in _WIN_WORDS):
        return True
    if any(word in message for word in _LOSS_WORDS):
        return False
    return None


def game_over_message(state: dict) -> str:
    """Human-facing acknowledgement of a finished run, labelled with what we know."""

    game_over = state.get("game_over") or {}
    run = state.get("run") or {}
    player = state.get("player") or {}
    char = str(player.get("character") or "?").replace("The ", "")
    act = run.get("act", "?")
    floor = run.get("floor", "?")
    detail = game_over.get("message") or "The run has ended."
    outcome = run_outcome(state)

    if outcome:
        return (f"[ run complete ]\n🏆 VICTORY — {char}! You beat the Spire.\n"
                f"Reached Act {act}. GG — start a new run when ready.")
    if outcome is False:
        return (f"[ run over ]\n💀 Defeated — {char}, Act {act}, Floor {floor}.\n{detail}")
    return (f"[ run over ]\n❓ Outcome undetermined — {char}, Act {act}, Floor {floor}.\n"
            f"No win or loss word in the producer's message, so this is not evidence of a "
            f"clear. {detail}")
