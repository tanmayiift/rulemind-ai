#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RESULTS_DIR="${RULEMIND_E2E_RESULTS_DIR:-$ROOT_DIR/qa/results}"
HOST="${RULEMIND_E2E_HOST:-127.0.0.1}"
PORT="${RULEMIND_E2E_PORT:-8080}"
DB_PATH="${RULEMIND_E2E_DB_PATH:-$ROOT_DIR/.runtime/mobile-e2e.sqlite3}"
PID_FILE="${RULEMIND_E2E_PID_FILE:-$RESULTS_DIR/mobile-e2e-backend.pid}"
LOG_FILE="${RULEMIND_E2E_LOG_FILE:-$RESULTS_DIR/mobile-e2e-backend.log}"
PYTHON_BIN="${RULEMIND_PYTHON_BIN:-}"

if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x /tmp/rulemind-review-venv/bin/python ]]; then
    PYTHON_BIN="/tmp/rulemind-review-venv/bin/python"
  else
    PYTHON_BIN="python3"
  fi
fi

mkdir -p "$RESULTS_DIR" "$(dirname "$DB_PATH")"

export DATABASE_URL="sqlite:///$DB_PATH"
export RULEMIND_CONFIG_KEY="${RULEMIND_CONFIG_KEY:-rulemind-test-key}"
export RULEMIND_ADMIN_JWT_SECRET="${RULEMIND_ADMIN_JWT_SECRET:-rulemind-test-admin-secret}"
# The mobile E2E journeys decide against the sample lending inventory, so seed it
# (the shipped app defaults to a clean workspace).
export RULEMIND_SEED_DEMO="${RULEMIND_SEED_DEMO:-1}"

cd "$ROOT_DIR/apps/python-executor"
"$PYTHON_BIN" -m uvicorn app.main:app --host "$HOST" --port "$PORT" >"$LOG_FILE" 2>&1 &
echo $! >"$PID_FILE"

echo "Started FastAPI backend on ${HOST}:${PORT}"
echo "PID file: $PID_FILE"
echo "Log file: $LOG_FILE"
