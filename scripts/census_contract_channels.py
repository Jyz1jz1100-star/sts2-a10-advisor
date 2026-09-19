#!/usr/bin/env python
"""Cross-tabulate the three contract channels the report quotes as one number.

``illegal_actions = 0`` is the only one of these the objective names, and it is the one
the wrapper can guarantee: in the default ``rejection_mode="filter"``
(training/v2_flat_env.py:164) an action the native layer refuses is removed from that
state's mask and the decision is handed back, so the policy never *executes* a
mask-illegal action. The disagreements are real and land in ``rejection_events``
instead; older-schema runs recorded them as episode endings
(``dead_end_reasons.native_rejection``) under the truncate-like semantics of that stack.

Walking every metrics file and tabulating the fields against each other is what stops
"0 illegal" being read as "mask and engine agreed". Prints, and can be diffed against
docs/evidence/contract_channels_20260919.json.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SKIP = {".venv", "__pycache__", ".git", ".cache", "node_modules"}


def metrics_files():
    for path in ROOT.rglob("*.json"):
        if SKIP.intersection(path.parts) or "metrics" not in path.parts:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            continue
        if isinstance(payload, dict):
            yield path, payload


def census():
    out = {
        "files_current_schema": 0, "files_legacy_schema": 0,
        "rejection_events": 0, "episodes": 0, "illegal_actions": 0,
        "dead_end_native_rejection_current": 0, "dead_end_native_rejection_legacy": 0,
        "legacy_dirs": collections.Counter(),
        "by_stage_split": collections.defaultdict(lambda: [0, 0]),
    }
    for path, payload in metrics_files():
        current = "rejection_events" in payload
        reasons = payload.get("dead_end_reasons") or {}
        native = int(reasons.get("native_rejection") or 0)
        events = int(payload.get("rejection_events") or 0)
        episodes = int(payload.get("episodes") or 0)
        if current:
            out["files_current_schema"] += 1
            out["rejection_events"] += events
            out["episodes"] += episodes
            out["illegal_actions"] += int(payload.get("illegal_actions") or 0)
            out["dead_end_native_rejection_current"] += native
            key = f"{payload.get('stage')}/{payload.get('split')}"
            out["by_stage_split"][key][0] += events
            out["by_stage_split"][key][1] += episodes
        else:
            out["files_legacy_schema"] += 1
            out["dead_end_native_rejection_legacy"] += native
            if native:
                out["legacy_dirs"][str(path.parent.parent)] += 1
    out["by_stage_split"] = dict(out["by_stage_split"])
    out["legacy_dirs"] = dict(out["legacy_dirs"])
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="dump the census as JSON")
    args = parser.parse_args()
    data = census()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        return 0
    print(f"files: {data['files_current_schema']} current-schema, "
          f"{data['files_legacy_schema']} legacy (no rejection_events field)")
    print(f"current-schema: illegal_actions={data['illegal_actions']} "
          f"rejection_events={data['rejection_events']} over episodes={data['episodes']} "
          f"({data['rejection_events'] / max(1, data['episodes']):.4f} per episode)")
    print(f"episodes ended by native_rejection: current={data['dead_end_native_rejection_current']} "
          f"legacy={data['dead_end_native_rejection_legacy']}")
    for key, (events, episodes) in sorted(data["by_stage_split"].items(), key=lambda kv: -kv[1][0]):
        if events:
            print(f"  {key}: {events} rejections / {episodes} episodes "
                  f"= {events / max(1, episodes):.4f}")
    for directory, count in sorted(data["legacy_dirs"].items(), key=lambda kv: -kv[1]):
        print(f"  legacy endings in {directory} ({count} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
