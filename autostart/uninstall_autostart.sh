#!/usr/bin/env bash
# Remove the Spire Oracle autostart entry and stop a running supervisor (macOS/Linux).
case "$(uname -s)" in
  Linux)
    rm -f "$HOME/.config/autostart/spire-oracle.desktop"
    echo "Removed Linux autostart entry."
    ;;
  Darwin)
    PLIST="$HOME/Library/LaunchAgents/com.spireoracle.supervisor.plist"
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Removed macOS LaunchAgent."
    ;;
esac
pkill -f 'autostart/supervisor\.py' 2>/dev/null || true
pkill -f 'bridge\.main' 2>/dev/null || true
pkill -f 'overlay/overlay\.py' 2>/dev/null || true
echo "Done."
