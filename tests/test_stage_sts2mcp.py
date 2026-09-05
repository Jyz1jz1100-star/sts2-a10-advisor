"""Tests for the candidate bridge staging transaction."""
from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.stage_sts2mcp import (
    StagingError,
    apply_candidate,
    bridge_port_occupied,
    build_plan,
    game_process_running,
    preflight_candidate,
    rollback_backup,
    sha256_file,
)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


class StageFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.mods = root / "game" / "mods"
        self.stage = root / "stage"
        self.backups = root / "backups"
        self.mods.mkdir(parents=True)
        self.stage.mkdir(parents=True)
        self.old_dll = self.mods / "STS2_MCP.dll"
        self.old_manifest = self.mods / "STS2_MCP.json"
        self.candidate_dll = self.stage / "STS2_MCP.dll"
        self.candidate_manifest = self.stage / "STS2_MCP.json"
        self.old_dll.write_bytes(b"old-bridge-dll")
        self.old_manifest.write_text(
            json.dumps(
                {
                    "id": "STS2_MCP",
                    "name": "STS2 MCP",
                    "version": "0.4.0",
                    "has_dll": True,
                    "has_pck": False,
                }
            ),
            encoding="utf-8",
        )
        self.candidate_dll.write_bytes(b"candidate-bridge-dll")
        self.candidate_manifest.write_text(
            json.dumps(
                {
                    "id": "STS2_MCP",
                    "name": "STS2 MCP",
                    "version": "0.4.0",
                    "has_dll": True,
                    "has_pck": False,
                }
            ),
            encoding="utf-8",
        )
        self.release = root / "release_info.json"
        self.steam_manifest = root / "appmanifest.acf"
        self.release.write_text(
            json.dumps(
                {
                    "version": "v0.111.0",
                    "commit": "commit-1",
                    "main_assembly_hash": 123,
                }
            ),
            encoding="utf-8",
        )
        self.steam_manifest.write_text(
            '"buildid"\t"build-1"\n"BetaKey"\t"public-beta"\n',
            encoding="utf-8",
        )
        self.lock = root / "live_version.lock.json"
        self.lock.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "game": {
                        "app_id": "2868840",
                        "version": "v0.111.0",
                        "commit": "commit-1",
                        "main_assembly_hash": 123,
                        "steam_build_id": "build-1",
                        "branch": "public-beta",
                        "release_info_path": str(self.release),
                        "steam_manifest_path": str(self.steam_manifest),
                    },
                    "bridge": {
                        "version": "0.4.0",
                        "base_url": "http://127.0.0.1:15526",
                        "dll_path": str(self.old_dll),
                        "manifest_path": str(self.old_manifest),
                        "dll_sha256": _digest(self.old_dll),
                        "manifest_sha256": _digest(self.old_manifest),
                    },
                }
            ),
            encoding="utf-8",
        )

    def plan(self, **overrides):
        values = {
            "lock_path": self.lock,
            "candidate_dll": self.candidate_dll,
            "candidate_manifest": self.candidate_manifest,
            "backup_root": self.backups,
            "candidate_dll_sha256": _digest(self.candidate_dll),
            "candidate_manifest_sha256": _digest(self.candidate_manifest),
            "verify_game": True,
        }
        values.update(overrides)
        return build_plan(**values)

    @staticmethod
    def idle_process() -> bool:
        return False

    @staticmethod
    def idle_bridge(_url: str) -> bool:
        return False


class StageSts2McpTests(unittest.TestCase):
    def test_dry_run_is_read_only_and_emits_lock_suggestion(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = StageFixture(Path(temp))
            plan = fixture.plan()
            result = preflight_candidate(
                plan,
                process_checker=fixture.idle_process,
                bridge_checker=fixture.idle_bridge,
            )
            self.assertEqual(result["status"], "ready")
            self.assertFalse(result["mutated"])
            self.assertFalse(result["lock_update_suggestion"]["health_verification"]["performed"])
            self.assertFalse(fixture.backups.exists())
            self.assertEqual(fixture.old_dll.read_bytes(), b"old-bridge-dll")

    def test_candidate_and_backup_paths_must_not_be_inside_installed_mods(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = StageFixture(Path(temp))
            with self.assertRaises(StagingError):
                fixture.plan(
                    candidate_dll=fixture.mods / "candidate.dll",
                    candidate_manifest=fixture.mods / "candidate.json",
                )
            with self.assertRaises(StagingError):
                fixture.plan(backup_root=fixture.mods / "backup")

    def test_relative_target_and_lock_target_mismatch_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = StageFixture(Path(temp))
            with self.assertRaises(StagingError):
                fixture.plan(target_dll="relative.dll")
            other = fixture.root / "other.dll"
            with self.assertRaises(StagingError):
                fixture.plan(target_dll=other)

    def test_candidate_hash_mismatch_and_installed_hash_mismatch_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = StageFixture(Path(temp))
            plan = fixture.plan(candidate_dll_sha256="0" * 64)
            with self.assertRaises(StagingError):
                preflight_candidate(
                    plan,
                    process_checker=fixture.idle_process,
                    bridge_checker=fixture.idle_bridge,
                )
            fixture.old_dll.write_bytes(b"tampered")
            with self.assertRaises(StagingError):
                preflight_candidate(
                    fixture.plan(),
                    process_checker=fixture.idle_process,
                    bridge_checker=fixture.idle_bridge,
                )

    def test_running_game_or_occupied_bridge_blocks_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = StageFixture(Path(temp))
            plan = fixture.plan()
            with self.assertRaises(StagingError):
                preflight_candidate(
                    plan,
                    process_checker=lambda: True,
                    bridge_checker=fixture.idle_bridge,
                )
            with self.assertRaises(StagingError):
                preflight_candidate(
                    plan,
                    process_checker=fixture.idle_process,
                    bridge_checker=lambda _url: True,
                )
            self.assertEqual(fixture.old_dll.read_bytes(), b"old-bridge-dll")

    def test_apply_backs_up_both_files_and_rollback_restores_them(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = StageFixture(Path(temp))
            lock_before = fixture.lock.read_bytes()
            plan = fixture.plan()
            preflight = preflight_candidate(
                plan,
                process_checker=fixture.idle_process,
                bridge_checker=fixture.idle_bridge,
            )
            applied = apply_candidate(
                plan,
                preflight,
                process_checker=fixture.idle_process,
                bridge_checker=fixture.idle_bridge,
            )
            self.assertEqual(applied["status"], "applied")
            self.assertEqual(fixture.old_dll.read_bytes(), b"candidate-bridge-dll")
            self.assertTrue(Path(applied["backup_manifest"]).is_file())
            self.assertTrue(Path(applied["lock_update_suggestion"]).is_file())
            suggestion = json.loads(Path(applied["lock_update_suggestion"]).read_text(encoding="utf-8"))
            self.assertFalse(suggestion["health_verification"]["performed"])
            self.assertEqual(fixture.candidate_dll.read_bytes(), b"candidate-bridge-dll")
            self.assertEqual(fixture.lock.read_bytes(), lock_before)

            rolled_back = rollback_backup(
                plan,
                applied["backup_manifest"],
                process_checker=fixture.idle_process,
                bridge_checker=fixture.idle_bridge,
            )
            self.assertEqual(rolled_back["status"], "rolled_back")
            self.assertEqual(fixture.old_dll.read_bytes(), b"old-bridge-dll")
            self.assertEqual(
                json.loads(fixture.old_manifest.read_text(encoding="utf-8"))["version"],
                "0.4.0",
            )
            backup_payload = json.loads(
                Path(applied["backup_manifest"]).read_text(encoding="utf-8")
            )
            self.assertTrue(backup_payload["rollback"]["performed"])
            self.assertEqual(fixture.lock.read_bytes(), lock_before)

    def test_rollback_manifest_must_be_inside_configured_backup_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = StageFixture(Path(temp))
            fixture.backups.mkdir()
            outside = fixture.root / "not-backups"
            outside.mkdir()
            manifest = outside / "backup_manifest.json"
            manifest.write_text("{}", encoding="utf-8")
            plan = fixture.plan()
            with self.assertRaisesRegex(StagingError, "inside the configured backup root"):
                rollback_backup(
                    plan,
                    manifest,
                    process_checker=fixture.idle_process,
                    bridge_checker=fixture.idle_bridge,
                )

    def test_rollback_backup_paths_must_stay_in_the_transaction_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = StageFixture(Path(temp))
            plan = fixture.plan()
            preflight = preflight_candidate(
                plan,
                process_checker=fixture.idle_process,
                bridge_checker=fixture.idle_bridge,
            )
            applied = apply_candidate(
                plan,
                preflight,
                process_checker=fixture.idle_process,
                bridge_checker=fixture.idle_bridge,
            )
            manifest_path = Path(applied["backup_manifest"])
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            escaped_dir = fixture.backups / "another-transaction"
            escaped_dir.mkdir()
            payload["files"]["dll"]["backup_path"] = str(
                escaped_dir / fixture.old_dll.name
            )
            manifest_path.write_text(
                json.dumps(payload), encoding="utf-8"
            )
            with self.assertRaisesRegex(StagingError, "escapes its backup directory"):
                rollback_backup(
                    plan,
                    manifest_path,
                    process_checker=fixture.idle_process,
                    bridge_checker=fixture.idle_bridge,
                )
            # A rejected manifest must not touch the live files.
            self.assertEqual(fixture.old_dll.read_bytes(), b"candidate-bridge-dll")

    def test_symlink_components_are_rejected_before_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = StageFixture(Path(temp))
            link_root = fixture.root / "linked-stage"
            try:
                os.symlink(fixture.stage, link_root, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is unavailable in this environment")
            with self.assertRaisesRegex(StagingError, "symlink"):
                fixture.plan(
                    candidate_dll=link_root / fixture.candidate_dll.name,
                    candidate_manifest=link_root / fixture.candidate_manifest.name,
                )

    def test_candidate_artifact_manifest_provenance_and_hash_are_fixed(self) -> None:
        artifact_dir = Path(__file__).resolve().parents[1] / "artifacts" / "sts2mcp-seeded"
        candidate = artifact_dir / "STS2_MCP.json"
        provenance = artifact_dir / "candidate_manifest_provenance.json"
        self.assertTrue(candidate.is_file())
        self.assertTrue(provenance.is_file())
        payload = json.loads(provenance.read_text(encoding="utf-8"))
        candidate_hash = _digest(candidate)
        self.assertEqual(payload["candidate_manifest_path"], "artifacts/sts2mcp-seeded/STS2_MCP.json")
        self.assertEqual(payload["candidate_manifest_sha256"], candidate_hash)
        self.assertEqual(payload["source_manifest_path"], "third_party/STS2MCP-main/mod_manifest.json")
        source = artifact_dir.parents[2] / "third_party" / "STS2MCP-main" / "mod_manifest.json"
        self.assertEqual(payload["source_manifest_sha256"], _digest(source))
        self.assertEqual(candidate.read_bytes(), source.read_bytes())
        self.assertEqual(payload["reviewed_source"]["review_status"], "reviewed")

    def test_lock_game_version_mismatch_blocks_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = StageFixture(Path(temp))
            fixture.release.write_text(
                json.dumps(
                    {
                        "version": "v0.999.0",
                        "commit": "commit-1",
                        "main_assembly_hash": 123,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(Exception):
                fixture.plan()

    def test_real_process_and_port_helpers_are_fail_closed_primitives(self) -> None:
        # These checks are read-only.  Stub the OS probes so the test does not
        # depend on the sandbox's tasklist permissions or an incidental port.
        with patch(
            "scripts.stage_sts2mcp.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
        ):
            self.assertFalse(game_process_running("definitely-not-sts2.exe"))
        with patch("scripts.stage_sts2mcp.os.name", "posix"), patch(
            "scripts.stage_sts2mcp.socket.create_connection",
            side_effect=ConnectionRefusedError(),
        ):
            self.assertFalse(bridge_port_occupied("http://127.0.0.1:15526"))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "x"
            path.write_bytes(b"x")
            self.assertEqual(len(sha256_file(path)), 64)

    def test_windows_no_listener_is_free_even_when_connect_probe_times_out(self) -> None:
        # A local connect can time out because of filtering despite there
        # being no listener.  The Windows listener table is authoritative.
        with patch("scripts.stage_sts2mcp.os.name", "nt"), patch(
            "scripts.stage_sts2mcp.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout="FREE\n", stderr=""),
        ) as run, patch(
            "scripts.stage_sts2mcp.socket.create_connection",
            side_effect=TimeoutError("connect timed out"),
        ) as connect:
            self.assertFalse(bridge_port_occupied("http://127.0.0.1:15526"))
        run.assert_called_once()
        self.assertIn("Get-NetTCPConnection", run.call_args.args[0][-1])
        connect.assert_not_called()

    def test_windows_listener_table_blocks_when_port_is_listening(self) -> None:
        with patch("scripts.stage_sts2mcp.os.name", "nt"), patch(
            "scripts.stage_sts2mcp.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout="LISTEN\r\n", stderr=""),
        ) as run:
            self.assertTrue(bridge_port_occupied("http://127.0.0.1:15526"))
        run.assert_called_once()

    def test_windows_listener_query_parse_or_command_failure_blocks(self) -> None:
        for result in (
            SimpleNamespace(returncode=0, stdout="unexpected", stderr=""),
            SimpleNamespace(returncode=1, stdout="", stderr="access denied"),
        ):
            with self.subTest(result=result), patch("scripts.stage_sts2mcp.os.name", "nt"), patch(
                "scripts.stage_sts2mcp.subprocess.run", return_value=result
            ):
                with self.assertRaises(StagingError):
                    bridge_port_occupied("http://127.0.0.1:15526")

    def test_windows_listener_query_handles_decode_error_and_none_stderr(self) -> None:
        with patch("scripts.stage_sts2mcp.os.name", "nt"), patch(
            "scripts.stage_sts2mcp.subprocess.run",
            side_effect=UnicodeDecodeError("utf-8", b"\x80", 0, 1, "invalid byte"),
        ):
            with self.assertRaises(StagingError):
                bridge_port_occupied("http://127.0.0.1:15526")
        with patch("scripts.stage_sts2mcp.os.name", "nt"), patch(
            "scripts.stage_sts2mcp.subprocess.run",
            return_value=SimpleNamespace(returncode=1, stdout=None, stderr=None),
        ):
            with self.assertRaisesRegex(StagingError, "exit code 1"):
                bridge_port_occupied("http://127.0.0.1:15526")

    def test_windows_listener_command_is_utf8_ascii_and_cli_blocks_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = StageFixture(Path(temp))
            command_result = SimpleNamespace(returncode=0, stdout=b"FREE\n", stderr=None)

            def run_probe(command, **kwargs):
                if command[0].casefold() == "tasklist":
                    return SimpleNamespace(returncode=0, stdout=b"", stderr=None)
                return command_result

            with patch("scripts.stage_sts2mcp.os.name", "nt"), patch(
                "scripts.stage_sts2mcp.subprocess.run", side_effect=run_probe
            ) as run, patch("scripts.stage_sts2mcp.socket.create_connection") as connect:
                result = bridge_port_occupied("http://127.0.0.1:15526")
                self.assertFalse(result)
                listener_call = run.call_args_list[-1]
                command = listener_call.args[0]
                self.assertEqual(command[:4], [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                ])
                script = command[-1]
                script.encode("ascii")
                self.assertIn("[Console]::OutputEncoding", script)
                self.assertIn("$OutputEncoding", script)
                self.assertIn("Get-NetTCPConnection", script)
                self.assertIn("-State Listen -ErrorAction Stop", script)
                self.assertIn("Where-Object", script)
                self.assertIn("$_.LocalPort -eq 15526", script)
                self.assertNotIn("-State Listen -LocalPort", script)
                self.assertIn("'LISTEN'", script)
                self.assertIn("'FREE'", script)
                self.assertIs(listener_call.kwargs["text"], False)
                connect.assert_not_called()

            # The command/decode failure path is surfaced by main() as the
            # documented blocked JSON result, never as a traceback.
            def run_with_decode_failure(command, **kwargs):
                if command[0].casefold() == "tasklist":
                    return SimpleNamespace(returncode=0, stdout=b"", stderr=None)
                raise UnicodeDecodeError("utf-8", b"\x80", 0, 1, "invalid byte")

            args = [
                "--lock-file", str(fixture.lock),
                "--candidate-dll", str(fixture.candidate_dll),
                "--candidate-manifest", str(fixture.candidate_manifest),
                "--candidate-dll-sha256", _digest(fixture.candidate_dll),
                "--candidate-manifest-sha256", _digest(fixture.candidate_manifest),
                "--backup-root", str(fixture.backups),
                "--no-installed-game-check",
            ]
            captured = io.StringIO()
            with patch("scripts.stage_sts2mcp.os.name", "nt"), patch(
                "scripts.stage_sts2mcp.subprocess.run", side_effect=run_with_decode_failure
            ), redirect_stderr(captured):
                from scripts.stage_sts2mcp import main

                self.assertEqual(main(args), 2)
            blocked = json.loads(captured.getvalue())
            self.assertEqual(blocked["status"], "blocked")
            self.assertFalse(blocked["mutated"])


if __name__ == "__main__":
    unittest.main()
