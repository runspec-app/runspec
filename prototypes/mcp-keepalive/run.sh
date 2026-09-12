#!/usr/bin/env bash
# Keep-alive prototype runner. Spins up the streamable-HTTP MCP server, runs a
# 4-scenario timeout matrix, tears the server down. Each client run is wrapped in
# a hard shell timeout so a misbehaving transport can never hang the run.
#
# Needs an interpreter with mcp v1 installed:
#   python3 -m venv .venv && .venv/bin/pip install "mcp>=1.0,<2"
#   PYTHON=.venv/bin/python ./run.sh
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
  [ "$code" != "000" ] && break
  sleep 0.2
done
echo "server up at $URL (http=$code)"
echo

cli() { timeout 25 "$PYTHON" "$DIR/client.py" "$@" || echo "  -> (shell timeout guard fired)"; }

echo "A) per-request read_timeout=3s, park 8s, progress every 1s"
echo "   expect: HARD ceiling — fails at ~3s despite progress"
cli 8 1 3 0

echo "B) no per-request timeout, park 8s, progress every 1s, httpx read=default(300s)"
echo "   expect: COMPLETES at ~8s"
cli 8 1 0 0

echo "C) no per-request timeout, park 8s, NO progress, httpx read=3s"
echo "   expect: httpx POST-stream read idle fails at ~3s"
cli 8 0 0 3

echo "D) no per-request timeout, park 8s, progress every 1s, httpx read=3s"
echo "   expect: progress (SSE bytes) resets httpx read -> COMPLETES at ~8s"
cli 8 1 0 3
