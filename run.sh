#!/bin/bash
# Launch the APISIX MCP server (port 8002) and the mcpo OpenAPI proxy (port 8000).
# OpenWebUI connects to mcpo on 8000 as an *OpenAPI* tool server.
#   URL:  http://host.docker.internal:8000
set -e

MCPO="$(command -v mcpo || echo "$HOME/Library/Python/3.14/bin/mcpo")"
cd "$(dirname "$0")"

# Load .env ourselves (exported, so child processes see it) rather than
# relying on the caller to `source .env` first — plain `source .env`
# without `set -a` sets shell variables but does NOT export them, so
# mcp_apisix.py silently starts with no APISIX_VIEWER_KEY.
if [ -f .env ]; then
  set -a
  source .env
  set +a
else
  echo "warning: .env not found (cp .env.example .env first) — APISIX_VIEWER_KEY will be unset" >&2
fi

# 1. Start the MCP server in the background on 8002.
python3 mcp_apisix.py &
MCP_PID=$!
echo "mcp_apisix.py running (pid $MCP_PID) on :8002"

# Stop the MCP server when mcpo exits / this script is interrupted.
trap 'kill $MCP_PID 2>/dev/null' EXIT

# Give the MCP server a moment to bind its port.
sleep 2

# 2. Start mcpo, proxying the streamable-http MCP server to OpenAPI.
"$MCPO" --port 8000 --server-type streamable-http -- http://localhost:8002/mcp
