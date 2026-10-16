#!/usr/bin/env bash
# Restore the Claude Code workspace as soon as the server (and, on GNOME, the window bridge) is ready (no fixed delay).
PORT="${CLAUDE_WS_PORT:-8765}"
PROFILE="$1"

# poll every 0.25s, give up waiting after 60s and restore anyway (windows then open unplaced)
for _ in $(seq 1 240); do
  curl -sf "http://localhost:$PORT/api/state" | grep -q '"bridge": true\|"bridge_expected": false' && break
  sleep 0.25
done

if [ -n "$PROFILE" ]; then
  body=$(python3 -c 'import json,sys; print(json.dumps({"profile": sys.argv[1]}))' "$PROFILE")
else
  body='{}'
fi
curl -s -X POST -H 'Content-Type: application/json' -d "$body" "http://localhost:$PORT/api/restore" >/dev/null
