"""Deterministic lifecycle tests for the fresh solver-batch supervisor."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from scripts.supervise_solver_batch import (
    EXIT_CHILD_FAILED,
    EXIT_GAME_LOST,
    EXIT_GAME_WAIT_TIMEOUT,
    EXIT_OK,
    EXIT_PARTIAL,
    EXIT_STOPPED,
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
    def __init__(self, *, comparison_code: int | None = 0, fail_name: str | None = None) -> None:
        self.comparison_code = comparison_code
        self.fail_name = fail_name
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
            process = FakeProcess(None)
            name = "fullauto_keeper"
        else:
            process = FakeProcess(None)
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


class LockTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
