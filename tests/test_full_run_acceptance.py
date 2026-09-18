"""Offline fixture tests for the real-game full-run acceptance ledger."""
from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from scripts.assess_full_run import (
    _load_lock,
    analyze_traces,
    build_plan,
    build_report,
    load_trace,
)


ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "config" / "live_version.lock.json"
LOCK = _load_lock(LOCK_PATH)


def _event(kind: str, sequence: int, raw: dict, **fields: object) -> dict:
    return {
        "schema_version": 1,
        "sequence": sequence,
        "timestamp_utc": f"2026-09-02T00:00:{sequence:02d}.000Z",
        "event_type": kind,
        "raw": raw,
        **fields,
    }


def _state(run_id: str, *, act: int, floor: int, state_type: str = "map") -> dict:
    return {
        "state_type": state_type,
        "run": {
            "run_id": run_id,
            "seed": f"seed-{run_id}",
            "act": act,
            "floor": floor,
            "ascension": 10,
            "game_mode": "standard",
        },
        "player": {
            "character_id": "IRONCLAD",
            "character": "The Ironclad",
            "hp": 70,
            "max_hp": 80,
        },
        "map": {"next_options": [{"index": 0, "type": "Monster"}]},
    }


def _valid_trace(run_count: int = 1, *, include_session: bool = True) -> list[dict]:
    events: list[dict] = []
    sequence = 0
    if include_session:
        events.append(
            _event(
                "session",
                sequence,
                {
                    "cohort": "assisted",
                    "observed_game": dict(LOCK["game"]),
                    "observed_mods": ["STS2_MCP"],
                    "model_id": "trained-test",
                },
            )
        )
        sequence += 1
    for index in range(run_count):
        run_id = f"run-{index}"
        seed = f"seed-{run_id}"
        events.append(
            _event(
                "run_identity",
                sequence,
                {
                    "run_id": run_id,
                    "seed": seed,
                    "character": "IRONCLAD",
                    "ascension": 10,
                    "game_mode": "standard",
                },
            )
        )
        sequence += 1
        before = _state(run_id, act=1, floor=1)
        events.append(_event("state", sequence, before, decision_id=f"d-{index}"))
        sequence += 1
        events.append(
            _event(
                "action",
                sequence,
                {"action": "choose_map_node", "index": 0},
                decision_id=f"d-{index}",
                state_type="map",
            )
        )
        sequence += 1
        events.append(
            _event(
                "result",
                sequence,
                {"status": "ok"},
                decision_id=f"d-{index}",
            )
        )
        sequence += 1
        # Seeing Act 2 and Act 3 is the observable boundary evidence for those
        # act-survival metrics; the explicit terminal message is the only run
        # win evidence.
        events.append(_event("state", sequence, _state(run_id, act=2, floor=15), decision_id=f"a2-{index}"))
        sequence += 1
        events.append(_event("state", sequence, _state(run_id, act=3, floor=30), decision_id=f"a3-{index}"))
        sequence += 1
        terminal = _state(run_id, act=3, floor=50, state_type="game_over")
        terminal["game_over"] = {"message": "Victory — you beat the Spire."}
        events.append(_event("state", sequence, terminal, decision_id=f"end-{index}"))
        sequence += 1
    return events


def _write_trace(path: Path, events: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )


class FullRunLedgerTests(unittest.TestCase):
    def test_valid_full_run_has_wilson_and_act_survival_but_is_not_formal_at_one_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "trace.jsonl"
            _write_trace(path, _valid_trace())
            trace = load_trace(path)
            runs, quality = analyze_traces(
                [trace], cohort="assisted", seed_mode="observational"
            )
            report = build_report(
                runs,
                quality,
                cohort="assisted",
                seed_mode="observational",
                pilot_runs=20,
                formal_runs=500,
                lock=LOCK,
                model_id="trained-test",
            )
        self.assertEqual(report["counts"]["full_run_wins"], 1)
        self.assertEqual(report["counts"]["runs"], 1)
        self.assertEqual(report["full_run_win_rate"]["rate"], 1.0)
        self.assertIsNotNone(report["full_run_win_rate"]["wilson_95_low"])
        self.assertTrue(report["act_survival"]["act_1"]["rate"] == 1.0)
        self.assertTrue(report["act_survival"]["act_2"]["rate"] == 1.0)
        self.assertTrue(report["act_survival"]["act_3"]["rate"] == 1.0)
        self.assertFalse(report["verdict"]["accepted"])
        self.assertIn("minimum_formal_runs", report["gates"])

    def test_trace_session_provenance_is_authoritative_for_game_and_mod_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "provenance.jsonl"
            _write_trace(path, _valid_trace())
            trace = load_trace(path)
            runs, quality = analyze_traces(
                [trace], cohort="assisted", seed_mode="observational"
            )
            report = build_report(
                runs,
                quality,
                cohort="assisted",
                seed_mode="observational",
                pilot_runs=1,
                formal_runs=500,
                lock=LOCK,
                model_id="trained-test",
            )
        blockers = report["verdict"]["blockers"]
        self.assertNotIn("game_identity_missing_or_mismatch", blockers)
        self.assertNotIn("allowed_mod_inventory_missing_or_mismatch", blockers)
        # The fixture intentionally omits artifact files; those independent
        # provenance preconditions must remain fail-closed.
        self.assertIn("checkpoint_hash_missing", blockers)
        self.assertIn("data_hash_missing", blockers)

    def test_session_artifact_hashes_are_retained_for_offline_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            checkpoint = Path(temp) / "policy.pt"
            manifest = Path(temp) / "manifest.json"
            checkpoint.write_bytes(b"checkpoint fixture")
            manifest.write_bytes(b"manifest fixture")
            events = _valid_trace()
            events[0]["raw"].update(
                {
                    "execution_owner": "http_route_executor",
                    "bridge_observed": {
                        "status": "ok",
                        "message": "STS2MCP v0.4.0 ready",
                    },
                    "checkpoint": str(checkpoint),
                    "checkpoint_sha256": hashlib.sha256(
                        checkpoint.read_bytes()
                    ).hexdigest(),
                    "data_manifest": str(manifest),
                    "data_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                }
            )
            path = Path(temp) / "artifact-provenance.jsonl"
            _write_trace(path, events)
            trace = load_trace(path)
            runs, quality = analyze_traces(
                [trace], cohort="assisted", seed_mode="observational"
            )
            report = build_report(
                runs,
                quality,
                cohort="assisted",
                seed_mode="observational",
                pilot_runs=1,
                formal_runs=500,
                lock=LOCK,
            )
        blockers = report["verdict"]["blockers"]
        self.assertNotIn("model_id_missing", blockers)
        self.assertNotIn("checkpoint_hash_missing", blockers)
        self.assertNotIn("data_hash_missing", blockers)
        self.assertTrue(report["provenance"]["checkpoint_hash_verified"])
        self.assertTrue(report["provenance"]["data_hash_verified"])

    def test_one_missing_trace_provenance_cannot_be_masked_by_a_valid_trace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            checkpoint = Path(temp) / "policy.pt"
            manifest = Path(temp) / "manifest.json"
            checkpoint.write_bytes(b"checkpoint fixture")
            manifest.write_bytes(b"manifest fixture")
            complete = _valid_trace()
            complete[0]["raw"].update(
                {
                    "execution_owner": "http_route_executor",
                    "bridge_observed": {
                        "status": "ok",
                        "message": "STS2MCP v0.4.0 ready",
                    },
                    "checkpoint": str(checkpoint),
                    "checkpoint_sha256": hashlib.sha256(
                        checkpoint.read_bytes()
                    ).hexdigest(),
                    "data_manifest": str(manifest),
                    "data_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                }
            )
            missing = _valid_trace()
            for key in (
                "observed_game",
                "observed_mods",
                "model_id",
                "bridge_observed",
                "checkpoint",
                "checkpoint_sha256",
                "data_manifest",
                "data_sha256",
            ):
                missing[0]["raw"].pop(key, None)
            complete_path = Path(temp) / "complete.jsonl"
            missing_path = Path(temp) / "missing.jsonl"
            _write_trace(complete_path, complete)
            _write_trace(missing_path, missing)
            traces = [load_trace(complete_path), load_trace(missing_path)]
            runs, quality = analyze_traces(
                traces, cohort="assisted", seed_mode="observational"
            )
            report = build_report(
                runs,
                quality,
                cohort="assisted",
                seed_mode="observational",
                pilot_runs=1,
                formal_runs=500,
                lock=LOCK,
            )
        blockers = report["verdict"]["blockers"]
        self.assertEqual(report["provenance"]["trace_count"], 2)
        self.assertEqual(len(report["provenance"]["trace_reports"]), 2)
        self.assertIn("game_identity_missing_or_mismatch", blockers)
        self.assertIn("allowed_mod_inventory_missing_or_mismatch", blockers)
        self.assertIn("bridge_identity_missing_or_mismatch", blockers)
        self.assertIn("model_id_missing", blockers)
        self.assertIn("checkpoint_hash_missing", blockers)
        self.assertIn("data_hash_missing", blockers)

    def test_conflicting_trace_provenance_is_not_hidden_by_the_first_trace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            checkpoint = Path(temp) / "policy.pt"
            manifest = Path(temp) / "manifest.json"
            checkpoint.write_bytes(b"checkpoint fixture")
            manifest.write_bytes(b"manifest fixture")
            first = _valid_trace()
            first[0]["raw"].update(
                {
                    "execution_owner": "http_route_executor",
                    "bridge_observed": {
                        "status": "ok",
                        "message": "STS2MCP v0.4.0 ready",
                    },
                    "checkpoint": str(checkpoint),
                    "checkpoint_sha256": hashlib.sha256(
                        checkpoint.read_bytes()
                    ).hexdigest(),
                    "data_manifest": str(manifest),
                    "data_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                }
            )
            second = json.loads(json.dumps(first))
            second[0]["raw"]["observed_game"]["steam_build_id"] = "stale-build"
            second[0]["raw"]["model_id"] = "different-trained-model"
            first_path = Path(temp) / "first.jsonl"
            second_path = Path(temp) / "second.jsonl"
            _write_trace(first_path, first)
            _write_trace(second_path, second)
            runs, quality = analyze_traces(
                [load_trace(first_path), load_trace(second_path)],
                cohort="assisted",
                seed_mode="observational",
            )
            report = build_report(
                runs,
                quality,
                cohort="assisted",
                seed_mode="observational",
                pilot_runs=1,
                formal_runs=500,
                lock=LOCK,
            )
        blockers = report["verdict"]["blockers"]
        self.assertIn("game_identity_missing_or_mismatch", blockers)
        self.assertIn("game_provenance_inconsistent", blockers)
        self.assertIn("model_provenance_inconsistent", blockers)

    def test_producer_only_artifact_hashes_are_unverified_blockers(self) -> None:
        events = _valid_trace()
        events[0]["raw"].update(
            {
                "execution_owner": "http_route_executor",
                "bridge_observed": {
                    "status": "ok",
                    "message": "STS2MCP v0.4.0 ready",
                },
                "checkpoint_sha256": "a" * 64,
                "data_sha256": "b" * 64,
            }
        )
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bare-hashes.jsonl"
            _write_trace(path, events)
            runs, quality = analyze_traces(
                [load_trace(path)], cohort="assisted", seed_mode="observational"
            )
            report = build_report(
                runs,
                quality,
                cohort="assisted",
                seed_mode="observational",
                pilot_runs=1,
                formal_runs=500,
                lock=LOCK,
            )
        blockers = report["verdict"]["blockers"]
        self.assertFalse(report["provenance"]["checkpoint_hash_verified"])
        self.assertFalse(report["provenance"]["data_hash_verified"])
        self.assertIn("checkpoint_hash_unverified", blockers)
        self.assertIn("data_hash_unverified", blockers)

    def test_execution_owner_is_required_and_keeper_is_not_an_owner(self) -> None:
        events = _valid_trace()
        events[0]["raw"]["execution_watchdogs"] = ["fullauto_keeper"]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "missing-owner.jsonl"
            _write_trace(path, events)
            runs, quality = analyze_traces(
                [load_trace(path)], cohort="assisted", seed_mode="observational"
            )
            report = build_report(
                runs,
                quality,
                cohort="assisted",
                seed_mode="observational",
                pilot_runs=1,
                formal_runs=500,
                lock=LOCK,
                model_id="trained-test",
            )
        self.assertEqual(report["execution"]["watchdogs"], ["fullauto_keeper"])
        self.assertIsNone(report["execution"]["owner"])
        self.assertIn("execution_owner_missing", report["verdict"]["blockers"])

    def test_two_execution_owners_are_a_hard_blocker(self) -> None:
        events = _valid_trace()
        events[0]["raw"]["execution_owners"] = [
            "http_route_executor",
            "combat_solver_full_auto",
        ]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "dual-owner.jsonl"
            _write_trace(path, events)
            runs, quality = analyze_traces(
                [load_trace(path)], cohort="assisted", seed_mode="observational"
            )
            report = build_report(
                runs,
                quality,
                cohort="assisted",
                seed_mode="observational",
                pilot_runs=1,
                formal_runs=500,
                lock=LOCK,
                model_id="trained-test",
            )
        self.assertEqual(
            report["execution"]["owners"],
            ["combat_solver_full_auto", "http_route_executor"],
        )
        self.assertIn("execution_owner_conflict", report["verdict"]["blockers"])
        self.assertFalse(report["gates"]["execution_owner_unique"]["passed"])

    def test_fullauto_uses_deploy_log_without_http_combat_coverage_penalty(self) -> None:
        from tests.test_deploy_evidence import _record, _write_journal
        from scripts.assess_full_run import merge_comparison_evidence

        events = _valid_trace()
        events[0]["raw"]["execution_owner"] = "combat_solver_full_auto"
        terminal = events[-1]
        terminal["sequence"] = 9
        events.extend(
            [
                _event(
                    "state",
                    7,
                    _state("run-0", act=1, floor=2, state_type="monster"),
                    decision_id="combat-0",
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "fullauto-deploy.jsonl"
            _write_trace(path, events)
            trace = load_trace(path)
            comparison = root / "comparison"
            comparison.mkdir()
            record = _record(
                run_id="run-0", seed="seed-run-0", decision_id="combat-0"
            )
            _write_journal(
                comparison,
                [{"battle_id": "b-1", "record_checkpoint": record}],
            )
            merge_comparison_evidence(trace, comparison)
            runs, quality = analyze_traces(
                [trace], cohort="assisted", seed_mode="observational"
            )
            report = build_report(
                runs,
                quality,
                cohort="assisted",
                seed_mode="observational",
                pilot_runs=1,
                formal_runs=500,
                lock=LOCK,
                model_id="trained-test",
            )
        self.assertEqual(report["execution"]["owner"], "combat_solver_full_auto")
        self.assertEqual(report["execution"]["combat_http_actions"], 0)
        self.assertEqual(report["execution"]["combat_mod_deploy_actions"], 1)
        self.assertNotIn("combat_http_actions_under_full_auto", report["verdict"]["blockers"])
        self.assertNotIn("action_results_missing", report["verdict"]["blockers"])

    def test_fullauto_and_http_combat_actions_cannot_coexist(self) -> None:
        events = _valid_trace()
        events[0]["raw"]["execution_owner"] = "combat_solver_full_auto"
        terminal = events[-1]
        terminal["sequence"] = 8
        events.extend(
            [
                _event(
                    "state",
                    6,
                    _state("run-0", act=1, floor=2, state_type="monster"),
                    decision_id="combat-0",
                ),
                _event(
                    "action",
                    7,
                    {"action": "play_card", "card_index": 0},
                    decision_id="combat-0",
                    state_type="monster",
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fullauto-http.jsonl"
            _write_trace(path, events)
            runs, quality = analyze_traces(
                [load_trace(path)], cohort="assisted", seed_mode="observational"
            )
            report = build_report(
                runs,
                quality,
                cohort="assisted",
                seed_mode="observational",
                pilot_runs=1,
                formal_runs=500,
                lock=LOCK,
                model_id="trained-test",
            )
        self.assertEqual(report["execution"]["combat_http_actions"], 1)
        self.assertIn("combat_http_actions_under_full_auto", report["verdict"]["blockers"])

    def test_one_verified_run_cannot_mask_another_run_missing_deploy_log(self) -> None:
        from tests.test_deploy_evidence import _record, _write_journal
        from scripts.assess_full_run import merge_comparison_evidence

        first_events = _valid_trace()
        first_events[0]["raw"]["execution_owner"] = "combat_solver_full_auto"
        first_events.append(
            _event(
                "state",
                20,
                _state("run-0", act=1, floor=2, state_type="monster"),
                decision_id="combat-0",
            )
        )
        second_events = _valid_trace()
        second_events[0]["raw"]["execution_owner"] = "combat_solver_full_auto"
        for event in second_events:
            raw = event.get("raw")
            if isinstance(raw, dict):
                if raw.get("run_id") == "run-0":
                    raw["run_id"] = "run-1"
                    raw["seed"] = "seed-run-1"
                run = raw.get("run")
                if isinstance(run, dict) and run.get("run_id") == "run-0":
                    run["run_id"] = "run-1"
                    run["seed"] = "seed-run-1"
        second_events.append(
            _event(
                "state",
                20,
                _state("run-1", act=1, floor=2, state_type="monster"),
                decision_id="combat-1",
            )
        )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first_path = root / "run-0.jsonl"
            second_path = root / "run-1.jsonl"
            _write_trace(first_path, first_events)
            _write_trace(second_path, second_events)
            first_trace = load_trace(first_path)
            comparison = root / "comparison"
            comparison.mkdir()
            _write_journal(
                comparison,
                [
                    {
                        "battle_id": "b-0",
                        "record_checkpoint": _record(
                            battle_id="b-0",
                            run_id="run-0",
                            seed="seed-run-0",
                            decision_id="combat-0",
                        ),
                    }
                ],
            )
            merge_comparison_evidence(first_trace, comparison)
            runs, quality = analyze_traces(
                [first_trace, load_trace(second_path)],
                cohort="assisted",
                seed_mode="observational",
            )
            report = build_report(
                runs,
                quality,
                cohort="assisted",
                seed_mode="observational",
                pilot_runs=2,
                formal_runs=500,
                lock=LOCK,
                model_id="trained-test",
            )

        self.assertNotIn(
            "combat_deploy_log_missing_for_run:run-0",
            report["verdict"]["blockers"],
        )
        self.assertIn(
            "combat_deploy_log_missing_for_run:run-1",
            report["verdict"]["blockers"],
        )

    def test_top_level_owner_and_source_are_not_deduped_away(self) -> None:
        identity = {
            "run_id": "run-0",
            "seed": "seed-run-0",
            "character": "IRONCLAD",
            "ascension": 10,
            "game_mode": "standard",
        }
        first = [
            _event(
                "run_identity",
                0,
                identity,
                execution_owner="http_route_executor",
                source="http_route",
            )
        ]
        second = [
            _event(
                "run_identity",
                0,
                identity,
                execution_owner="combat_solver_full_auto",
                source="deploy_log",
            )
        ]
        with tempfile.TemporaryDirectory() as temp:
            first_path = Path(temp) / "owner-a.jsonl"
            second_path = Path(temp) / "owner-b.jsonl"
            _write_trace(first_path, first)
            _write_trace(second_path, second)
            runs, quality = analyze_traces(
                [load_trace(first_path), load_trace(second_path)],
                cohort="assisted",
                seed_mode="observational",
            )
            report = build_report(
                runs,
                quality,
                cohort="assisted",
                seed_mode="observational",
                pilot_runs=1,
                formal_runs=500,
                lock=LOCK,
                model_id="trained-test",
            )

        self.assertEqual(
            report["execution"]["owners"],
            ["combat_solver_full_auto", "http_route_executor"],
        )
        self.assertIn("execution_owner_conflict", report["verdict"]["blockers"])

    def test_http_owner_rejects_mod_deploy_evidence(self) -> None:
        from tests.test_deploy_evidence import _record, _write_journal
        from scripts.assess_full_run import merge_comparison_evidence

        events = _valid_trace()
        events[0]["raw"]["execution_owner"] = "http_route_executor"
        events.extend(
            [
                _event(
                    "state",
                    8,
                    _state("run-0", act=1, floor=2, state_type="monster"),
                    decision_id="combat-0",
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "http-mod-deploy.jsonl"
            _write_trace(path, events)
            trace = load_trace(path)
            comparison = root / "comparison"
            comparison.mkdir()
            record = _record(
                run_id="run-0", seed="seed-run-0", decision_id="combat-0"
            )
            _write_journal(
                comparison,
                [{"battle_id": "b-1", "record_checkpoint": record}],
            )
            merge_comparison_evidence(trace, comparison)
            runs, quality = analyze_traces(
                [trace], cohort="assisted", seed_mode="observational"
            )
            report = build_report(
                runs,
                quality,
                cohort="assisted",
                seed_mode="observational",
                pilot_runs=1,
                formal_runs=500,
                lock=LOCK,
                model_id="trained-test",
            )
        self.assertIn("combat_mod_deploy_actions_under_http_owner", report["verdict"]["blockers"])

    def test_run_id_is_the_deduplication_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = []
            for suffix in ("a", "b"):
                path = Path(temp) / f"trace-{suffix}.jsonl"
                _write_trace(path, _valid_trace())
                paths.append(load_trace(path))
            runs, quality = analyze_traces(
                paths, cohort="assisted", seed_mode="observational"
            )
        # The same concrete run copied into two traces must not inflate wins.
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].run_id, "run-0")
        self.assertGreaterEqual(len(runs[0].trace_paths), 2)
        self.assertEqual(runs[0].action_count, 1)
        self.assertEqual(runs[0].state_count, 4)

    def test_pending_continue_does_not_leak_into_the_next_trace(self) -> None:
        first = [
            _event(
                "run_identity",
                0,
                {
                    "run_id": "saved-run",
                    "seed": "saved-seed",
                    "character": "IRONCLAD",
                    "ascension": 10,
                    "game_mode": "standard",
                },
            ),
            _event(
                "state",
                1,
                {"state_type": "menu", "menu_screen": "main", "options": ["continue"]},
                decision_id="menu",
            ),
            _event(
                "action",
                2,
                {"action": "menu_select", "option": "continue"},
                decision_id="menu",
                state_type="menu",
            ),
        ]
        second = _valid_trace()
        with tempfile.TemporaryDirectory() as temp:
            first_path = Path(temp) / "continue.jsonl"
            second_path = Path(temp) / "fresh.jsonl"
            _write_trace(first_path, first)
            _write_trace(second_path, second)
            runs, _quality = analyze_traces(
                [load_trace(first_path), load_trace(second_path)],
                cohort="assisted",
                seed_mode="observational",
            )
        by_id = {run.run_id: run for run in runs}
        self.assertIn("resume_without_compendium_identity", by_id["saved-run"].to_json("assisted")["issues"])
        self.assertNotIn("resume_without_compendium_identity", by_id["run-0"].to_json("assisted")["issues"])
        self.assertTrue(by_id["run-0"].terminal_seen)

    def test_pending_action_and_state_do_not_cross_trace_boundaries(self) -> None:
        first = [
            _event(
                "run_identity",
                0,
                {
                    "run_id": "run-a",
                    "seed": "seed-a",
                    "character": "IRONCLAD",
                    "ascension": 10,
                    "game_mode": "standard",
                },
            ),
            _event("state", 1, _state("run-a", act=1, floor=1), decision_id="shared"),
            _event(
                "action",
                2,
                {"action": "choose_map_node", "index": 0},
                decision_id="shared",
                state_type="map",
            ),
        ]
        second = [
            _event(
                "run_identity",
                0,
                {
                    "run_id": "run-b",
                    "seed": "seed-b",
                    "character": "IRONCLAD",
                    "ascension": 10,
                    "game_mode": "standard",
                },
            ),
            _event("result", 2, {"status": "ok"}, decision_id="shared"),
        ]
        with tempfile.TemporaryDirectory() as temp:
            first_path = Path(temp) / "action.jsonl"
            second_path = Path(temp) / "result.jsonl"
            _write_trace(first_path, first)
            _write_trace(second_path, second)
            runs, _quality = analyze_traces(
                [load_trace(first_path), load_trace(second_path)],
                cohort="assisted",
                seed_mode="observational",
            )
        by_id = {run.run_id: run for run in runs}
        self.assertEqual(by_id["run-a"].missing_results, 1)
        self.assertEqual(by_id["run-b"].action_count, 0)

    def test_ambiguous_terminal_and_incomplete_run_fail_closed(self) -> None:
        events = _valid_trace()
        terminal = events[-1]["raw"]
        terminal["game_over"] = {"message": "Run ended."}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ambiguous.jsonl"
            _write_trace(path, events)
            runs, quality = analyze_traces(
                [load_trace(path)], cohort="assisted", seed_mode="observational"
            )
            report = build_report(
                runs,
                quality,
                cohort="assisted",
                seed_mode="observational",
                pilot_runs=1,
                formal_runs=500,
                lock=LOCK,
                model_id="trained-test",
            )
        self.assertEqual(report["counts"]["full_run_wins"], 0)
        self.assertEqual(report["counts"]["unknown_or_incomplete"], 1)
        self.assertFalse(report["verdict"]["accepted"])
        self.assertIn("unknown_or_nonterminal_runs", report["verdict"]["blockers"])

    def test_continue_without_compendium_identity_never_counts_as_valid_run(self) -> None:
        events = [
            _event(
                "session",
                0,
                {
                    "cohort": "assisted",
                    "observed_game": dict(LOCK["game"]),
                    "observed_mods": ["STS2_MCP"],
                    "model_id": "trained-test",
                },
            ),
            _event(
                "run_identity",
                1,
                {
                    "run_id": "old-run",
                    "seed": "old-seed",
                    "character": "IRONCLAD",
                    "ascension": 10,
                    "game_mode": "standard",
                },
            ),
            _event(
                "state",
                2,
                {"state_type": "menu", "menu_screen": "main", "options": ["continue"]},
                decision_id="menu-1",
            ),
            _event(
                "action",
                3,
                {"action": "menu_select", "option": "continue"},
                decision_id="menu-1",
                state_type="menu",
            ),
            _event(
                "state",
                4,
                _state("old-run", act=1, floor=7),
                decision_id="resume-state",
            ),
            _event(
                "state",
                5,
                {
                    **_state("old-run", act=1, floor=7, state_type="game_over"),
                    "game_over": {"message": "Defeated"},
                },
                decision_id="resume-end",
            ),
        ]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "resume.jsonl"
            _write_trace(path, events)
            runs, quality = analyze_traces(
                [load_trace(path)], cohort="assisted", seed_mode="observational"
            )
            report = build_report(
                runs,
                quality,
                cohort="assisted",
                seed_mode="observational",
                pilot_runs=1,
                formal_runs=500,
                lock=LOCK,
                model_id="trained-test",
            )
        self.assertEqual(report["counts"]["save_load_runs"], 1)
        self.assertIn("resume_without_compendium_identity", report["verdict"]["blockers"])
        self.assertFalse(report["verdict"]["accepted"])

    def test_no_sl_requires_explicit_cohort_marker_and_rejects_resume(self) -> None:
        events = _valid_trace()
        events[0]["raw"]["cohort"] = "assisted"
        # Add a resume marker to the otherwise valid run.
        events.insert(
            2,
            _event(
                "action",
                100,
                {"action": "menu_select", "option": "continue"},
                decision_id="d-0",
                state_type="menu",
            ),
        )
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "no-sl.jsonl"
            _write_trace(path, events)
            runs, quality = analyze_traces(
                [load_trace(path)], cohort="no-sl", seed_mode="observational"
            )
            report = build_report(
                runs,
                quality,
                cohort="no-sl",
                seed_mode="observational",
                pilot_runs=1,
                formal_runs=500,
                lock=LOCK,
                model_id="trained-test",
            )
        self.assertIn("no_sl_cohort_marker_missing", report["verdict"]["blockers"])
        self.assertIn("no_sl_save_load_used", report["verdict"]["blockers"])

    def test_plan_declares_20_and_500_without_starting_a_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            plan = build_plan(
                cohort="assisted",
                seed_mode="observational",
                pilot_runs=20,
                formal_runs=500,
                lock_path=LOCK_PATH,
            )
        self.assertEqual(plan["pilot"]["required_runs"], 20)
        self.assertEqual(plan["formal"]["required_runs"], 500)
        self.assertTrue(plan["plan"]["formal_command_is_not_started"])
        self.assertFalse(plan["verdict"]["accepted"])


if __name__ == "__main__":
    unittest.main()
