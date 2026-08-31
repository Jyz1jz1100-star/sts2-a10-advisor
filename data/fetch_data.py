"""Download the StS2 card/relic dataset locally.

Pulls eng cards.json + relics.json from the spire-codex datamine
(https://github.com/ptrlrd/spire-codex, PolyForm Noncommercial — personal use)
into this folder, where grounding.py reads them. Re-run after big game patches.

    python data/fetch_data.py

If GitHub is unreachable, manually download the two raw files (URLs below) into
this folder.
"""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
RAW = "https://raw.githubusercontent.com/ptrlrd/spire-codex/main/data/eng"
SOURCES = {
    "cards.json": f"{RAW}/cards.json",
    "relics.json": f"{RAW}/relics.json",
}


def fetch(url: str) -> object:
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


def main() -> int:
    ok = True
    for fname, url in SOURCES.items():
        try:
            data = fetch(url)
            (HERE / fname).write_text(json.dumps(data, indent=0), encoding="utf-8")
            count = len(data) if isinstance(data, (list, dict)) else "?"
            print(f"  {fname}: {count} entries  <- {url}")
        except (requests.RequestException, ValueError) as exc:
            ok = False
            print(f"  FAILED {fname}: {exc}\n    Download manually from: {url}")

    (HERE / "version.json").write_text(json.dumps({
        "game_version": "unknown",
        "source": "ptrlrd/spire-codex (data/eng)",
        "fetched_utc": datetime.datetime.utcnow().isoformat() + "Z",
        "note": "StS2 is in Early Access; card/relic text changes ~biweekly. "
                "Re-run this after major patches.",
    }, indent=2), encoding="utf-8")

    if ok:
        print("Done. Dataset written to", HERE)
    else:
        print("Some downloads failed — see messages above.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
