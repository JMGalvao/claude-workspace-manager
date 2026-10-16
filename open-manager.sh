#!/usr/bin/env bash
# Open the Claude Workspace manager as an app window (no browser chrome); focus it if it is already open.
PORT="${CLAUDE_WS_PORT:-8765}"
URL="http://localhost:$PORT"

if [ "$(uname)" = "Darwin" ]; then
  exec open "$URL"
fi

# already open? ask the GNOME bridge for a window titled "Claude Workspace" and activate it
python3 - <<'PY' && exit 0
import ast, json, subprocess, sys
B = ["gdbus", "call", "--session", "--dest", "org.claudews.Bridge", "--object-path", "/org/claudews/Bridge", "--method"]
try:
    out = subprocess.run(B + ["org.claudews.Bridge.ListWindows"], capture_output=True, text=True, timeout=3).stdout
    wins = json.loads(ast.literal_eval(out.strip())[0])
    w = next(w for w in wins if w["title"].startswith("Claude Workspace") and "terminal" not in w["cls"].lower())
    subprocess.run(B + ["org.claudews.Bridge.ActivateWindow", f"uint64 {w['id']}"], capture_output=True, timeout=3)
except Exception:
    sys.exit(1)
PY

for b in chromium chromium-browser google-chrome google-chrome-stable brave-browser; do
  if command -v "$b" >/dev/null 2>&1; then
    exec "$b" --app="$URL" --window-size=1360,860 >/dev/null 2>&1
  fi
done
exec xdg-open "$URL"
