#!/usr/bin/env bash
# Spire Oracle — one-shot setup for macOS/Linux.  Run:  bash install.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "============================================"
echo "  Spire Oracle setup"
echo "============================================"

# 1. Python 3.11+ -----------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  echo "[X] python3 not found. Install Python 3.11+ from https://www.python.org/downloads/"
  exit 1
fi
PYV=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,11) else 1)'; then
  echo "[X] Python $PYV found, but 3.11+ is required."
  exit 1
fi
echo "[OK] Python $PYV"

# 2. tkinter (overlay) — a system package, can't be pip-installed -----------
if ! python3 -c 'import tkinter' >/dev/null 2>&1; then
  echo "[!] Python is missing tkinter (the overlay needs it)."
  echo "    Debian/Ubuntu: sudo apt install python3-tk"
  echo "    Fedora:        sudo dnf install python3-tkinter"
  echo "    macOS (brew):  brew install python-tk"
fi

# 3. Claude Code CLI (required for advice) ----------------------------------
if ! command -v claude >/dev/null 2>&1; then
  echo "[!] 'claude' CLI not found. The advisor needs Claude Code (your subscription)."
  echo "    Install + sign in: https://claude.com/claude-code"
fi

# 4. Dependencies + dataset -------------------------------------------------
echo "Installing Python dependencies..."
python3 -m pip install -r requirements.txt
echo "Downloading StS2 card/relic dataset..."
python3 data/fetch_data.py

# 5. Validate the CLI -------------------------------------------------------
if command -v claude >/dev/null 2>&1; then
  echo "Validating Claude Code CLI (calls the model once)..."
  python3 -m bridge.model_client --selftest || \
    echo "[!] Selftest issue; re-run later: python3 -m bridge.model_client --selftest"
fi

# 6. Mod reachability hint --------------------------------------------------
if command -v curl >/dev/null 2>&1; then
  if curl -fsS http://127.0.0.1:15526/ >/dev/null 2>&1; then
    echo "[OK] STS2MCP detected"
  else
    echo "[i] STS2MCP not detected (fine if StS2 isn't running). Install + enable:"
    echo "    https://github.com/Gennadiyev/STS2MCP"
  fi
fi

echo ""
echo "Setup complete."
echo "  Run now:        ./start.sh    (stop with ./stop.sh)"
echo "  Auto-launch:    bash autostart/install_autostart.sh"
