"""One command to play a whole run, on the real client or in the simulator.

    python scripts/play.py --backend live
    python scripts/play.py --backend sim --checkpoint <path> --seeds 130008177

This is a dispatcher, not a second implementation. Every decision still belongs to the component
that owns it: the in-game CombatSolver plays combat, ``bridge.autoplay`` plays the screens between
fights, ``scripts/supervise_solver_batch.py`` runs and attests a live batch, and
``scripts/probe_three_act_campaign.py`` runs a simulator campaign. What this file adds is the part
nobody should have to remember -- bring the game up, fail before playing if the installed mods do
not match the lock, and say what the run concluded.

It does not touch the game's own state beyond launching it through Steam and calling the bridge.
The in-game auto-solver is left exactly as the operator set it, because that is the core.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR = ROOT / "scripts" / "supervise_solver_batch.py"
CAMPAIGN_PROBE = ROOT / "scripts" / "probe_three_act_campaign.py"

STEAM_APP_ID = 2868840
STEAM_EXE = Path(r"C:\Program Files (x86)\Steam\steam.exe")
DEFAULT_BASE_URL = "http://127.0.0.1:15526"


def bridge_ready(base_url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url}/", timeout=timeout) as response:
            return 200 <= response.status < 400
    except (urllib.error.URLError, OSError, ValueError):
        return False


def steam_running() -> bool:
    """Steam itself, not just the game: `-applaunch` is dropped if the client is down."""

    output = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq steamwebhelper.exe"],
        capture_output=True,
        text=True,
    ).stdout
    return "steamwebhelper" in output


def bring_up_game(base_url: str, wait_seconds: float) -> bool:
    if bridge_ready(base_url):
        return True
    if not STEAM_EXE.is_file():
        print(f"  no Steam client at {STEAM_EXE}; start the game and re-run")
        return False
    if not steam_running():
        print("  Steam is not running, so -applaunch would be dropped; start Steam and re-run")
        return False
    print(f"  launching app {STEAM_APP_ID} through Steam, waiting up to {wait_seconds:.0f}s")
    subprocess.run([str(STEAM_EXE), "-applaunch", str(STEAM_APP_ID)], check=False)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if bridge_ready(base_url):
            return True
        time.sleep(2.0)
    return False


def supervisor_command(args: argparse.Namespace, *, dry_run: bool) -> list[str]:
    # The supervisor resolves the solver lock, the game log directory and its own batch id, so
    # this wrapper only passes what it genuinely accepts.
    command = [
        sys.executable,
        str(SUPERVISOR),
        "--mode",
        "observational",
        "--base-url",
        args.base_url,
        "--max-battles",
        str(args.max_battles),
        "--max-runs",
        str(args.max_runs),
        "--game-wait-seconds",
        str(args.game_wait_seconds),
    ]
    if args.batch_id:
        command += ["--batch-id", args.batch_id]
    if args.max_seconds:
        command += ["--max-seconds", str(args.max_seconds)]
    if dry_run:
        command += ["--dry-run"]
    else:
        command += ["--allow-actions"]
    return command


def report_live_verdict(batch_id: str | None) -> None:
    """Print what the batch concluded, from the artifacts the batch itself wrote."""

    if not batch_id:
        return
    status = ROOT / "runs" / "solver_supervisor" / batch_id / "status.json"
    if not status.is_file():
        print(f"  no status file at {status}")
        return
    payload = json.loads(status.read_text(encoding="utf-8"))
    attestation = (payload.get("mod_attestation") or {}).get("at_start") or {}
    summary_path = (payload.get("evidence_paths") or {}).get("comparison_summary")
    verdict = {}
    if summary_path and Path(summary_path).is_file():
        verdict = (
            json.loads(Path(summary_path).read_text(encoding="utf-8")).get("verdict") or {}
        )
    print("  mod inventory matched the lock:", attestation.get("all_match_lock"))
    print("  battles:", verdict.get("n_battles"), "accepted:", verdict.get("accepted"))
    print("  acceptance blockers:", verdict.get("acceptance_blockers"))
    print("  run identity verified:", verdict.get("run_identity_verified"))
    for key in ("battles", "comparison_summary"):
        path = (payload.get("evidence_paths") or {}).get(key)
        if path:
            print(f"  {key}: {path}")


def run_live(args: argparse.Namespace) -> int:
    print(f"[1/3] bridge at {args.base_url}")
    if not bring_up_game(args.base_url, args.game_wait_seconds):
        print("  the game never answered on the bridge; nothing was played")
        return 2
    print("  bridge is up")

    print("[2/3] pre-flight through the supervisor itself, so the gate cannot drift from the run")
    preflight = supervisor_command(args, dry_run=True)
    print("  " + " ".join(preflight))
    if subprocess.call(preflight) != 0:
        print("  pre-flight refused; nothing was played")
        return 2
    print("  pre-flight passed")
    if args.preflight_only:
        print("[3/3] skipped: --preflight-only")
        return 0

    print("[3/3] playing (combat belongs to the in-game solver)")
    playing = supervisor_command(args, dry_run=False)
    print("  " + " ".join(playing))
    code = subprocess.call(playing)
    report_live_verdict(args.batch_id)
    return code


def run_sim(args: argparse.Namespace) -> int:
    command = [
        sys.executable,
        str(CAMPAIGN_PROBE),
        "--config",
        str(args.config),
        "--checkpoint",
        str(args.checkpoint),
        "--seeds",
        args.seeds,
    ]
    if args.max_steps:
        command += ["--max-steps", str(args.max_steps)]
    if args.output:
        command += ["--out", str(args.output)]
    print("  " + " ".join(command))
    return subprocess.call(command)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--backend", choices=("live", "sim"), required=True)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--game-wait-seconds", type=float, default=180.0)
    parser.add_argument("--max-battles", type=int, default=400)
    parser.add_argument("--max-runs", type=int, default=3)
    parser.add_argument("--max-seconds", type=int, default=None)
    parser.add_argument("--batch-id", help="only to report the finished batch's evidence")
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="check the bridge and the gates, then stop without playing a single action",
    )
    parser.add_argument("--config", type=Path, default=ROOT / "config/training_v2.toml")
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--seeds", help="comma-separated seeds; the probe refuses duplicates")
    parser.add_argument("--max-steps", type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.backend == "sim":
        if not args.checkpoint or not args.seeds:
            parser.error("--backend sim needs --checkpoint and --seeds")
        return run_sim(args)
    return run_live(args)


if __name__ == "__main__":
    raise SystemExit(main())
