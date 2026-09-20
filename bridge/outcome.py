"""What the bridge is allowed to claim about a finished run.

Kept separate from ``bridge.main`` so the acceptance suite can test the claim
without importing the polling loop (which needs ``yaml`` and a live config).
"""
from __future__ import annotations

import re

#: Matched as stems anchored at a word start. Two reasons, both learned the hard
#: way: a plain substring test read ``"incomplete"`` as a victory (it contains
#: ``"complete"``), while a whole-word test stopped reading ``"Victory"`` as one at
#: all because ``\bvictor\b`` cannot match it. Anchoring at the word start keeps
#: every inflection (victory/victors, triumphed, cleared, perish/ished) and still
#: refuses ``incomplete``, whose ``complet`` is not at a word boundary.
#: The vocabulary is deliberately not enlarged: these live message strings have
#: never been observed here (the game is not running), and every speculative word
#: adds false-positive surface to the one claim that must not be wrong.
_WIN_STEMS = ("victor", "triumph", "won", "win", "ascend", "clear", "complet",
              "escap")
#: Death wording only, as originally scoped. "abandon" and "fail" are deliberately
#: absent: quitting a run mid-Act is not evidence that the player lost, and the
#: test for "Run abandoned" pins that case to undetermined on purpose.
_LOSS_STEMS = ("die", "death", "defeat", "lost", "slain", "fell", "fall", "peris")
#: Downgrade a victory reading -- never a defeat reading -- when the message says
#: the run is still going or that the clear is partial.
_AMBIGUOUS_MARKERS = ("incomplete", "not cleared", "run continues", "but the")

_WIN_RE = tuple(re.compile(rf"\b{re.escape(stem)}") for stem in _WIN_STEMS)
_LOSS_RE = tuple(re.compile(rf"\b{re.escape(stem)}") for stem in _LOSS_STEMS)


def run_outcome(state: dict) -> bool | None:
    """``True`` won, ``False`` lost, ``None`` undetermined.

    A bridge that publishes the game's own victory room reads that flag and
    nothing else.  STS2MCP 0.4.0 does not: ``McpMod.StateBuilder.cs:457`` hard
    codes ``message = "Run ended."`` for both outcomes, so on the locked bridge
    the producer's wording is the only evidence available and it can never say
    "won".  Reaching this screen with HP left is deliberately not treated as a
    victory either: abandoning a run mid-Act also ends alive, and an inferred
    "cleared A10" is exactly the false claim this project cannot take back.

    Win wording is considered before loss wording, but a message carrying *both*
    polarities, or one that walks a victory back, is reported undetermined rather
    than resolved by precedence: with two bosses in the final act, an intermediate
    act-clear screen must not be promoted into a run clear.
    """

    game_over = state.get("game_over")
    if not isinstance(game_over, dict):
        game_over = {}
    explicit = game_over.get("is_victory")
    if isinstance(explicit, bool):
        return explicit
    message = str(game_over.get("message") or "").lower()
    if not message:
        return None
    win_hit = any(pattern.search(message) for pattern in _WIN_RE)
    loss_hit = any(pattern.search(message) for pattern in _LOSS_RE)
    if win_hit and (loss_hit or any(m in message for m in _AMBIGUOUS_MARKERS)):
        return None
    if win_hit:
        return True
    if loss_hit:
        return False
    return None


def outcome_source(state: dict) -> str:
    """Name the evidence a victory reading rested on, so its absence is visible.

    ``no_victory_signal`` and ``game_over_message_wording`` both mean the bridge
    could not tell us the outcome structurally; only a run carrying
    ``bridge_is_victory_flag`` is real victory evidence.
    """
    game_over = state.get("game_over")
    if not isinstance(game_over, dict):
        game_over = {}
    if isinstance(game_over.get("is_victory"), bool):
        return "bridge_is_victory_flag"
    if str(game_over.get("message") or ""):
        return "game_over_message_wording"
    return "no_victory_signal"


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
