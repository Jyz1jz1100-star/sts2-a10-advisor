"""Autostart supervisor for Spire Oracle.

Runs at Windows login. Polls the STS2MCP health endpoint (which only answers
while StS2 is running with the mod loaded). When the game appears, it launches
the bridge + overlay; when the game closes, it tears the whole process tree down
(so no orphaned `claude` sessions linger). One lightweight process at idle.

Install it to run at login:   powershell autostart\install_autostart.ps1
Run manually:                 pythonw autostart\supervisor.py
Verify launch/teardown once:  python  autostart\supervisor.py --test
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

# Make the project root importable when launched as a bare script.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bridge import util  # noqa: E402
from bridge.client import BridgeClient  # noqa: E402

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _python_launcher() -> str:
    """On Windows prefer pythonw.exe (no console window). Elsewhere use the current
    interpreter (python3)."""
    if os.name == "nt":
        cand = Path(sys.executable).with_name("pythonw.exe")
        return str(cand) if cand.exists() else sys.executable
    return sys.executable


def _single_instance(port: int):
    """Bind a loopback port as a cross-process mutex; returns the socket or exits."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        s.listen(1)
        return s  # keep a reference alive for the process lifetime
    except OSError:
        print(f"Supervisor already running (port {port} in use). Exiting.")
        sys.exit(0)


def _bridge_client(cfg: dict) -> BridgeClient:
    b = cfg["bridge"]
    return BridgeClient(b["base_url"], b["state_path"], b["health_path"],
                        timeout=b.get("request_timeout_s", 4.0))


def _popen(args: list[str]) -> subprocess.Popen:
    kwargs: dict = {"cwd": str(ROOT)}
    if os.name == "nt":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    else:
        # Own process group so we can kill the whole tree (incl. `claude`) on POSIX.
        kwargs["start_new_session"] = True
    return subprocess.Popen(args, **kwargs)


def _launch_children(logger) -> list[subprocess.Popen]:
    py = _python_launcher()
    logger.info("Game detected -> launching bridge + overlay (%s)", py)
    bridge = _popen([py, "-m", "bridge.main"])
    overlay = _popen([py, str(ROOT / "overlay" / "overlay.py")])
    return [bridge, overlay]


def _stop_children(procs: list[subprocess.Popen], logger) -> None:
    if not procs:
        return
    logger.info("Game gone -> stopping advisor.")
    for p in procs:
        if p.poll() is not None:
            continue
        if os.name == "nt":
            # /T kills the whole tree, incl. any `claude` subprocesses.
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           creationflags=_CREATE_NO_WINDOW,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            # Kill the process group (the child + its `claude` descendants).
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                try:
                    p.terminate()
                except OSError:
                    pass
    for p in procs:
        try:
            p.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass


def _alive(procs: list[subprocess.Popen] | None) -> bool:
    return bool(procs) and all(p.poll() is None for p in procs)


def run() -> None:
    cfg = util.load_config()
    logger = util.setup_logging("runtime/supervisor.log")
    ac = cfg.get("autostart", {})
    interval = float(ac.get("check_interval_s", 3.0))
    grace = int(ac.get("grace_misses", 4))
    _single_instance(int(ac.get("mutex_port", 15580)))

    client = _bridge_client(cfg)
    procs: list[subprocess.Popen] | None = None
    misses = 0
    logger.info("Supervisor started; watching %s%s", client.base_url, client.health_path)

    try:
        while True:
            up = client.is_up()
            if up:
                misses = 0
                if not _alive(procs):
                    if procs:  # a child died while the game was up — restart cleanly
                        _stop_children(procs, logger)
                    procs = _launch_children(logger)
            else:
                misses += 1
                if procs and misses >= grace:
                    _stop_children(procs, logger)
                    procs = None
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        _stop_children(procs or [], logger)


def _test() -> int:
    """One launch/teardown cycle to confirm the wiring (safe, self-cleaning)."""
    cfg = util.load_config()
    logger = util.setup_logging("runtime/supervisor.log")
    client = _bridge_client(cfg)
    print("Game/bridge up:", client.is_up())
    print("python launcher:", _python_launcher())
    procs = _launch_children(logger)
    hb = util.resolve(cfg["paths"]["heartbeat_file"])
    advice = util.resolve(cfg["paths"]["advice_file"])
    print("Launched bridge + overlay; watching for heartbeat for ~8s ...")
    seen_hb = False
    for _ in range(16):
        time.sleep(0.5)
        if hb.exists():
            seen_hb = True
    print("  children alive:", _alive(procs))
    print("  heartbeat written:", seen_hb)
    print("  advice file exists:", advice.exists())
    _stop_children(procs, logger)
    print("  stopped children. alive now:", _alive(procs))
    print("\nRESULT:", "OK — autostart wiring works." if seen_hb
          else "children launched but no heartbeat (check runtime/advisor.log).")
    return 0 if seen_hb else 1


if __name__ == "__main__":
    if "--test" in sys.argv:
        raise SystemExit(_test())
    run()
