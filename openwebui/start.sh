#!/usr/bin/env bash
# Start the live demo: nl2sql MCP server (MIMIC-IV demo DB) + Open WebUI, both on localhost.
#   openwebui/start.sh        -> waits until ready, configures Open WebUI, then open http://127.0.0.1:8080
#   openwebui/stop.sh
# Prerequisites: uv tool install --python 3.11 open-webui ; scripts/get_mimic_db.sh ; OPENROUTER_API_KEY
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(dirname "$HERE")"
STATE="$HOME/.local/share/nl2sql"
LOGS="$STATE/logs"
OW_DATA="$HOME/.local/share/open-webui"      # outside iCloud
VENV="${UV_PROJECT_ENVIRONMENT:-$HOME/.venvs/nl2sql}"   # outside the project: iCloud-safe
MCP_PORT=8765
OW_PORT=8080
mkdir -p "$LOGS" "$OW_DATA"

# Database: NL2SQL_MIMIC_DB, else data/mimic_iv.sqlite (scripts/get_mimic_db.sh), else ../m3_repro/db
MIMIC_DB="${NL2SQL_MIMIC_DB:-}"
if [ -z "$MIMIC_DB" ]; then
  for candidate in "$PROJECT/data/mimic_iv.sqlite" "$PROJECT/../m3_repro/db/mimic_iv.sqlite"; do
    if [ -f "$candidate" ]; then MIMIC_DB="$(cd "$(dirname "$candidate")" && pwd)/mimic_iv.sqlite"; break; fi
  done
fi
if [ -z "$MIMIC_DB" ] || [ ! -f "$MIMIC_DB" ]; then
  echo "MIMIC-IV database not found. Download it with: scripts/get_mimic_db.sh" >&2
  exit 1
fi
command -v lsof >/dev/null || { echo "lsof is required (macOS/Linux)" >&2; exit 1; }
[ -x "$HOME/.local/bin/open-webui" ] || { echo "Open WebUI not installed. Run: uv tool install --python 3.11 open-webui" >&2; exit 1; }

# OpenRouter key: environment, else nl2sql/.env, else ../m3_repro/.env (never printed)
if [ -z "${OPENROUTER_API_KEY:-}" ]; then
  for f in "$PROJECT/.env" "$PROJECT/../m3_repro/.env"; do
    if [ -f "$f" ]; then OPENROUTER_API_KEY="$(grep -E '^OPENROUTER_API_KEY=' "$f" | head -1 | cut -d= -f2- | tr -d '"'"'")"; fi
    [ -n "${OPENROUTER_API_KEY:-}" ] && break
  done
fi
[ -n "${OPENROUTER_API_KEY:-}" ] || { echo "OPENROUTER_API_KEY not found" >&2; exit 1; }

# Stable secret key so sessions/tool connections survive restarts (Open WebUI requirement for MCP)
if [ ! -f "$OW_DATA/.webui_secret_key" ]; then
  (umask 077; python3 -c "import secrets; print(secrets.token_urlsafe(48))" > "$OW_DATA/.webui_secret_key")
fi

listening() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

if listening $MCP_PORT; then
  echo "MCP server already running on :$MCP_PORT"
else
  (cd "$PROJECT" && UV_PROJECT_ENVIRONMENT="$VENV" nohup uv run nl2sql mcp \
     --db "sqlite:///$MIMIC_DB" --port $MCP_PORT --name "MIMIC-IV demo" > "$LOGS/mcp-mimic.log" 2>&1 &
   echo $! > "$STATE/mcp.pid")
  echo "MCP server starting on :$MCP_PORT (log: $LOGS/mcp-mimic.log)"
fi

if listening $OW_PORT; then
  echo "Open WebUI already running on :$OW_PORT"
else
  TOOL_SERVERS=$(cat <<JSON
[{"type": "mcp", "url": "http://127.0.0.1:$MCP_PORT/mcp", "path": "", "spec_type": "url", "spec": "",
  "auth_type": "none", "key": "", "config": {"enable": true},
  "info": {"id": "mimic", "name": "MIMIC-IV SQL tools", "description": "Read-only SQL tools for the MIMIC-IV demo database"}}]
JSON
)
  DATA_DIR="$OW_DATA" \
  WEBUI_SECRET_KEY="$(cat "$OW_DATA/.webui_secret_key")" \
  WEBUI_AUTH=False \
  ENABLE_OLLAMA_API=False \
  ENABLE_OPENAI_API=True \
  OPENAI_API_BASE_URL="https://openrouter.ai/api/v1" \
  OPENAI_API_KEY="$OPENROUTER_API_KEY" \
  TOOL_SERVER_CONNECTIONS="$TOOL_SERVERS" \
  ENABLE_VERSION_UPDATE_CHECK=False \
  nohup "$HOME/.local/bin/open-webui" serve --host 127.0.0.1 --port $OW_PORT > "$LOGS/open-webui.log" 2>&1 &
  echo $! > "$STATE/open-webui.pid"
  echo "Open WebUI starting on :$OW_PORT (log: $LOGS/open-webui.log) — first start takes a minute"
fi

printf "Waiting for Open WebUI"
for _ in $(seq 1 120); do
  if curl -fs -m 2 "http://127.0.0.1:$OW_PORT/health" >/dev/null 2>&1; then break; fi
  printf "."; sleep 2
done
echo
curl -fs -m 2 "http://127.0.0.1:$OW_PORT/health" >/dev/null 2>&1 || { echo "Open WebUI did not become ready; see $LOGS/open-webui.log" >&2; exit 1; }

# Idempotent: connection, model preset, default model
(cd "$PROJECT" && NL2SQL_MIMIC_DB="$MIMIC_DB" UV_PROJECT_ENVIRONMENT="$VENV" uv run python openwebui/configure.py)
echo "Ready: http://127.0.0.1:$OW_PORT"
