#!/usr/bin/env bash
# Start the live demo: nl2sql MCP server (MIMIC-IV demo DB) + Open WebUI, both on localhost only.
# Waits until Open WebUI is ready, configures it (idempotent) and prints the URL.
#
#   openwebui/start.sh                    # then open http://127.0.0.1:8080
#   openwebui/stop.sh
#
# Prerequisites (see README "Try the demo"): uv, `uv tool install --python 3.11 open-webui`,
# scripts/get_mimic_db.sh, OPENROUTER_API_KEY in the environment or in .env.
#
# Optional environment: NL2SQL_OW_PORT (8080), NL2SQL_MCP_PORT (8765), NL2SQL_MIMIC_DB,
# NL2SQL_OW_DATA (~/.local/share/open-webui), UV_PROJECT_ENVIRONMENT (~/.venvs/nl2sql).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(dirname "$HERE")"
OW_PORT="${NL2SQL_OW_PORT:-8080}"
MCP_PORT="${NL2SQL_MCP_PORT:-8765}"
OW_DATA="${NL2SQL_OW_DATA:-$HOME/.local/share/open-webui}"
VENV="${UV_PROJECT_ENVIRONMENT:-$HOME/.venvs/nl2sql}"   # outside the project folder (iCloud-safe)
STATE="$HOME/.local/share/nl2sql"
LOGS="$STATE/logs"
OW_URL="http://127.0.0.1:$OW_PORT"

fail() { echo "ERROR: $*" >&2; exit 1; }

# --- prerequisites -----------------------------------------------------------------------------
command -v uv >/dev/null || fail "uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
command -v curl >/dev/null || fail "curl not found."
command -v lsof >/dev/null || fail "lsof not found (macOS has it; Debian/Ubuntu: sudo apt install lsof)."

OPEN_WEBUI="$(command -v open-webui || true)"
[ -n "$OPEN_WEBUI" ] || OPEN_WEBUI="$(uv tool dir --bin 2>/dev/null)/open-webui"
[ -x "$OPEN_WEBUI" ] || fail "Open WebUI not installed. Run: uv tool install --python 3.11 open-webui"

# Database: NL2SQL_MIMIC_DB, else data/mimic_iv.sqlite, else ../m3_repro/db (original author's layout)
MIMIC_DB="${NL2SQL_MIMIC_DB:-}"
if [ -z "$MIMIC_DB" ]; then
  for candidate in "$PROJECT/data/mimic_iv.sqlite" "$PROJECT/../m3_repro/db/mimic_iv.sqlite"; do
    if [ -f "$candidate" ]; then MIMIC_DB="$(cd "$(dirname "$candidate")" && pwd)/mimic_iv.sqlite"; break; fi
  done
fi
[ -n "$MIMIC_DB" ] && [ -f "$MIMIC_DB" ] || fail "MIMIC-IV database not found. Run: scripts/get_mimic_db.sh"

# OpenRouter key: environment, else .env (never printed)
if [ -z "${OPENROUTER_API_KEY:-}" ]; then
  for f in "$PROJECT/.env" "$PROJECT/../m3_repro/.env"; do
    if [ -f "$f" ]; then
      OPENROUTER_API_KEY="$(grep -E '^OPENROUTER_API_KEY=' "$f" | head -1 | cut -d= -f2- | tr -d '"'"'" | tr -d '[:space:]')"
    fi
    [ -n "${OPENROUTER_API_KEY:-}" ] && break
  done
fi
[ -n "${OPENROUTER_API_KEY:-}" ] || fail "OPENROUTER_API_KEY not set. Put OPENROUTER_API_KEY=... into $PROJECT/.env"

mkdir -p "$LOGS" "$OW_DATA"

# --- ports: free, or already ours ----------------------------------------------------------------
port_pid() { lsof -nP -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null | head -1; }
port_cmd() { ps -p "$1" -o command= 2>/dev/null || true; }

MCP_RUNNING=false
if pid="$(port_pid "$MCP_PORT")" && [ -n "$pid" ]; then
  case "$(port_cmd "$pid")" in
    *nl2sql*mcp*) MCP_RUNNING=true ;;
    *) fail "Port $MCP_PORT is used by another program ($(port_cmd "$pid" | cut -c1-80)). Set NL2SQL_MCP_PORT to a free port." ;;
  esac
fi
OW_RUNNING=false
if pid="$(port_pid "$OW_PORT")" && [ -n "$pid" ]; then
  if curl -fs -m 3 "$OW_URL/health" | grep -q '"status"'; then
    OW_RUNNING=true
  else
    fail "Port $OW_PORT is used by another program ($(port_cmd "$pid" | cut -c1-80)). Set NL2SQL_OW_PORT to a free port."
  fi
fi

# --- start ------------------------------------------------------------------------------------------
if $MCP_RUNNING; then
  rm -f "$STATE/mcp-$MCP_PORT.pid"   # not launched by this run: don't treat it as ours to watch
  echo "MCP server already running on :$MCP_PORT"
else
  echo "Preparing Python environment ($VENV)..."
  (cd "$PROJECT" && UV_PROJECT_ENVIRONMENT="$VENV" uv sync -q)
  # Background processes get their own stdin/stdout/stderr (a plain `cmd > log 2>&1 < /dev/null &`,
  # no subshell): otherwise they keep this script's output pipe open and callers reading it
  # (e.g. coding agents) wait forever.
  UV_PROJECT_ENVIRONMENT="$VENV" nohup uv run --project "$PROJECT" nl2sql mcp \
     --db "sqlite:///$MIMIC_DB" --port "$MCP_PORT" --name "MIMIC-IV demo" \
     > "$LOGS/mcp-$MCP_PORT.log" 2>&1 < /dev/null &
  echo $! > "$STATE/mcp-$MCP_PORT.pid"
  echo "MCP server starting on :$MCP_PORT (log: $LOGS/mcp-$MCP_PORT.log)"
fi

if $OW_RUNNING; then
  rm -f "$STATE/open-webui-$OW_PORT.pid"
  echo "Open WebUI already running on :$OW_PORT"
else
  # Stable secret key so sessions and tool connections survive restarts (needed for MCP)
  if [ ! -f "$OW_DATA/.webui_secret_key" ]; then
    (umask 077; python3 -c "import secrets; print(secrets.token_urlsafe(48))" > "$OW_DATA/.webui_secret_key")
  fi
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
  nohup "$OPEN_WEBUI" serve --host 127.0.0.1 --port "$OW_PORT" > "$LOGS/open-webui-$OW_PORT.log" 2>&1 < /dev/null &
  echo $! > "$STATE/open-webui-$OW_PORT.pid"
  echo "Open WebUI starting on :$OW_PORT (log: $LOGS/open-webui-$OW_PORT.log)"
fi

# --- wait, then configure --------------------------------------------------------------------------
died() {  # pidfile, log — true if a process we launched has already exited
  [ -f "$1" ] && ! kill -0 "$(cat "$1")" 2>/dev/null
}
show_log_and_fail() {
  echo; echo "--- last lines of $2:" >&2; tail -15 "$2" >&2; fail "$1"
}

printf "Waiting for Open WebUI (first start can take several minutes)"
for _ in $(seq 1 180); do
  curl -fs -m 2 "$OW_URL/health" >/dev/null 2>&1 && break
  died "$STATE/open-webui-$OW_PORT.pid" && show_log_and_fail "Open WebUI exited during startup." "$LOGS/open-webui-$OW_PORT.log"
  died "$STATE/mcp-$MCP_PORT.pid" && show_log_and_fail "MCP server exited during startup." "$LOGS/mcp-$MCP_PORT.log"
  printf "."; sleep 2
done
echo
curl -fs -m 2 "$OW_URL/health" >/dev/null 2>&1 || fail "Open WebUI did not become ready within 6 minutes. See $LOGS/open-webui-$OW_PORT.log"

for _ in $(seq 1 30); do
  [ -n "$(port_pid "$MCP_PORT")" ] && break
  died "$STATE/mcp-$MCP_PORT.pid" && show_log_and_fail "MCP server exited during startup." "$LOGS/mcp-$MCP_PORT.log"
  sleep 1
done
[ -n "$(port_pid "$MCP_PORT")" ] || fail "MCP server did not start. See $LOGS/mcp-$MCP_PORT.log"

(cd "$PROJECT" && NL2SQL_MIMIC_DB="$MIMIC_DB" NL2SQL_OW_URL="$OW_URL" UV_PROJECT_ENVIRONMENT="$VENV" \
   uv run python openwebui/configure.py)
echo "READY: $OW_URL"
