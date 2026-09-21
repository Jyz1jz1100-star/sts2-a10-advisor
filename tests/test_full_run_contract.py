"""The eleven-item full-run gate must be able to pass a run that earned it.

These are synthetic traces, not replays: they exist so that a lookup which can
only ever return None -- as the ancient check did, comparing int acts against the
str keys the JSON artifact carries -- fails here instead of silently blocking every
real acceptance attempt.  The passing fixture is deliberately the *whole* contract
at once, so no item can be satisfied by a hole in another.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.verify_full_run_contract import audit as audit_trace


def _state(sequence, state_type, act, floor, hp=60, **extra):
    state = {
        "state_type": state_type,
        "run": {"act": act, "floor": floor, "ascension": 10},
        "player": {"hp": hp, "max_hp": 80, "character_id": "IRONCLAD"},
    }
    state.update(extra)
    return {"event_type": "state", "raw": state, "sequence": sequence}


def _action(sequence, **payload):
    return {"event_type": "action", "raw": payload, "sequence": sequence}


def _boss(sequence, act, floor):
    return _state(
        sequence, "boss", act, floor,
        battle={"enemies": [{"id": f"ENEMY_{act}_{floor}"}]},
    )


def _ancient(sequence, act, floor, event_id):
    return _state(
        sequence, "event", act, floor,
        event={"event_id": event_id, "is_ancient": True, "options": []},
    )


def _result(sequence, status="ok", **extra):
    raw = {"status": status}
    raw.update(extra)
    return {"event_type": "result", "raw": raw, "sequence": sequence}


SESSION = {
    "event_type": "session",
    "raw": {
        "observed_game": {"version": "v0.111.0", "steam_build_id": "24724944"},
        "observed_mods": ["CombatSolver", "STS2-RitsuLib", "STS2_MCP"],
        "execution_owner": "combat_solver_full_auto",
        "cohort": "assisted",
        "seed_mode": "observational",
    },
    "sequence": 0,
}

TERMINAL = {
    "event_type": "state",
    "raw": {
        "state_type": "game_over",
        "run": {"act": 3, "floor": 49, "ascension": 10},
        "player": {"hp": 0, "max_hp": 80, "character_id": "IRONCLAD"},
        "game_over": {"is_victory": True, "message": "Run ended."},
    },
    "sequence": 30,
}


def _resequence(records: list[dict], offset: int) -> list[dict]:
    out = []
    for record in records:
        copy = dict(record)
        copy["sequence"] = record["sequence"] + offset
        out.append(copy)
    return out


def full_victory_run(first_floor: int = 1) -> list[dict]:
    """Floor 1 -> three acts, three ancients, 1+1+2 bosses, the game's victory.

    Every screen carries the action that moved it on and an acknowledged result:
    the legality item requires at least one posted action, so a fixture of bare
    states would attest nothing.
    """
    return [
        SESSION,
        _state(1, "map", 1, first_floor),
        _ancient(2, 1, 1, "NEOW"),
        _action(3, action="choose_event_option", index=0),
        _result(4),
        _boss(5, 1, 17),
        _action(6, action="end_turn"),
        _result(7),
        _state(8, "rewards", 1, 17),
        _action(9, action="proceed"),
        _state(10, "map", 2, 18),
        _ancient(11, 2, 18, "PAEL"),
        _action(12, action="choose_event_option", index=0),
        _result(13),
        _boss(14, 2, 33),
        _action(15, action="end_turn"),
        _result(16),
        _state(17, "card_select", 2, 33),
        _state(18, "map", 3, 34),
        _ancient(19, 3, 34, "NONUPEIPE"),
        _action(20, action="choose_event_option", index=0),
        _result(21),
        _boss(22, 3, 48),
        _action(23, action="end_turn"),
        _result(24),
        _state(25, "map", 3, 48),
        _boss(26, 3, 49),
        _action(27, action="end_turn"),
        _result(28),
        TERMINAL,
    ]


def failed_checks(best_run) -> list[str]:
    return [c["check"] for c in best_run["checks"] if not c["passed"]]


class FullRunContractTests(unittest.TestCase):
    def audit(self, records) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.jsonl"
            path.write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
                encoding="utf-8",
            )
            return audit_trace(path)

    def test_a_complete_run_satisfies_all_eleven_items(self) -> None:
        report = self.audit(full_victory_run())
        best = report["best_run"]
        self.assertIsNotNone(best, report)
        self.assertEqual(failed_checks(best), [], [c["detail"] for c in best["checks"]])
        self.assertTrue(report["contract_satisfied"])

    def test_eleven_items_are_all_present(self) -> None:
        best = self.audit(full_victory_run())["best_run"]
        self.assertEqual(len(best["checks"]), 11)

    def test_an_ancient_from_the_wrong_act_is_rejected(self) -> None:
        """Act 1 is Neow alone, act 2 Orobas/Pael/Tezcatara, act 3 Nonupeipe/Tanx/Vakuu.

        Darv is allowed anywhere because it is the *shared* ancient every act may
        draw (ActModel.cs:344-347 with ModelDb.cs:152-153), which is why the
        per-act sets are not singletons.
        """
        records = [
            _ancient(r.get("sequence"), 2, 18, "NEOW")
            if r.get("raw", {}).get("event", {}).get("is_ancient")
            and r["raw"]["run"]["act"] == 2
            else r
            for r in full_victory_run()
        ]
        best = self.audit(records)["best_run"]
        # Both the named item and the run's own composite definition refuse: the
        # second one reads the same act->Ancient requirement, so a run cannot be
        # "complete" by its own lights while carrying the wrong act's Ancient.
        self.assertEqual(
            failed_checks(best),
            ["three_ancients_are_this_builds", "run_complete_by_its_own_definition"],
        )

    def test_a_run_joined_mid_run_cannot_attest_the_floors_it_never_walked(self) -> None:
        records = list(full_victory_run())
        records[1] = _state(1, "map", 1, 12)
        best = self.audit(records)["best_run"]
        self.assertIn("starts_at_floor_one", failed_checks(best))
        self.assertFalse(self.audit(records)["contract_satisfied"])

    def test_a_death_at_the_last_boss_is_not_a_clear(self) -> None:
        records = full_victory_run()
        records = [dict(r) for r in full_victory_run()]
        records[-1] = json.loads(json.dumps(TERMINAL))
        records[-1]["raw"]["game_over"] = {"is_victory": False, "message": "Run ended."}
        best = self.audit(records)["best_run"]
        self.assertIn("terminal_is_the_games_victory_flag", failed_checks(best))
        self.assertIn("run_complete_by_its_own_definition", failed_checks(best))

    def test_a_boss_survived_only_up_to_a_menu_is_not_cleared(self) -> None:
        """The win is the terminal's claim; a menu after a boss proves nothing.

        This is the false positive the contract had to be written against: the
        build flags victory and then kills the party, so "still alive" can never
        certify the last boss, and "some screen followed" certifies too little.
        """
        records = full_victory_run()[:-1]
        records.append(_state(31, "menu", 3, 49, hp=0, menu_screen="main"))
        best = self.audit(records)["best_run"]
        self.assertIn("terminal_is_the_games_victory_flag", failed_checks(best))

    def test_a_refused_action_is_charged_to_its_own_run(self) -> None:
        records = list(full_victory_run())
        records.insert(4, _result(900, "error", error="No event options available"))
        best = self.audit(records)["best_run"]
        self.assertIn("every_action_was_legal_and_acked", failed_checks(best))

    def test_a_clean_run_is_not_disqualified_by_a_neighbour(self) -> None:
        """Records are segmented per run, so legality cannot leak across runs."""
        dirty_tail = [
            _state(300, "map", 1, 1),
            _result(301, "error", error="Map screen is not open"),
            _state(302, "game_over", 1, 1, hp=0,
                   game_over={"is_victory": False, "message": "Run ended."}),
        ]
        report = self.audit(full_victory_run() + dirty_tail)
        self.assertTrue(report["contract_satisfied"], report["all_runs"])


    def test_a_fresh_start_observed_before_the_first_node_is_floor_one_eligible(self) -> None:
        """A run captured at Neow reports floor 0, which is earlier than floor 1.

        Only a saved run can report a floor above 1 at its first act frame, so
        accepting 0 cannot admit a mid-run continuation -- while requiring exactly
        1 disqualified the fresh runs, which is how a second run in a batch could
        never attest a start it had genuinely played from the beginning.
        """
        best = self.audit(full_victory_run(first_floor=0))["best_run"]
        self.assertEqual(failed_checks(best), [])

    def test_a_later_run_in_the_same_file_still_carries_the_streams_provenance(self) -> None:
        """The session record sits once per file; every run in it was played there."""
        first = full_victory_run()
        second = _resequence(full_victory_run(), 100)
        report = self.audit(first + second)
        self.assertEqual(report["runs_evaluated"], 2)
        self.assertTrue(report["contract_satisfied"], report["all_runs"])
        best = report["best_run"]
        self.assertIn("provenance_bound_to_the_locked_build",
                      [c["check"] for c in best["checks"]])
        self.assertEqual(failed_checks(best), [])


if __name__ == "__main__":
    unittest.main()
