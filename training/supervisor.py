from __future__ import annotations

import argparse
import json
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass
class RunManifest:
    run_id: str
    started_at: str
    command: list[str]
    game_build: str
    character: str
    ascension: int
    no_save_load: bool
    status: str = "running"
    exit_code: int | None = None


def atomic_json(path: Path, payload: dict) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Supervise a reproducible training process")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--game-build", required=True)
    parser.add_argument("--character", default="IRONCLAD")
    parser.add_argument("--ascension", type=int, default=10)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.command:
        parser.error("a child command is required after --")
    command = args.command[1:] if args.command[0] == "--" else args.command

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = args.run_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    manifest = RunManifest(
        run_id=run_id,
        started_at=datetime.now(UTC).isoformat(),
        command=command,
        game_build=args.game_build,
        character=args.character,
        ascension=args.ascension,
        no_save_load=True,
    )
    manifest_path = run_dir / "manifest.json"
    heartbeat = run_dir / "heartbeat.json"
    atomic_json(manifest_path, asdict(manifest))

    with (run_dir / "stdout.log").open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, text=True)
        while proc.poll() is None:
            atomic_json(
                heartbeat,
                {"time": datetime.now(UTC).isoformat(), "pid": proc.pid, "status": "running"},
            )
            time.sleep(5)
        manifest.exit_code = proc.returncode
        manifest.status = "completed" if proc.returncode == 0 else "failed"
        atomic_json(manifest_path, asdict(manifest))
        atomic_json(
            heartbeat,
            {"time": datetime.now(UTC).isoformat(), "pid": proc.pid, "status": manifest.status},
        )
    raise SystemExit(proc.returncode)


if __name__ == "__main__":
    main()
