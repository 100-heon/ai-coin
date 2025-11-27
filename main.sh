#!/usr/bin/env bash

# One-shot launcher: start paper MCP services + dashboard + agent in one terminal.
# Press Ctrl+C to stop everything.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT_DIR}"

if [ -f ".env" ]; then
  set -a
  # shellcheck disable=SC1091
  source ".env"
  set +a
fi

CONFIG_PATH="${1:-configs/default_config.json}"
PORT="${PORT:-9000}"
TEST_PORT="${TEST_PORT:-}"
DASHBOARD_RELOAD="${DASHBOARD_RELOAD:-true}"
UVICORN_WORKERS="${UVICORN_WORKERS:-2}"
UVICORN_LIMIT_CONCURRENCY="${UVICORN_LIMIT_CONCURRENCY:-200}"

LOG_DIR="${ROOT_DIR}/logs"
mkdir -p "${LOG_DIR}"

cleanup() {
  echo "🛑 Stopping services..."
  for pid in ${PIDS:-}; do
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" || true
      wait "$pid" 2>/dev/null || true
    fi
  done
}
trap cleanup EXIT

echo "🔧 Starting MCP services (paper)..."
python agent_tools/start_mcp_services_paper.py >"${LOG_DIR}/mcp-paper.log" 2>&1 &
PIDS="$!"

start_uvicorn() {
  local target_port="$1"
  local reload_flag="$2"
  local log_file="$3"
  echo "🌐 Starting dashboard (uvicorn) on :${target_port} (reload=${reload_flag})..."
  local cmd=(python -m uvicorn dashboard.app:app --host 0.0.0.0 --port "${target_port}")
  if [[ "${reload_flag,,}" == "true" ]]; then
    echo "🔁  Dashboard reload enabled for port ${target_port}"
    cmd+=(--reload --reload-dir "${ROOT_DIR}/dashboard" --reload-dir "${ROOT_DIR}/dashboard/static")
    cmd+=(--workers 1 --limit-concurrency "${UVICORN_LIMIT_CONCURRENCY}")
  else
    cmd+=(--workers "${UVICORN_WORKERS}" --limit-concurrency "${UVICORN_LIMIT_CONCURRENCY}")
  fi
  "${cmd[@]}" >"${log_file}" 2>&1 &
  PIDS+=" $!"
}

start_uvicorn "${PORT}" "${DASHBOARD_RELOAD}" "${LOG_DIR}/dashboard.log"

if [[ -n "${TEST_PORT}" ]]; then
  TEST_RELOAD="${TEST_RELOAD:-true}"
  start_uvicorn "${TEST_PORT}" "${TEST_RELOAD}" "${LOG_DIR}/dashboard-test.log"
fi

STARTUP_SLEEP_SECONDS=${STARTUP_SLEEP_SECONDS:-5}
echo "⏳ Waiting ${STARTUP_SLEEP_SECONDS}s for services to warm up..."
sleep "${STARTUP_SLEEP_SECONDS}"

echo "🤖 Running agent with ${CONFIG_PATH}..."
python main.py "${CONFIG_PATH}"

echo "✅ Agent run finished. MCP and dashboard still running (Ctrl+C to stop)."
wait
