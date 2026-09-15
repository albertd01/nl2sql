#!/usr/bin/env bash
# Stop the live demo processes started by openwebui/start.sh.
STATE="$HOME/.local/share/nl2sql"
for name in open-webui mcp; do
  pidfile="$STATE/$name.pid"
  if [ -f "$pidfile" ] && kill "$(cat "$pidfile")" 2>/dev/null; then echo "stopped $name"; fi
  rm -f "$pidfile"
done
# uv run / serve spawn children; make sure the ports are free
for port in 8080 8765; do
  pids=$(lsof -nP -tiTCP:$port -sTCP:LISTEN 2>/dev/null) && [ -n "$pids" ] && kill $pids && echo "freed :$port"
done
exit 0
