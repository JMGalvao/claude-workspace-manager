#!/usr/bin/env bash
# Setup of Claude Workspace on macOS (EXPERIMENTAL, written without access to a Mac: please report problems).
# Starts the server at login and gives you a command to open the manager. No workspace/Space placement on macOS.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.claudeworkspace.server"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY="$(command -v python3)"

chmod +x "$DIR/server.py" "$DIR/trust.py" "$DIR/open-manager.sh" "$DIR/restore-on-login.sh"
[ -f "$DIR/workspace.json" ] || cp "$DIR/workspace.example.json" "$DIR/workspace.json"

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
<key>Label</key><string>$LABEL</string>
<key>ProgramArguments</key><array><string>$PY</string><string>$DIR/server.py</string></array>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
<key>WorkingDirectory</key><string>$DIR</string>
<key>StandardErrorPath</key><string>/tmp/claude-workspace.err</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "Servidor a correr em http://localhost:8765"
echo "Abrir o gestor: $DIR/open-manager.sh"
echo "Na primeira vez que abrir um terminal, o macOS pede permissão para o Terminal ser controlado (Automação): aceita."
