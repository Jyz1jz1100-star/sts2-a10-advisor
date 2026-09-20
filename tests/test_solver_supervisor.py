"""Deterministic lifecycle tests for the fresh solver-batch supervisor."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import scripts.supervise_solver_batch as supervise_solver_batch
from scripts.supervise_solver_batch import (
    EXIT_CHILD_FAILED,
    EXIT_GAME_LOST,
    EXIT_GAME_WAIT_TIMEOUT,
    EXIT_OK,
    EXIT_PARTIAL,
    EXIT_PREFLIGHT_FAILED,
    EXIT_STOPPED,
    AUTOPLAY_CLASSIFIED_STOP_EXIT,
    BatchLock,
    BatchSupervisor,
    DEFAULT_AUTOPLAY_LOCK,
    SupervisorAlreadyRunning,
    SupervisorConfigurationError,
    SupervisorConfig,
    _pid_is_alive,
    _windows_pid_is_alive,
    build_component_commands,
    new_batch_id,
    read_trace_session_end,
    validate_batch_id,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeProcess:
    _next_pid = 41000

    def __init__(self, returncode: int | None = None) -> None:
        self.pid = FakeProcess._next_pid
        FakeProcess._next_pid += 1
        self.returncode = returncode
        self.terminate_calls = 0
        self.kill_calls = 0
        self.signal_calls: list[object] = []

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, value: object) -> None:
        self.signal_calls.append(value)
        self.returncode = 0

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.returncode = 0

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise TimeoutError("still running")
        return self.returncode


class FakePopen:
    def __init__(
        self,
        *,
        comparison_code: int | None = 0,
        fail_name: str | None = None,
        autoplay_code: int | None = None,
        keeper_code: int | None = None,
    ) -> None:
        self.comparison_code = comparison_code
        self.fail_name = fail_name
        self.autoplay_code = autoplay_code
        self.keeper_code = keeper_code
        self.started: list[tuple[list[str], dict]] = []
        self.processes: dict[str, FakeProcess] = {}

    def __call__(self, command: list[str], **kwargs):
        if self.fail_name and self.fail_name in " ".join(command):
            raise OSError("synthetic launch failure")
        joined = " ".join(command)
        if "run_solver_comparison.py" in joined:
            process = FakeProcess(self.comparison_code)
            name = "comparison"
        elif "bridge.fullauto_keeper" in joined:
            process = FakeProcess(self.keeper_code)
            name = "fullauto_keeper"
        else:
            process = FakeProcess(self.autoplay_code)
            name = "autoplay"
        self.started.append((list(command), kwargs))
        self.processes[name] = process
        return process


class FakeKernel32:
    def __init__(self, exit_code: int = 259, handle: int = 0x1234) -> None:
        self.exit_code = exit_code
        self.handle = handle
        self.open_args: tuple[object, ...] | None = None
        self.closed: list[object] = []

    def OpenProcess(self, *args):
        self.open_args = args
        return self.handle

    def GetExitCodeProcess(self, _handle, pointer) -> bool:
        pointer._obj.value = self.exit_code
        return True

    def CloseHandle(self, handle) -> bool:
        self.closed.append(handle)
        return True


def _config(root: Path, clock: FakeClock, **overrides) -> SupervisorConfig:
    values = {
        "batch_id": "ssb-test-0001",
        "mode": "observational",
        "output_root": root,
        "game_log_dir": root / "game-logs",
        "max_battles": 2,
        "max_runs": 2,
        "max_actions": 4,
        "poll_seconds": 0.1,
        "game_wait_seconds": 1.0,
        "game_loss_grace_seconds": 0.2,
        "graceful_timeout_seconds": 0.2,
        "allow_actions": True,
    }
    values.update(overrides)
    return SupervisorConfig(**values)


def _write_comparison_artifacts(
    supervisor: BatchSupervisor,
    *,
    status: str = "complete",
    stopped_reason: str = "max_battles",
    n_battles: int = 2,
    automated: bool = True,
    integrity_valid: bool = True,
    identity_verified: bool = True,
) -> None:
    """Write the minimum shape emitted by run_solver_comparison.py."""

    comparison_dir = supervisor.comparison_output_dir
    comparison_dir.mkdir(parents=True, exist_ok=True)
    fixed_seed_claim = supervisor.config.mode == "fixed" and integrity_valid
    identity = {
        "schema_version": 1,
        "status": "verified" if identity_verified else "pending",
        "verified": identity_verified,
        "required": {
            "game_mode": "standard",
            "character": "IRONCLAD",
            "ascension": 10,
            "singleplayer": True,
        },
        "observations": 1 if identity_verified else 0,
        "verified_observations": 1 if identity_verified else 0,
        "missing_fields": [],
        "conflicts": [],
        "errors": [],
    }
    summary = {
        "run_identity": identity,
        "batch": {
            "batch_id": supervisor.config.resolved_comparison_batch_id,
            "status": status,
            "resume": False,
            "new_battles": n_battles,
            "stopped_reason": stopped_reason,
            "seed_mode": supervisor.config.mode,
            "fixed_seed_claim": fixed_seed_claim,
        },
        "verdict": {
            "stopped_reason": stopped_reason,
            "n_battles": n_battles,
            "existing_battles": 0,
            "automated": automated,
            "run_identity_verified": identity_verified,
            "accepted": (
                automated
                and integrity_valid
                and identity_verified
                and supervisor.config.mode == "fixed"
            ),
            "fixed_seed_verified": fixed_seed_claim,
            "record_seed_integrity": {"valid": integrity_valid},
        },
    }
    manifest = {
        "run_identity": identity,
        "batch_id": supervisor.config.resolved_comparison_batch_id,
        "status": status,
        "automated": automated,
        "seed_mode": supervisor.config.mode,
        "fixed_seed_claim": fixed_seed_claim,
        "resume": {"requested": False},
        "verdict": {
            "stopped_reason": stopped_reason,
            "n_battles": n_battles,
            "existing_battles": 0,
            "automated": automated,
            "run_identity_verified": identity_verified,
            "accepted": (
                automated
                and integrity_valid
                and identity_verified
                and supervisor.config.mode == "fixed"
            ),
            "fixed_seed_verified": fixed_seed_claim,
            "record_seed_integrity": {"valid": integrity_valid},
        },
    }
    (comparison_dir / "summary.json").write_text(
        json.dumps(summary), encoding="utf-8"
    )
    (comparison_dir / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    rows = [
        {"battle_id": f"csb-test-{index}"}
        for index in range(n_battles)
    ]
    (comparison_dir / "battles.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


class BatchIdTests(unittest.TestCase):
    def test_generated_ids_are_fresh_and_not_contaminated(self) -> None:
        stamp = datetime(2026, 9, 2, tzinfo=UTC)
        first = new_batch_id(stamp)
        second = new_batch_id(stamp)
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("ssb-20260902T000000Z-"))
        self.assertEqual(validate_batch_id(first), first)

    def test_phase1_50_is_never_an_adoptable_id(self) -> None:
        for value in ("phase1-50", "ssb-phase1-50", "old_phase1_50_copy"):
            with self.subTest(value=value):
                with self.assertRaises(SupervisorConfigurationError):
                    validate_batch_id(value)


class WindowsPidTests(unittest.TestCase):
    def test_windows_query_uses_limited_handle_and_closes_it(self) -> None:
        kernel32 = FakeKernel32(exit_code=259)
        self.assertTrue(_windows_pid_is_alive(12345, kernel32=kernel32))
        self.assertEqual(kernel32.open_args[0], 0x1000)
        self.assertEqual(kernel32.open_args[2], 12345)
        self.assertEqual(kernel32.closed, [0x1234])

        exited = FakeKernel32(exit_code=0)
        self.assertFalse(_windows_pid_is_alive(12345, kernel32=exited))

    def test_windows_path_never_calls_os_kill(self) -> None:
        # Patch the platform branch and query helper only.  No real process
        # handle is opened and, importantly, the current process is untouched.
        with patch("scripts.supervise_solver_batch.os.name", "nt"), patch(
            "scripts.supervise_solver_batch._windows_pid_is_alive", return_value=True
        ) as query, patch("scripts.supervise_solver_batch.os.kill") as kill:
            self.assertTrue(_pid_is_alive(12345))
        query.assert_called_once_with(12345)
        kill.assert_not_called()


class CommandPlanTests(unittest.TestCase):
    def test_fixed_default_ledgers_are_batch_local_and_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _config(root, FakeClock(), mode="fixed", batch_id="ssb-first")
            second = _config(root, FakeClock(), mode="fixed", batch_id="ssb-second")
            self.assertNotEqual(first.resolved_seed_ledger, second.resolved_seed_ledger)
            self.assertEqual(
                first.resolved_seed_ledger,
                (root / "ssb-first" / "seed_allocation.ledger.json").resolve(),
            )
            commands = build_component_commands(first, root / first.batch_id)
            autoplay = commands["autoplay"]
            self.assertEqual(
                Path(autoplay[autoplay.index("--seed-ledger") + 1]).resolve(),
                first.resolved_seed_ledger,
            )

    def test_plan_coordinates_all_components_with_explicit_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root, FakeClock(), mode="fixed")
            commands = build_component_commands(config, root / config.batch_id)

        self.assertEqual(set(commands), {"comparison", "autoplay", "fullauto_keeper"})
        comparison = commands["comparison"]
        self.assertIn("--seed-mode", comparison)
        self.assertEqual(comparison[comparison.index("--seed-mode") + 1], "fixed")
        self.assertIn("--automated", comparison)
        self.assertEqual(
            comparison[comparison.index("--bridge-grace-seconds") + 1],
            str(config.game_loss_grace_seconds),
        )
        self.assertNotIn("--resume", comparison)
        self.assertIn("--allow-actions", commands["autoplay"])
        self.assertIn("--out-of-combat-only", commands["autoplay"])
        # The comparison track loads Combat Solver workshop mods, so autoplay
        # must verify its environment against the solver lock, not the
        # STS2_MCP-only A10 acceptance lock.
        self.assertIn("--lock-file", commands["autoplay"])
        self.assertEqual(
            Path(commands["autoplay"][commands["autoplay"].index("--lock-file") + 1]),
            DEFAULT_AUTOPLAY_LOCK,
        )
        self.assertEqual(
            commands["autoplay"][commands["autoplay"].index("--log-dir") + 1],
            "",
        )
        flattened = " ".join(part for command in commands.values() for part in command)
        self.assertNotIn("phase1-50", flattened)
        self.assertNotEqual(
            config.batch_id,
            config.resolved_comparison_batch_id,
        )


class LifecycleTests(unittest.TestCase):
    def test_dry_run_creates_manifest_without_game_or_children(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock()
            popen = FakePopen()
            probe_calls: list[int] = []
            config = _config(root, clock, dry_run=True, allow_actions=False)
            supervisor = BatchSupervisor(
                config,
                popen_factory=popen,
                game_probe=lambda _config: probe_calls.append(1) or False,
                clock=clock,
                sleep=clock.sleep,
            )

            result = supervisor.run()
            self.assertEqual(result, EXIT_OK)
            self.assertEqual(popen.started, [])
            self.assertEqual(probe_calls, [])
            status = json.loads(supervisor.status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "dry_run")
            self.assertFalse(status["resume"])
            self.assertFalse(status["comparison_resume_supported"])
            self.assertEqual(status["mode"], "observational")
            self.assertFalse((root / ".active.lock").exists())

    def test_missing_game_waits_then_fails_before_starting_children(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock()
            popen = FakePopen()
            config = _config(root, clock, game_wait_seconds=0.2)
            supervisor = BatchSupervisor(
                config,
                popen_factory=popen,
                game_probe=lambda _config: False,
                clock=clock,
                sleep=clock.sleep,
            )

            result = supervisor.run()
            self.assertEqual(result, EXIT_GAME_WAIT_TIMEOUT)
            self.assertEqual(popen.started, [])
            status = json.loads(supervisor.status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["stop_reason"], "game_wait_timeout")
            events = [
                json.loads(line)["event"]
                for line in supervisor.supervisor_log_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("game_waiting", events)
            self.assertIn("game_wait_timeout", events)

    def test_comparison_success_stops_other_children_and_records_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock()
            popen = FakePopen(comparison_code=0)
            config = _config(root, clock)
            supervisor = BatchSupervisor(
                config,
                popen_factory=popen,
                game_probe=lambda _config: True,
                clock=clock,
                sleep=clock.sleep,
                comparison_output_dir=root / "comparison",
            )
            _write_comparison_artifacts(
                supervisor,
                integrity_valid=False,
            )

            result = supervisor.run()
            self.assertEqual(result, EXIT_OK)
            self.assertEqual(len(popen.started), 3)
            status = json.loads(supervisor.status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "complete")
            self.assertEqual(status["stop_reason"], "comparison_complete")
            self.assertEqual(status["exit_codes"]["comparison"], 0)
            self.assertEqual(status["exit_codes"]["fullauto_keeper"], 0)
            self.assertEqual(status["exit_codes"]["autoplay"], 0)
            self.assertTrue(status["run_identity"]["valid"])
            self.assertTrue(popen.processes["fullauto_keeper"].signal_calls or popen.processes["fullauto_keeper"].terminate_calls)
            self.assertFalse((root / ".active.lock").exists())

    def test_code_zero_with_partial_comparison_artifacts_is_not_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock()
            popen = FakePopen(comparison_code=0)
            config = _config(root, clock)
            supervisor = BatchSupervisor(
                config,
                popen_factory=popen,
                game_probe=lambda _config: True,
                clock=clock,
                sleep=clock.sleep,
                comparison_output_dir=root / "comparison",
            )
            _write_comparison_artifacts(
                supervisor,
                status="partial",
                stopped_reason="max_seconds",
                n_battles=1,
                integrity_valid=False,
            )

            result = supervisor.run()
            self.assertEqual(result, EXIT_PARTIAL)
            status = json.loads(supervisor.status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "partial")
            self.assertNotEqual(status["status"], "complete")
            self.assertEqual(status["comparison_result"]["classification"], "partial")
            self.assertEqual(
                status["comparison_result"]["stopped_reason"]["summary"],
                "max_seconds",
            )

    def test_observational_nonpartition_integrity_is_collect_complete_not_fixed_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock()
            popen = FakePopen(comparison_code=0)
            config = _config(root, clock, mode="observational")
            supervisor = BatchSupervisor(
                config,
                popen_factory=popen,
                game_probe=lambda _config: True,
                clock=clock,
                sleep=clock.sleep,
                comparison_output_dir=root / "comparison",
            )
            _write_comparison_artifacts(
                supervisor,
                integrity_valid=False,
            )

            result = supervisor.run()
            self.assertEqual(result, EXIT_OK)
            comparison = json.loads(supervisor.status_path.read_text(encoding="utf-8"))["comparison_result"]
            self.assertEqual(comparison["classification"], "complete")
            self.assertFalse(comparison["fixed_seed_claim"])
            self.assertEqual(set(comparison["fixed_seed_claim_values"].values()), {False})
            self.assertFalse(comparison["acceptance_claim"])
            self.assertIn("observational_mode", comparison["acceptance_blockers"])
            self.assertIn("record_integrity_invalid", comparison["acceptance_blockers"])

    def test_fixed_nonpartition_integrity_blocks_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock()
            popen = FakePopen(comparison_code=0)
            config = _config(root, clock, mode="fixed")
            supervisor = BatchSupervisor(
                config,
                popen_factory=popen,
                game_probe=lambda _config: True,
                clock=clock,
                sleep=clock.sleep,
                comparison_output_dir=root / "comparison",
            )
            _write_comparison_artifacts(
                supervisor,
                integrity_valid=False,
            )

            result = supervisor.run()
            self.assertEqual(result, EXIT_CHILD_FAILED)
            status = json.loads(supervisor.status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["comparison_result"]["classification"], "failed")
            self.assertFalse(status["comparison_result"]["fixed_seed_claim"])
            self.assertIn("record_integrity_invalid", status["comparison_result"]["issues"])

    def test_unverified_run_identity_blocks_complete_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock()
            popen = FakePopen(comparison_code=0)
            config = _config(root, clock)
            supervisor = BatchSupervisor(
                config,
                popen_factory=popen,
                game_probe=lambda _config: True,
                clock=clock,
                sleep=clock.sleep,
                comparison_output_dir=root / "comparison",
            )
            _write_comparison_artifacts(supervisor, identity_verified=False)

            result = supervisor.run()
            self.assertEqual(result, EXIT_CHILD_FAILED)
            status = json.loads(supervisor.status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["comparison_result"]["classification"], "failed")
            self.assertIn("run_identity_contract_failed", status["comparison_result"]["issues"])
            self.assertFalse(status["comparison_result"]["run_identity"]["valid"])

    def test_child_failure_stops_remaining_children_and_preserves_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock()
            popen = FakePopen(comparison_code=17)
            config = _config(root, clock)
            supervisor = BatchSupervisor(
                config,
                popen_factory=popen,
                game_probe=lambda _config: True,
                clock=clock,
                sleep=clock.sleep,
            )

            result = supervisor.run()
            self.assertEqual(result, EXIT_CHILD_FAILED)
            status = json.loads(supervisor.status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["exit_codes"]["comparison"], 17)
            self.assertEqual(status["stop_reason"], "comparison_failed")
            events = [
                json.loads(line)
                for line in supervisor.supervisor_log_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(any(row["event"] == "child_exited" and row.get("exit_code") == 17 for row in events))

    def test_game_loss_stops_live_children_with_distinct_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock()
            popen = FakePopen(comparison_code=None)
            probes = iter([True, False])
            config = _config(root, clock, game_loss_grace_seconds=0.0)
            supervisor = BatchSupervisor(
                config,
                popen_factory=popen,
                game_probe=lambda _config: next(probes, False),
                clock=clock,
                sleep=clock.sleep,
            )

            result = supervisor.run()
            self.assertEqual(result, EXIT_GAME_LOST)
            status = json.loads(supervisor.status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["stop_reason"], "game_lost_timeout")
            self.assertEqual(status["status"], "failed")

    def test_operator_stop_is_graceful_and_returns_stopped_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock()
            popen = FakePopen(comparison_code=None)
            config = _config(root, clock, run_timeout_seconds=1.0)
            supervisor: BatchSupervisor

            def sleep_and_request(seconds: float) -> None:
                clock.sleep(seconds)
                supervisor.request_stop("test_stop")

            supervisor = BatchSupervisor(
                config,
                popen_factory=popen,
                game_probe=lambda _config: True,
                clock=clock,
                sleep=sleep_and_request,
            )
            result = supervisor.run()
            self.assertEqual(result, EXIT_STOPPED)
            status = json.loads(supervisor.status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "stopped")
            self.assertEqual(status["stop_reason"], "test_stop")


class FailureConvergenceTests(unittest.TestCase):
    """Regression tests for the ssb-20260906T102557Z-183d0b05 failure class.

    That batch ended with autoplay crashing on a frozen menu state, the
    supervisor killing the comparison child mid-write, and a sibling summary
    left as ``status=running`` residue with ``comparison_result: null`` in the
    manifest.  These tests pin the converged lifecycle.
    """

    def _session_end_trace(self, root: Path, batch_id: str, reason: str) -> None:
        trace = root / batch_id / "autoplay_trace.jsonl"
        trace.parent.mkdir(parents=True, exist_ok=True)
        trace.write_text(
            json.dumps(
                {
                    "event_type": "session_end",
                    "raw": {
                        "summary": {"stop_reason": reason, "runs_started": 0},
                        "detail": f"{reason}: offline fixture",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def test_autoplay_clean_exit_completes_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock()
            popen = FakePopen(comparison_code=None, autoplay_code=0)
            config = _config(root, clock)
            supervisor = BatchSupervisor(
                config,
                popen_factory=popen,
                game_probe=lambda _config: True,
                clock=clock,
                sleep=clock.sleep,
                comparison_output_dir=root / "comparison",
            )
            _write_comparison_artifacts(supervisor)

            result = supervisor.run()
            self.assertEqual(result, EXIT_OK)
            status = json.loads(supervisor.status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "complete")
            self.assertEqual(status["stop_reason"], "autoplay_completed")
            self.assertEqual(status["exit_codes"]["autoplay"], 0)
            self.assertTrue(status["comparison_result"])
            # The other children were stopped cooperatively, not left running.
            self.assertIsNotNone(status["exit_codes"]["comparison"])
            self.assertIsNotNone(status["exit_codes"]["fullauto_keeper"])

    def test_autoplay_clean_exit_with_partial_artifacts_is_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock()
            popen = FakePopen(comparison_code=None, autoplay_code=0)
            config = _config(root, clock)
            supervisor = BatchSupervisor(
                config,
                popen_factory=popen,
                game_probe=lambda _config: True,
                clock=clock,
                sleep=clock.sleep,
                comparison_output_dir=root / "comparison",
            )
            _write_comparison_artifacts(
                supervisor, status="partial", stopped_reason="max_seconds", n_battles=1
            )

            result = supervisor.run()
            self.assertEqual(result, EXIT_PARTIAL)
            status = json.loads(supervisor.status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["stop_reason"], "autoplay_completed")
            self.assertEqual(status["comparison_result"]["classification"], "partial")

    def test_autoplay_classified_stop_preserves_reason_and_residual(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock()
            popen = FakePopen(comparison_code=None, autoplay_code=AUTOPLAY_CLASSIFIED_STOP_EXIT)
            config = _config(root, clock)
            supervisor = BatchSupervisor(
                config,
                popen_factory=popen,
                game_probe=lambda _config: True,
                clock=clock,
                sleep=clock.sleep,
                comparison_output_dir=root / "comparison",
            )

            def start_with_trace(command, **kwargs):
                if "bridge.autoplay" in " ".join(command):
                    self._session_end_trace(
                        root, config.batch_id, "stale_state"
                    )
                return popen(command, **kwargs)

            supervisor.popen_factory = start_with_trace
            _write_comparison_artifacts(
                supervisor, status="running", stopped_reason="running", n_battles=0
            )

            result = supervisor.run()
            self.assertEqual(result, EXIT_CHILD_FAILED)
            status = json.loads(supervisor.status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["stop_reason"], "autoplay_classified_stop")
            self.assertEqual(status["autoplay_summary"]["summary"]["stop_reason"], "stale_state")
            # The killed comparison's own artifacts still say "running"; the
            # supervisor must record that residue explicitly, never promote it.
            self.assertEqual(status["comparison_result"]["classification"], "partial")
            self.assertEqual(status["comparison_result"]["status"]["summary"], "running")
            self.assertIn("status_not_complete", status["comparison_result"]["issues"])

    def test_autoplay_crash_records_comparison_classification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock()
            popen = FakePopen(comparison_code=None, autoplay_code=1)
            config = _config(root, clock)
            supervisor = BatchSupervisor(
                config,
                popen_factory=popen,
                game_probe=lambda _config: True,
                clock=clock,
                sleep=clock.sleep,
                comparison_output_dir=root / "comparison",
            )
            _write_comparison_artifacts(
                supervisor, status="running", stopped_reason="running", n_battles=0
            )

            result = supervisor.run()
            self.assertEqual(result, EXIT_CHILD_FAILED)
            status = json.loads(supervisor.status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["stop_reason"], "autoplay_exited")
            # A crashed child writes no session_end; the manifest must not
            # invent one, but the residual comparison state is still recorded.
            self.assertIsNone(status["autoplay_summary"])
            self.assertEqual(status["comparison_result"]["classification"], "partial")
            self.assertIn("status_not_complete", status["comparison_result"]["issues"])

    def test_missing_trace_session_end_stays_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(read_trace_session_end(Path(tmp) / "absent.jsonl"))


class EvidenceAttributionTests(unittest.TestCase):
    """Solver-log snapshots are binding inputs; attribution stays explicit."""

    def test_manifest_records_solver_log_snapshot_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game_logs = root / "game-logs"
            game_logs.mkdir()
            (game_logs / "godot.log").write_text(
                "[INFO] [CombatSolver] CombatSolver v0.31.0 initialized\n",
                encoding="utf-8",
            )
            clock = FakeClock()
            popen = FakePopen(comparison_code=None, autoplay_code=0)
            config = _config(root, clock, game_log_dir=game_logs)
            supervisor = BatchSupervisor(
                config,
                popen_factory=popen,
                game_probe=lambda _config: True,
                clock=clock,
                sleep=clock.sleep,
                comparison_output_dir=root / "comparison",
            )
            _write_comparison_artifacts(supervisor)

            result = supervisor.run()
            self.assertEqual(result, EXIT_OK)
            status = json.loads(supervisor.status_path.read_text(encoding="utf-8"))
            logs = status["combat_solver_logs"]
            self.assertEqual(logs["log_dir"], str(game_logs))
            self.assertEqual(len(logs["at_start"]), 1)
            self.assertEqual(len(logs["at_end"]), 1)
            started = logs["at_start"][0]
            self.assertTrue(started["sha256"])
            self.assertEqual(started["sha256"], logs["at_end"][0]["sha256"])
            self.assertIn("timestamp proximity is not evidence", logs["purpose"])

    def test_comparison_completed_path_also_finalizes(self) -> None:
        """The comparison-exit-0 branches must run the terminal reconciliation.

        Regression: the complete branch returned after stopping the other
        children without finalizing, so a normally completed batch could
        still end with ``combat_solver_logs.at_end = null``.
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game_logs = root / "game-logs"
            game_logs.mkdir()
            (game_logs / "godot.log").write_text(
                "[INFO] [CombatSolver] CombatSolver v0.31.0 initialized\n",
                encoding="utf-8",
            )
            clock = FakeClock()
            popen = FakePopen(comparison_code=0)
            config = _config(root, clock, game_log_dir=game_logs)
            supervisor = BatchSupervisor(
                config,
                popen_factory=popen,
                game_probe=lambda _config: True,
                clock=clock,
                sleep=clock.sleep,
                comparison_output_dir=root / "comparison",
            )
            _write_comparison_artifacts(supervisor)

            result = supervisor.run()
            self.assertEqual(result, EXIT_OK)
            status = json.loads(supervisor.status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["stop_reason"], "comparison_complete")
            self.assertIsNotNone(status["combat_solver_logs"]["at_end"])
            self.assertEqual(len(status["combat_solver_logs"]["at_end"]), 1)

    def test_comparison_partial_and_invalid_paths_also_finalize(self) -> None:
        for write_artifacts, expected_code, expected_reason in (
            (True, EXIT_PARTIAL, "comparison_partial"),
            (False, EXIT_CHILD_FAILED, "comparison_artifacts_invalid"),
        ):
            with self.subTest(write_artifacts=write_artifacts):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    game_logs = root / "game-logs"
                    game_logs.mkdir()
                    (game_logs / "godot.log").write_text("log\n", encoding="utf-8")
                    clock = FakeClock()
                    popen = FakePopen(comparison_code=0)
                    config = _config(root, clock, game_log_dir=game_logs)
                    supervisor = BatchSupervisor(
                        config,
                        popen_factory=popen,
                        game_probe=lambda _config: True,
                        clock=clock,
                        sleep=clock.sleep,
                        comparison_output_dir=root / "comparison",
                    )
                    if write_artifacts:
                        _write_comparison_artifacts(
                            supervisor, status="partial", stopped_reason="max_seconds"
                        )

                    result = supervisor.run()
                    self.assertEqual(result, expected_code)
                    status = json.loads(
                        supervisor.status_path.read_text(encoding="utf-8")
                    )
                    self.assertEqual(status["stop_reason"], expected_reason)
                    self.assertIsNotNone(
                        status["combat_solver_logs"]["at_end"]
                    )

    def test_torn_trace_tail_does_not_lose_session_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace = Path(tmp) / "autoplay_trace.jsonl"
            complete_row = json.dumps(
                {
                    "event_type": "session_end",
                    "raw": {"summary": {"stop_reason": "stale_state"}, "detail": "x"},
                }
            ).encode("utf-8")
            # A killed process can leave a truncated append: a valid prefix of
            # the next JSON row ending in a partial UTF-8 sequence.
            torn_tail = b'{"event_type": "sta\xff'
            trace.write_bytes(complete_row + b"\n" + torn_tail)
            summary = read_trace_session_end(trace)
            self.assertEqual(summary["summary"]["stop_reason"], "stale_state")
            self.assertTrue(summary["trace_tail_corrupt"])

    def test_fully_corrupt_trace_yields_no_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace = Path(tmp) / "autoplay_trace.jsonl"
            trace.write_bytes(b"\xff\xfe not json at all \x00")
            self.assertIsNone(read_trace_session_end(trace))

    def test_intact_trace_has_no_corruption_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace = Path(tmp) / "autoplay_trace.jsonl"
            trace.write_text(
                json.dumps(
                    {
                        "event_type": "session_end",
                        "raw": {"summary": {"stop_reason": None}, "detail": None},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            summary = read_trace_session_end(trace)
            self.assertNotIn("trace_tail_corrupt", summary)

    def test_report_marks_historical_logs_unattributable(self) -> None:
        from scripts.report_solver_log_attribution import build_report

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / "logs"
            log_dir.mkdir()
            (log_dir / "godot2026-09-06T18.25.02.log").write_text(
                "[INFO] [CombatSolver] CombatSolver v0.31.0 initialized\n"
                "[INFO] [CombatSolver] SEARCH_REQUEST generation=1 turn=1\n",
                encoding="utf-8",
            )
            supervisor_root = root / "supervisor"
            batch_dir = supervisor_root / "ssb-20260906T102557Z-183d0b05"
            batch_dir.mkdir(parents=True)
            (batch_dir / "manifest.json").write_text(
                json.dumps({"batch_id": "ssb-20260906T102557Z-183d0b05"}),
                encoding="utf-8",
            )

            report = build_report(log_dir, supervisor_root)
            self.assertEqual(report["summary"]["total_logs"], 1)
            self.assertEqual(report["summary"]["unattributable"], 1)
            entry = report["logs"][0]
            self.assertFalse(entry["attributable"])
            self.assertEqual(entry["verdict"], "无法归属")
            self.assertIn(
                "no supervisor batch manifest inventory observed this file's hash",
                entry["reasons"],
            )
            self.assertIn("log_contains_no_run_identity_binding", entry["reasons"])
            self.assertEqual(entry["observed_solver_versions"], ["0.31.0"])

    def test_snapshot_observation_never_claims_production(self) -> None:
        from scripts.report_solver_log_attribution import build_report

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / "logs"
            log_dir.mkdir()
            pre_existing = log_dir / "godot-old.log"
            pre_existing.write_text("CombatSolver v0.31.0\n", encoding="utf-8")
            created = log_dir / "godot-new.log"
            created.write_text("CombatSolver v0.31.0 new\n", encoding="utf-8")
            pre_digest = hashlib.sha256(pre_existing.read_bytes()).hexdigest().upper()
            created_digest = hashlib.sha256(created.read_bytes()).hexdigest().upper()
            supervisor_root = root / "supervisor"
            batch_dir = supervisor_root / "ssb-test-0002"
            batch_dir.mkdir(parents=True)
            (batch_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "batch_id": "ssb-test-0002",
                        "combat_solver_logs": {
                            # The pre-existing log appears in BOTH inventories:
                            # the union must not read as "produced by batch".
                            "at_start": [
                                {"path": str(pre_existing), "sha256": pre_digest}
                            ],
                            "at_end": [
                                {"path": str(pre_existing), "sha256": pre_digest},
                                {"path": str(created), "sha256": created_digest},
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )

            report = build_report(log_dir, supervisor_root)
            by_name = {
                Path(entry["path"]).name: entry for entry in report["logs"]
            }
            old = by_name["godot-old.log"]
            self.assertFalse(old["attributable"])
            self.assertEqual(old["verdict"], "无法归属到具体运行")
            self.assertEqual(
                old["observed_in_batch_inventories"],
                [
                    {
                        "batch_id": "ssb-test-0002",
                        "observation": "hash_observed_at_start",
                    }
                ],
            )
            new = by_name["godot-new.log"]
            self.assertFalse(new["attributable"])
            self.assertEqual(new["verdict"], "无法归属到具体运行")
            self.assertEqual(
                new["observed_in_batch_inventories"],
                [
                    {
                        "batch_id": "ssb-test-0002",
                        "observation": "hash_observed_at_end_only",
                    }
                ],
            )
            self.assertIn(
                "inventory observation only; binding to a run would require "
                "content-range and run-identity association inside the log, "
                "which no current format provides",
                new["reasons"],
            )
            # No creation claim may survive anywhere in the report.
            self.assertNotIn("created", json.dumps(report))

    def test_appended_legacy_log_is_not_claimed_as_window_creation(self) -> None:
        """An appended file changes its hash; that is not a new file.

        Regression for the ``created_during_window`` over-inference: the same
        ``godot.log`` exists before the batch (hash H1) and is appended to
        during it (hash H2 at end).  H2 may only be reported as
        ``hash_observed_at_end_only`` — never as a creation/production claim.
        """

        from scripts.report_solver_log_attribution import build_report

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / "logs"
            log_dir.mkdir()
            log_path = log_dir / "godot.log"
            log_path.write_text("CombatSolver v0.31.0\n", encoding="utf-8")
            h1 = hashlib.sha256(log_path.read_bytes()).hexdigest().upper()
            log_path.write_text(
                "CombatSolver v0.31.0\n+ appended line during batch\n",
                encoding="utf-8",
            )
            h2 = hashlib.sha256(log_path.read_bytes()).hexdigest().upper()
            self.assertNotEqual(h1, h2)
            supervisor_root = root / "supervisor"
            batch_dir = supervisor_root / "ssb-test-0003"
            batch_dir.mkdir(parents=True)
            (batch_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "batch_id": "ssb-test-0003",
                        "combat_solver_logs": {
                            "at_start": [{"path": str(log_path), "sha256": h1}],
                            "at_end": [{"path": str(log_path), "sha256": h2}],
                        },
                    }
                ),
                encoding="utf-8",
            )

            report = build_report(log_dir, supervisor_root)
            self.assertEqual(len(report["logs"]), 1)
            entry = report["logs"][0]
            self.assertFalse(entry["attributable"])
            self.assertEqual(
                entry["observed_in_batch_inventories"],
                [
                    {
                        "batch_id": "ssb-test-0003",
                        "observation": "hash_observed_at_end_only",
                    }
                ],
            )
            self.assertNotIn("created", json.dumps(report))

    def test_missing_or_unhashed_start_snapshot_does_not_imply_creation(self) -> None:
        from scripts.report_solver_log_attribution import build_report

        for broken_start in (None, [{"path": "x", "sha256": None, "hash_error": "OSError"}]):
            with self.subTest(broken_start=broken_start):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    log_dir = root / "logs"
                    log_dir.mkdir()
                    log_path = log_dir / "godot.log"
                    log_path.write_text("CombatSolver v0.31.0\n", encoding="utf-8")
                    digest = hashlib.sha256(
                        log_path.read_bytes()
                    ).hexdigest().upper()
                    supervisor_root = root / "supervisor"
                    batch_dir = supervisor_root / "ssb-test-0004"
                    batch_dir.mkdir(parents=True)
                    (batch_dir / "manifest.json").write_text(
                        json.dumps(
                            {
                                "batch_id": "ssb-test-0004",
                                "combat_solver_logs": {
                                    "at_start": broken_start,
                                    "at_end": [
                                        {"path": str(log_path), "sha256": digest}
                                    ],
                                },
                            }
                        ),
                        encoding="utf-8",
                    )

                    report = build_report(log_dir, supervisor_root)
                    entry = report["logs"][0]
                    self.assertFalse(entry["attributable"])
                    self.assertEqual(
                        entry["observed_in_batch_inventories"],
                        [
                            {
                                "batch_id": "ssb-test-0004",
                                "observation": "hash_observed_at_end_only",
                            }
                        ],
                    )
                    self.assertNotIn("created", json.dumps(report))



    def test_active_lock_rejects_duplicate_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = BatchLock(root, "ssb-test-first")
            second = BatchLock(root, "ssb-test-second")
            first.acquire(root / "ssb-test-first")
            try:
                with self.assertRaises(SupervisorAlreadyRunning):
                    second.acquire(root / "ssb-test-second")
            finally:
                first.release()
            self.assertFalse((root / ".active.lock").exists())

    def test_live_configuration_requires_explicit_action_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SupervisorConfigurationError):
                _config(Path(tmp), FakeClock(), allow_actions=False)


class PreflightGateTests(unittest.TestCase):
    """``--dry-run`` is what the one-command entry point asks "can this be played".

    That answer has to come from the same lock the batch itself runs under, or the
    gate and the run disagree about the build.  Both directions are pinned here:
    a matching installed build is allowed, a different Steam build id is refused
    before any child is started.
    """

    def _lock(self, root: Path, *, observed_build: str = "build-1") -> Path:
        release = root / "release_info.json"
        release.write_text(
            json.dumps(
                {
                    "version": "v0.111.0",
                    "commit": "41cef1ea",
                    "main_assembly_hash": 123,
                }
            ),
            encoding="utf-8",
        )
        manifest = root / "appmanifest_2868840.acf"
        manifest.write_text(
            f'"appid"\t"2868840"\n"buildid"\t"{observed_build}"\n'
            '"BetaKey"\t"public-beta"\n',
            encoding="utf-8",
        )
        lock = root / "combat_solver.lock.json"
        lock.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "game": {
                        "app_id": "2868840",
                        "version": "v0.111.0",
                        "commit": "41cef1ea",
                        "main_assembly_hash": 123,
                        "steam_build_id": "build-1",
                        "branch": "public-beta",
                        "release_info_path": str(release),
                        "steam_manifest_path": str(manifest),
                    },
                    "bridge": {
                        "version": "0.4.0",
                        "base_url": "http://127.0.0.1:15526",
                    },
                }
            ),
            encoding="utf-8",
        )
        return lock

    def _run_dry(self, root: Path, lock: Path):
        clock = FakeClock()
        popen = FakePopen()
        supervisor = BatchSupervisor(
            _config(root, clock, dry_run=True, allow_actions=False),
            popen_factory=popen,
            game_probe=lambda _config: False,
            clock=clock,
            sleep=clock.sleep,
        )
        with patch.object(supervise_solver_batch, "DEFAULT_AUTOPLAY_LOCK", lock):
            return supervisor.run(), supervisor, popen

    def test_matching_installed_game_allows_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code, supervisor, popen = self._run_dry(root, self._lock(root))
            self.assertEqual(code, EXIT_OK)
            self.assertEqual(popen.started, [])
            status = json.loads(supervisor.status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "dry_run")
            gates = status["preflight_gates"]
            self.assertTrue(gates["ok"])
            self.assertEqual(gates["failures"], [])
            self.assertEqual(gates["installed_game"]["steam_build_id"], "build-1")

    def test_installed_game_drift_refuses_before_any_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = self._lock(root, observed_build="build-2")
            code, supervisor, popen = self._run_dry(root, lock)
            self.assertEqual(code, EXIT_PREFLIGHT_FAILED)
            self.assertEqual(popen.started, [])
            status = json.loads(supervisor.status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "preflight_failed")
            gates = status["preflight_gates"]
            self.assertFalse(gates["ok"])
            self.assertTrue(
                any("installed game does not match" in text for text in gates["failures"]),
                gates["failures"],
            )
            # The mismatch text is the evidence: which field, expected and observed.
            self.assertIn("steam_build_id", gates["installed_game_error"])

    def test_unmeasurable_mod_bytes_refuse_the_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = self._lock(root)
            # A lock that declares mod paths which are not there cannot attest,
            # and a batch that cannot name its mod bytes is not evidence.
            lock.write_text(
                lock.read_text(encoding="utf-8").replace(
                    '"bridge": {',
                    '"evaluation_environment": {"mod_dll_inventory": ['
                    '{"mod_id": "CombatSolver", "path": "'
                    + (root / "absent.dll").as_posix()
                    + '", "sha256": "' + "0" * 64 + '"}]}, "bridge": {',
                ),
                encoding="utf-8",
            )
            code, supervisor, _popen = self._run_dry(root, lock)
            self.assertEqual(code, EXIT_PREFLIGHT_FAILED)
            gates = json.loads(
                supervisor.status_path.read_text(encoding="utf-8")
            )["preflight_gates"]
            self.assertIn("CombatSolver", gates["mod_attestation"]["unreadable"])


if __name__ == "__main__":
    unittest.main()
