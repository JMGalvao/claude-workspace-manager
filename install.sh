#!/usr/bin/env bash
# Idempotent setup of Claude Workspace on this machine. Safe to run again after moving the folder.
set -euo pipefail
# Usage: ./install.sh [--trust-folders]
#   --trust-folders  also add a claude() shell function that pre-accepts the "trust this folder" dialog
#                    (it edits ~/.claude.json, see README). Off by default.
TRUST=0
[ "${1:-}" = "--trust-folders" ] && TRUST=1
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXT_UUID="claude-workspace-bridge@local"
EXT_LINK="$HOME/.local/share/gnome-shell/extensions/$EXT_UUID"

[ -f "$DIR/workspace.json" ] || cp "$DIR/workspace.example.json" "$DIR/workspace.json"
chmod +x "$DIR/server.py" "$DIR/trust.py" "$DIR/open-manager.sh" "$DIR/restore-on-login.sh"

# 1. GNOME extension: link the source folder into the shell's extension directory
mkdir -p "$(dirname "$EXT_LINK")"
if [ -e "$EXT_LINK" ] && [ ! -L "$EXT_LINK" ]; then
  mv "$EXT_LINK" "$EXT_LINK.bak.$(date +%s)"
fi
ln -sfn "$DIR/extension" "$EXT_LINK"
python3 - "$EXT_UUID" <<'PY'
import ast, subprocess, sys
uuid = sys.argv[1]
cur = ast.literal_eval(subprocess.check_output(
    ["gsettings", "get", "org.gnome.shell", "enabled-extensions"], text=True).strip().replace("@as ", ""))
if uuid not in cur:
    cur.append(uuid)
    subprocess.check_call(["gsettings", "set", "org.gnome.shell", "enabled-extensions", str(cur)])
PY

# 2. systemd user service (always-on server)
mkdir -p "$HOME/.config/systemd/user"
sed "s|@DIR@|$DIR|g" "$DIR/system/claude-workspace.service" > "$HOME/.config/systemd/user/claude-workspace.service"
systemctl --user daemon-reload
systemctl --user enable claude-workspace.service >/dev/null 2>&1
systemctl --user restart claude-workspace.service

# 3. application menu entry
mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/claude-workspace.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=Claude Workspace
Comment=Restaurar e gerir as sessões do Claude Code
Exec=$DIR/open-manager.sh
Icon=$DIR/icon.svg
Terminal=false
Categories=Development;
DESK

# 4. login restore entry: only repoint it if the user already enabled it
AUTO="$HOME/.config/autostart/claude-workspace.desktop"
if [ -f "$AUTO" ]; then
  sed -i "s|^Exec=.*restore-on-login.sh|Exec=$DIR/restore-on-login.sh|" "$AUTO"
fi

# 5. (opt-in) shell function: trust the current folder before starting claude
BLOCK_MARK="# Claude Workspace: trust the current folder"
if [ "$TRUST" = 0 ]; then
  echo "Pasta confiável automática: desligada (usa --trust-folders para ativar)."
elif grep -q "claude-workspace/trust.py\|$BLOCK_MARK" "$HOME/.bashrc"; then
  sed -i "s|python3 \"[^\"]*trust.py\"|python3 \"$DIR/trust.py\"|" "$HOME/.bashrc"
else
  cat >> "$HOME/.bashrc" <<RC

$BLOCK_MARK before starting claude (no "trust this folder?" dialog)
claude() {
  python3 "$DIR/trust.py" "\$PWD" 2>/dev/null
  command claude "\$@"
}
RC
fi

# 6. keyboard shortcut Super+W -> open the manager
python3 - "$DIR" <<'PY'
import ast, subprocess, sys
d = sys.argv[1]
K = "org.gnome.settings-daemon.plugins.media-keys"
path = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/claude-workspace/"
cur = ast.literal_eval(subprocess.check_output(["gsettings", "get", K, "custom-keybindings"], text=True).strip().replace("@as ", ""))
if path not in cur:
    cur.append(path)
    subprocess.check_call(["gsettings", "set", K, "custom-keybindings", str(cur)])
s = f"{K}.custom-keybinding:{path}"
subprocess.check_call(["gsettings", "set", s, "name", "Claude Workspace"])
subprocess.check_call(["gsettings", "set", s, "command", f"{d}/open-manager.sh"])
subprocess.check_call(["gsettings", "set", s, "binding", "<Super>w"])
PY

echo "Instalado a partir de $DIR"
echo "Se a extensão é nova neste PC, termina sessão e volta a entrar uma vez."
echo "Gestor: http://localhost:8765  (Super+W)"
