from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from .explain import render_zh
from .policy import Policy, SmokeBaselinePolicy
from .policy_live import LiveHeuristicPolicy


def fetch_json(url: str, timeout: float) -> dict:
    request = Request(url, method="GET", headers={"Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - localhost by CLI
        return json.loads(response.read().decode("utf-8"))


def build_policy(name: str) -> Policy:
    if name == "heuristic":
        return LiveHeuristicPolicy()
    if name == "smoke":
        return SmokeBaselinePolicy()
    raise ValueError(f"unknown policy {name!r}; expected heuristic|smoke")


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only local STS2 advisor")
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:15526/api/v1/singleplayer?format=json",
    )
    parser.add_argument(
        "--output", type=Path, default=Path("runtime/latest_advice.txt")
    )
    parser.add_argument("--heartbeat", type=Path, default=Path("runtime/heartbeat"))
    parser.add_argument("--poll", type=float, default=0.25)
    parser.add_argument(
        "--policy",
        default="heuristic",
        choices=["heuristic", "smoke"],
        help="heuristic = A10 out-of-combat rules (combat screens stay quiet, "
        "the Combat Solver overlay owns them); smoke = integration baseline",
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    policy = build_policy(args.policy)
    previous = None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.heartbeat.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            state = fetch_json(args.url, timeout=1.0)
            args.heartbeat.touch()
            signature = json.dumps(state, sort_keys=True, ensure_ascii=False)
            if signature != previous:
                try:
                    args.output.write_text(
                        render_zh(policy.recommend(state)), encoding="utf-8"
                    )
                except ValueError:
                    # unsupported screen (combat, event, neow, ...): clear the
                    # overlay instead of showing stale advice from another screen
                    args.output.write_text(
                        "本屏暂不介入（战斗建议见 Combat Solver 面板；"
                        "事件/Neow 尚无规则依据）。",
                        encoding="utf-8",
                    )
                previous = signature
        except (URLError, TimeoutError, json.JSONDecodeError):
            pass
        if args.once:
            return
        time.sleep(args.poll)


if __name__ == "__main__":
    main()
