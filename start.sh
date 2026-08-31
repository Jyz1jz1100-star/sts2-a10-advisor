#!/usr/bin/env bash
# Launch the advisor manually (bridge + overlay) on macOS/Linux.
set -euo pipefail
cd "$(dirname "$0")"
nohup python3 -m bridge.main >/dev/null 2>&1 &
nohup python3 overlay/overlay.py >/dev/null 2>&1 &
echo "Spire Oracle started (bridge + overlay). Launch StS2 and play!"
echo "Stop it with ./stop.sh"
