#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RESULTS_DIR="${RULEMIND_E2E_RESULTS_DIR:-$ROOT_DIR/qa/results}"
mkdir -p "$RESULTS_DIR"
PYTHON_BIN="${RULEMIND_PYTHON_BIN:-}"

if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x /tmp/rulemind-review-venv/bin/python ]]; then
    PYTHON_BIN="/tmp/rulemind-review-venv/bin/python"
  else
    PYTHON_BIN="python3"
  fi
fi

if [[ -z "${JAVA_HOME:-}" && -d "/Applications/Android Studio.app/Contents/jbr/Contents/Home" ]]; then
  export JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home"
fi

cd "$ROOT_DIR"

"$PYTHON_BIN" -m unittest discover -s apps/python-executor/tests -p 'test_*.py'
pnpm typecheck
pnpm test

(
  cd packages/sdk-android
  ./gradlew --no-daemon --stacktrace :rulemind-core:test :sample-app:test :sample-app:assembleDebug :sample-app:assembleAndroidTest
)

if command -v flutter >/dev/null 2>&1; then
  (
    cd packages/sdk-flutter
    flutter pub get
    flutter test
  )
  (
    cd packages/sdk-flutter/example
    flutter pub get
  )
else
  echo "Flutter SDK not found; skipping local Flutter checks." | tee "$RESULTS_DIR/flutter-local-skip.log"
fi

"$PYTHON_BIN" qa/mobile_e2e/generate_closeout_report.py \
  --output-md "$RESULTS_DIR/mobile-e2e-closeout.md" \
  --output-json "$RESULTS_DIR/mobile-e2e-closeout.json"
