#!/usr/bin/env bash
# Run the live AI provider suite (#123).
#
# The live half of tests/test_ai_live.py is skipped unless a real provider key is supplied,
# because it makes BILLED calls. This script exists so the invocation is recorded once and
# does not have to be retyped -- the repo path contains a space, the venv is not on PATH, and
# unittest needs apps/python-executor as its working directory, which together make the
# hand-typed command easy to get wrong.
#
#   ./run_ai_live.sh sk-ant-...
#   RULEMIND_AI_LIVE_KEY=sk-ant-... ./run_ai_live.sh
#   RULEMIND_AI_LIVE_PROVIDER=openai ./run_ai_live.sh sk-...
set -euo pipefail

# Resolve paths from this script's own location, so it runs from any directory.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="$(cd "$HERE/.." && pwd)"
REPO_ROOT="$(cd "$APP_ROOT/../.." && pwd)"
PY="$REPO_ROOT/.venv-rulemind/bin/python"

KEY="${1:-${RULEMIND_AI_LIVE_KEY:-}}"
if [ -z "$KEY" ]; then
  echo "usage: $0 <provider-api-key>      (or set RULEMIND_AI_LIVE_KEY)" >&2
  echo "the key is never written to disk or logged." >&2
  exit 2
fi
if [ ! -x "$PY" ]; then
  echo "venv python not found at $PY" >&2
  exit 1
fi

cd "$APP_ROOT"
RULEMIND_AI_LIVE=1 \
RULEMIND_AI_LIVE_PROVIDER="${RULEMIND_AI_LIVE_PROVIDER:-anthropic}" \
RULEMIND_AI_LIVE_KEY="$KEY" \
  "$PY" -m unittest tests.test_ai_live -v
