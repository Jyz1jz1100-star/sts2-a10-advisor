"""What a real run actually covered, derived only from the observed state stream.

The acceptance target is a three-act run with each act's Ancient and 1+1+2 bosses
ending on the game's own victory screen.  Nothing in the live path could say
whether a batch did that: the summary counted actions, not flow.  This module
reads a stream of states (from a running game or a recorded trace) and reports
which required steps were *seen*, so an unvisited Ancient or an unfought second
boss shows up as a gap instead of being averaged into a win rate.

Every fact here is trace-derived.  Floor counts and act numbers are never used
to infer that a boss was beaten or that a run was won.
"""
from __future__ import annotations

from typing import Any, Iterable

from bridge.outcome import outcome_source, run_outcome

#: The build's canonical progression is Overgrowth -> Hive -> Glory, so a
#: complete run is three acts, one Ancient each, and bosses 1 + 1 + 2.
REQUIRED_ACTS = (1, 2, 3)
REQUIRED_BOSSES_PER_ACT = {1: 1, 2: 1, 3: 2}

_COMBAT_SCREENS = frozenset({"monster", "elite", "boss", "hand_select"})

#: Screens the out-of-combat driver has a content-specific rule for.  Anything
#: else that gets dismissed by the generic proceed is an unmodelled screen.
RULED_SCREENS = frozenset(
    _COMBAT_SCREENS
    | {
        "card_reward", "shop", "rest_site", "map", "treasure", "relic_select",
        "bundle_select", "rewards", "event", "card_select", "menu", "game_over",
        "fake_merchant",
    }
)


def _run_field(state: dict, key: str):
    run = state.get("run")
    return run.get(key) if isinstance(run, dict) else None


class RunCoverage:
    """Accumulates one run's out-of-combat flow from successive states."""

    def __init__(self) -> None:
        self.acts_seen: set[int] = set()
        #: False until a state from an in-progress run is seen.  The menu reports
        #: act 1 / floor 0 with no run behind it, which is not a visited act.
        self.started = False
        #: True once the terminal screen has been read; further polls of that
        #: same screen are ignored until the caller begins the next run.
        self.closed = False
        #: act -> ancient event id, from a state whose event carries is_ancient
        self.ancients: dict[int, str] = {}
        #: (act, floor) -> sorted enemy entity ids seen in that boss battle
        self.boss_battles: dict[tuple[int, int], tuple[str, ...]] = {}
        #: (act, floor) -> what the game showed next while the player lived
        self.boss_cleared: dict[tuple[int, int], str] = {}
        #: act -> floor where an act change to that act was observed
        self.act_entries: dict[int, int] = {}
        #: every generic proceed, with the screen and position it dismissed
        self.proceed_bypasses: list[dict[str, Any]] = []
        #: screens that had no rule at all, keyed by type, from the first frame
        self.unhandled_screens: dict[str, list[dict[str, Any]]] = {}
        #: prompts the driver left to the in-game combat owner, by screen type
        self.deferred_to_combat: dict[str, int] = {}
        #: frames the driver held off on because the client is mid-transition
        self.waits_for_transition: dict[str, int] = {}
        #: state_types that were seen but have no out-of-combat rule
        self.unknown_screens: dict[str, int] = {}
        self.outcome: bool | None = None
        self.outcome_source: str | None = None
        self.terminal_floor: int | None = None
        self._pending_boss: tuple[int, int] | None = None

    # ------------------------------------------------------------------ input

    def observe(self, state: dict, *, action: dict | None = None) -> None:
        if not isinstance(state, dict) or self.closed:
            # The terminal screen persists across several one-second polls, so
            # once a run is finished every further frame of it is ignored; the
            # caller starts a new ledger for the next run.
            return
        state_type = str(state.get("state_type") or "unknown")
        act, floor = _run_field(state, "act"), _run_field(state, "floor")
        if isinstance(act, int) and state_type not in ("menu", "game_over"):
            # The main menu still reports act 1 / floor 0 from the last run's
            # shell, and a terminal screen repeats on every poll; counting
            # either splits one real run into phantom ones.
            self.started = True
            self.acts_seen.add(act)
            self.act_entries.setdefault(act, floor if isinstance(floor, int) else 0)

        event = state.get("event")
        if isinstance(event, dict) and event.get("is_ancient") and isinstance(act, int):
            event_id = str(event.get("event_id") or "?").upper()
            if event_id != "NEOW" or act == 1:
                # Neow is act 1's Ancient; a later act's Ancient is a different
                # model, and the run-start Neow must not be counted twice.
                self.ancients.setdefault(act, event_id)

        if state_type == "boss" and isinstance(act, int) and isinstance(floor, int):
            self._record_battle((act, floor), state)
            self._pending_boss = (act, floor)
        elif state_type in _COMBAT_SCREENS:
            pass  # hand_select and friends are inside the same fight; see below
        elif state_type == "game_over":
            pass  # the terminal decides for itself, since a win is recorded at HP 0
        elif self._pending_boss is not None:
            # Only *leaving the node* settles a boss.  Cancelling on an in-combat
            # prompt instead dropped beaten bosses from the record, which
            # understates coverage and would make the full-run gate unpassable
            # for reasons that have nothing to do with the run.
            player = state.get("player") or {}
            if self._player_alive(state):
                # The label names the screen that followed and the HP carried into
                # it; it used to read "floor" here while printing HP, which invited
                # exactly the wrong inference about where the boss sat.
                self.boss_cleared.setdefault(
                    self._pending_boss,
                    f"{state_type}:hp{player.get('hp', '?')}",
                )
            self._pending_boss = None

        if state_type == "game_over":
            self._finish(state)

        if action is not None and str(action.get("action") or "") == "proceed":
            self.note_bypass(state_type, act, floor, reason=str(action.get("_reason") or ""))

    def note_deferred_to_combat(self, screen_type: str) -> None:
        """Record a prompt deliberately left to the Combat Solver.

        Abstaining is correct for some screens and a silent gap for others, so
        the two must not look alike in the evidence.
        """
        key = str(screen_type)
        self.deferred_to_combat[key] = self.deferred_to_combat.get(key, 0) + 1

    def note_wait_for_transition(self, reason: str) -> None:
        """Record a frame the driver deliberately held off on.

        A held frame is neither an illegal post nor a skipped screen, but a run
        that spent most of its time holding has to be able to say so.
        """
        key = str(reason)
        self.waits_for_transition[key] = (
            self.waits_for_transition.get(key, 0) + 1
        )

    def note_unhandled(self, state_type: str, act, floor) -> None:
        """Record a screen with no rule the moment it is seen.

        Waiting 300 s before saying anything turned a missing handler into what
        looked like a hang.  The first frame is the report; the budget on top of
        it belongs to the caller.
        """
        self.unhandled_screens.setdefault(state_type, []).append(
            {"act": act, "floor": floor}
        )

    def note_bypass(self, state_type: str, act, floor, *, reason: str = "") -> None:
        """Record a screen the driver left without a content-specific rule.

        Every screen is recorded, including the ordinary ones: a run that never
        shows a reward or map screen has a flow problem, and an empty list is
        only informative because nothing is filtered out of it.
        """
        self.proceed_bypasses.append(
            {"state_type": state_type, "act": act, "floor": floor, "reason": reason}
        )

    def note_unknown_screen(self, state_type: str) -> None:
        if state_type != "unknown":
            return
        self.unknown_screens["unknown"] = self.unknown_screens.get("unknown", 0) + 1

    def _finish(self, state: dict) -> None:
        self.outcome = run_outcome(state)
        self.outcome_source = outcome_source(state)
        floor = _run_field(state, "floor")
        self.terminal_floor = floor if isinstance(floor, int) else None
        if self.outcome is True and self._pending_boss is not None:
            # The build records a win by flagging victory and then killing the
            # party (RunManager.cs:1238-1246), so the terminal screen proves the
            # boss it followed even though the alive-player test below cannot.
            self.boss_cleared[self._pending_boss] = "victory_terminal"
            self._pending_boss = None
        self.closed = True

    def _record_battle(self, key: tuple[int, int], state: dict) -> None:
        battle = state.get("battle") or {}
        enemies = battle.get("enemies") if isinstance(battle, dict) else None
        ids = sorted(
            {
                str(enemy.get("entity_id"))
                for enemy in (enemies or [])
                if isinstance(enemy, dict) and enemy.get("entity_id")
            }
        )
        if ids:
            self.boss_battles[key] = tuple(ids)
        else:  # a frame before the mod fills the roster still proves the node
            self.boss_battles.setdefault(key, ())

    @staticmethod
    def _player_alive(state: dict) -> bool:
        player = state.get("player") or {}
        hp = player.get("hp") if isinstance(player, dict) else None
        return isinstance(hp, (int, float)) and hp > 0

    # ----------------------------------------------------------------- output

    def bosses_cleared_by_act(self) -> dict[int, int]:
        counts: dict[int, int] = {}
        for (act, _floor) in self.boss_cleared:
            counts[act] = counts.get(act, 0) + 1
        return counts

    def coverage(self) -> dict[str, Any]:
        cleared = self.bosses_cleared_by_act()
        acts_covered = [a for a in REQUIRED_ACTS if a in self.acts_seen]
        ancients_covered = [a for a in REQUIRED_ACTS if a in self.ancients]
        bosses_covered = [
            a for a in REQUIRED_ACTS if cleared.get(a, 0) >= REQUIRED_BOSSES_PER_ACT[a]
        ]
        return {
            "schema_version": 1,
            "started": self.started,
            "acts_seen": sorted(self.acts_seen),
            "acts_in_required_progression": acts_covered,
            "ancients_by_act": {str(k): v for k, v in sorted(self.ancients.items())},
            "ancients_covered_acts": ancients_covered,
            "boss_battles_by_act_floor": {
                f"{act}:{floor}": list(ids)
                for (act, floor), ids in sorted(self.boss_battles.items())
            },
            "bosses_cleared_by_act": {str(k): v for k, v in sorted(cleared.items())},
            # Which boss *nodes* were cleared, not just how many: a count cannot
            # tell "cleared Act 3's first boss" from "cleared its second".
            "bosses_cleared": {
                f"{act}:{floor}": how
                for (act, floor), how in sorted(self.boss_cleared.items())
            },
            "bosses_covered_acts": bosses_covered,
            "required_bosses_per_act": REQUIRED_BOSSES_PER_ACT,
            "outcome": self.outcome,
            "outcome_source": self.outcome_source,
            "terminal_floor": self.terminal_floor,
            "proceed_bypasses": list(self.proceed_bypasses),
            "unhandled_screens": {
                key: list(frames) for key, frames in sorted(self.unhandled_screens.items())
            },
            "unhandled_screen_bypasses": [
                bypass for bypass in self.proceed_bypasses
                if bypass["state_type"] not in RULED_SCREENS
            ],
            "unknown_screens": dict(self.unknown_screens),
            "deferred_to_combat": dict(self.deferred_to_combat),
            "waits_for_transition": dict(self.waits_for_transition),
            # The one claim that must not be wrong: a run is complete only on the
            # game's own victory terminal with all three acts, all three ancients
            # and four boss clears in the same continuous stream.
            "run_complete": (
                self.outcome is True
                and len(acts_covered) == len(REQUIRED_ACTS)
                and len(ancients_covered) == len(REQUIRED_ACTS)
                and len(bosses_covered) == len(REQUIRED_ACTS)
            ),
        }


def coverage_from_states(states: Iterable[dict]) -> RunCoverage:
    ledger = RunCoverage()
    for state in states:
        ledger.observe(state)
    return ledger
