#!/usr/bin/env bash
# Install the Spire Oracle supervisor to run at login (macOS/Linux).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SUP="$ROOT/autostart/supervisor.py"
PY="$(command -v python3)"

case "$(uname -s)" in
  Linux)
    DIR="$HOME/.config/autostart"
    mkdir -p "$DIR"
    cat > "$DIR/spire-oracle.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Spire Oracle
Exec=$PY "$SUP"
X-GNOME-Autostart-enabled=true
Comment=Slay the Spire 2 advisor autostart supervisor
EOF
    echo "Installed Linux autostart: $DIR/spire-oracle.desktop"
    ;;
  Darwin)
    DIR="$HOME/Library/LaunchAgents"
    mkdir -p "$DIR"
    PLIST="$DIR/com.spireoracle.supervisor.plist"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.spireoracle.supervisor</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$SUP</string></array>
  <key>RunAtLoad</key><true/>
  <key>WorkingDirectory</key><string>$ROOT</string>
</dict></plist>
EOF
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
    echo "Installed macOS LaunchAgent: $PLIST"
    ;;
  *)
    echo "Unsupported OS: $(uname -s)"; exit 1 ;;
esac

echo "It will start at next login. Start it now without rebooting:"
echo "  ( cd \"$ROOT\" && \"$PY\" \"$SUP\" & )"
