#!/usr/bin/env bash
# Stop the demo processes started by openwebui/start.sh. Only processes that belong to the demo
# (Open WebUI, `nl2sql mcp`) are stopped; other programs on the same ports are left alone.
OW_PORT="${NL2SQL_OW_PORT:-8080}"
MCP_PORT="${NL2SQL_MCP_PORT:-8765}"
STATE="$HOME/.local/share/nl2sql"

stop_port() {  # port, pattern the process command must match, label
  local port="$1" pattern="$2" label="$3" stopped=false
  for pid in $(lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null); do
    cmd="$(ps -p "$pid" -o command= 2>/dev/null)"
    if [[ "$cmd" == $pattern ]]; then
      kill "$pid" 2>/dev/null && stopped=true
    else
      echo "Not stopping :$port — it belongs to another program: ${cmd:0:80}"
    fi
  done
  $stopped && echo "stopped $label (:$port)"
  return 0
}

# Launcher processes (uv run, nohup) first, then whatever still listens on the ports.
for f in "$STATE/open-webui-$OW_PORT.pid" "$STATE/mcp-$MCP_PORT.pid"; do
  if [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null; then echo "stopped $(basename "$f" .pid)"; fi
  rm -f "$f"
done
sleep 1
stop_port "$OW_PORT" "*open-webui*" "Open WebUI"
stop_port "$MCP_PORT" "*nl2sql*mcp*" "MCP server"
exit 0
