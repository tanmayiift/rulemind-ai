#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RESULTS_DIR="${RULEMIND_E2E_RESULTS_DIR:-$ROOT_DIR/qa/results}"
PID_FILE="${RULEMIND_E2E_PID_FILE:-$RESULTS_DIR/mobile-e2e-backend.pid}"

if [[ -f "$PID_FILE" ]]; then
  PID="$(cat "$PID_FILE")"
  if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    echo "Stopped FastAPI backend PID $PID"
  fi
  rm -f "$PID_FILE"
fi
