#!/bin/bash

# AI-Trader Upbit launch script
# Starts MCP services (Upbit toolchain) and runs the trading agent once.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT_DIR}"

# Load environment variables so subprocesses inherit credentials/config.
if [ -f ".env" ]; then
    set -a
    # shellcheck disable=SC1091
    source ".env"
    set +a
fi

cleanup() {
    if [ -n "${MCP_PID:-}" ] && kill -0 "${MCP_PID}" 2>/dev/null; then
        echo "🛑 Stopping MCP services (pid ${MCP_PID})..."
        kill "${MCP_PID}" || true
        wait "${MCP_PID}" 2>/dev/null || true
    fi
}
trap cleanup EXIT

LOG_DIR="${ROOT_DIR}/logs"
mkdir -p "${LOG_DIR}"

echo "🔧 Starting MCP services (Upbit)..."
cd agent_tools
python start_mcp_services_upbit.py 2>&1 & 
MCP_PID=$!
cd ..

# Give services a moment to become reachable (configurable)
STARTUP_SLEEP_SECONDS=${STARTUP_SLEEP_SECONDS:-10}
echo "⏳ Startup sleep: waiting ${STARTUP_SLEEP_SECONDS}s for services..."
sleep "${STARTUP_SLEEP_SECONDS}"

CONFIG_PATH="${1:-configs/default_config.json}"
echo "🤖 Running AI-Trader with config: ${CONFIG_PATH}"
python main.py "${CONFIG_PATH}"

echo "✅ Trading run completed"
