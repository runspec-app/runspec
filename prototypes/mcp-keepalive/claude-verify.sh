#!/usr/bin/env bash
# Verify the parked-confirm keep-alive behaviour against the REAL Claude Code CLI
# (not just the SDK client). Starts the prototype MCP server, then drives
# `claude -p` to call the `park` tool under three conditions.
#
# Needs: the `claude` CLI on PATH and authenticated; an mcp-v1 interpreter for the
# server (python3 -m venv .venv && .venv/bin/pip install "mcp>=1.0,<2").
#   PYTHON=.venv/bin/python ./claude-verify.sh
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${PYTHON:-python3}"
PORT="${PORT:-8765}"
export URL="http://127.0.0.1:${PORT}/mcp"

PORT="$PORT" "$PYTHON" "$DIR/server.py" >/tmp/keepalive-server.log 2>&1 &
SRV=$!
trap 'kill -9 $SRV 2>/dev/null' EXIT
for _ in $(seq 1 50); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "$URL" 2>/dev/null || echo 000)
  [ "$code" != "000" ] && [ -n "$code" ] && break
  sleep 0.2
done
echo "server up (http=$code)"; echo

ask() {  # <park_seconds> <tool_timeout_ms>
  local secs="$1" tmo="$2" start
  start=$(date +%s)
  MCP_TOOL_TIMEOUT="$tmo" timeout 160 claude -p \
    "Call the MCP tool 'park' exactly once with arguments seconds=$secs and interval=5. Then report ONLY the tool's exact returned text, or the exact error if it fails. Do not call it more than once." \
    --mcp-config "$DIR/claude-mcp.json" \
    --allowedTools "mcp__keepalive__park" --max-turns 3 2>&1 | tail -4
  echo "   (wall clock: $(( $(date +%s) - start ))s)"; echo
}

echo "T1) park 20s, MCP_TOOL_TIMEOUT=60000 (default)  -> expect COMPLETES"
ask 20 60000
echo "T2) park 90s, MCP_TOOL_TIMEOUT=60000 (default)  -> expect TIMES OUT at 60s (progress does NOT save it)"
ask 90 60000
echo "T3) park 90s, MCP_TOOL_TIMEOUT=120000           -> expect COMPLETES (ceiling raised)"
ask 90 120000
