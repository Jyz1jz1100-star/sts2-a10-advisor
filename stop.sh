#!/usr/bin/env bash
# Stop the advisor (bridge, overlay, and the autostart supervisor if running).
cd "$(dirname "$0")"
pkill -f 'bridge\.main' 2>/dev/null || true
pkill -f 'overlay/overlay\.py' 2>/dev/null || true
pkill -f 'autostart/supervisor\.py' 2>/dev/null || true
echo "Spire Oracle stopped."
