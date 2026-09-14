#!/usr/bin/env bash
# Run every test lane, collect machine-readable output, and generate TEST_RESULTS.md.
#
# WHY THIS EXISTS. GitHub Actions has been refusing to run for this repo's entire recent history
# -- every run fails in 3-12 seconds with ZERO steps executed, which is an exhausted quota, not a
# test failure. So the only signal is local, and "it passed on my machine" is not a signal anyone
# else can check. This script makes a local run publishable: one command, every lane, a dated
# report with real numbers that gets committed.
#
# It is also what the reporting workflow runs, so the numbers on GitHub and the numbers in the
# committed file come from the same code path rather than from two definitions of "the tests".
#
#   scripts/run_all_tests.sh              # everything
#   scripts/run_all_tests.sh python web   # just those lanes
#
# TWO ENVIRONMENT TRAPS ARE ENCODED BELOW, because both cost hours and neither error names its
# real cause:
#   1. `python3` on a machine with a newer Homebrew Python breaks the PyO3 build AND Playwright's
#      webServer ("No module named uvicorn"). The venv interpreter is used explicitly.
#   2. `cargo test` in rulemind-core-rs MUST pass --no-default-features: the default `python`
#      feature pulls pyo3 `extension-module`, which deliberately does not link libpython, so a
#      plain `cargo test` fails at LINK time with a missing _Py_TrueStruct.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

ROOT="$(pwd)"
VENV="$ROOT/.venv-rulemind/bin/python"
OUT="$ROOT/test-results"
REPORT="$ROOT/TEST_RESULTS.md"
mkdir -p "$OUT"

export RULEMIND_TEST_REPORT=1
export RULEMIND_CONFIG_KEY="${RULEMIND_CONFIG_KEY:-rulemind-test-key}"
export MODEL_POOL_SIZE="${MODEL_POOL_SIZE:-1}"
export SANDBOX_POOL_SIZE="${SANDBOX_POOL_SIZE:-2}"

LANES="${*:-python python-pg web rust flutter kotlin contracts}"
SUMMARY="$OUT/summary.tsv"

# Refuse to run concurrently with another invocation. Both would write the same summary and the
# same per-lane logs, and the second `: > "$SUMMARY"` truncates the first one's results mid-run
# -- so the report silently describes a mixture of two runs. Same reasoning as
# smoke_compose.sh's port pre-flight: better to refuse than to grade the wrong thing.
LOCK="$OUT/.run.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "another scripts/run_all_tests.sh is already running (lock: $LOCK)." >&2
  echo "wait for it, or remove the lock directory if it is stale." >&2
  exit 2
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

: > "$SUMMARY"

say()  { printf '\n\033[1m== %s\033[0m\n' "$1"; }
record() { printf '%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "$5" >> "$SUMMARY"; }

count_tests() {
  local lane="$1" log="$2" n=""
  case "$lane" in
    python|python-pg)
      # unittest/xmlrunner: "Ran N tests in ..."
      n=$(grep -oE "^Ran [0-9]+ tests?" "$log" | grep -oE "[0-9]+" | tail -1) ;;
    rust|rust-decide)
      # cargo prints one line per target; SUM them, and the empty doctest target contributes 0.
      n=$(grep -oE "test result: ok\. [0-9]+ passed" "$log" | grep -oE "[0-9]+" | paste -sd+ - | bc 2>/dev/null) ;;
    web)
      n=$(grep -oE "Tests +[0-9]+ passed" "$log" | grep -oE "[0-9]+" | tail -1) ;;
    flutter)
      n=$(grep -oE "\+[0-9]+: All tests passed" "$log" | grep -oE "[0-9]+" | tail -1) ;;
    kotlin)
      # Gradle prints nothing useful on success; read its JUnit XML, which it always writes.
      n=$(find "$ROOT/packages/sdk-android/rulemind-core/build/test-results" -name "*.xml" 2>/dev/null \
            | xargs grep -ho 'tests="[0-9]*"' 2>/dev/null | grep -oE "[0-9]+" | paste -sd+ - | bc 2>/dev/null) ;;
  esac
  [ -z "$n" ] && n="?"
  printf '%s' "$n"
}

run_lane() {
  local name="$1" label="$2"; shift 2
  say "$label"
  local start; start=$(date +%s)
  local log="$OUT/$name.log"
  if "$@" > "$log" 2>&1; then
    local status="pass"
  else
    local status="FAIL"
  fi
  local dur=$(( $(date +%s) - start ))
  # Counted from the runner's own output, never hardcoded -- a typed-once number is how
  # README.md came to claim 1369 tests against an actual 1892.
  #
  # Per-runner, not a guessing cascade. The first version tried three patterns in order and took
  # `tail -1` of whatever matched, which reported Rust as 0 tests: cargo prints a final
  # "test result: ok. 0 passed" for the empty doctest target, and that was the last match.
  local count
  count="$(count_tests "$name" "$log")"
  record "$name" "$label" "$status" "${count}" "${dur}s"
  printf '  %s — %s tests, %ss\n' "$status" "$count" "$dur"
}

has() { case " $LANES " in *" $1 "*) return 0;; *) return 1;; esac; }

if has python; then
  run_lane python "Python — SQLite" bash -c \
    "cd '$ROOT/apps/python-executor' && env -u DATABASE_URL '$VENV' -m xmlrunner discover -s tests -o '$OUT/python-junit'"
fi

if has python-pg; then
  # RULEMIND_TEST_PG_SCHEMA_PER_PATH is not optional: without it 66 modules construct
  # Storage(path=...) and silently fall back to SQLite, so the "Postgres lane" re-runs SQLite.
  # The URL must be postgresql:// -- requirements pins psycopg2, and the +psycopg scheme fails
  # every module import with ModuleNotFoundError, which reads like a broken virtualenv.
  if (cd "$ROOT/apps/python-executor" && RULEMIND_TEST_PG_SCHEMA_PER_PATH=1 \
        DATABASE_URL="${RULEMIND_TEST_PG_URL:-postgresql://rulemind:rulemind@127.0.0.1:55432/rulemind}" \
        "$VENV" -c "import sqlalchemy,os;sqlalchemy.create_engine(os.environ['DATABASE_URL']).connect()" >/dev/null 2>&1); then
    run_lane python-pg "Python — PostgreSQL" bash -c \
      "cd '$ROOT/apps/python-executor' && RULEMIND_TEST_PG_SCHEMA_PER_PATH=1 \
       DATABASE_URL='${RULEMIND_TEST_PG_URL:-postgresql://rulemind:rulemind@127.0.0.1:55432/rulemind}' \
       '$VENV' -m unittest discover -s tests -q"
  else
    record python-pg "Python — PostgreSQL" "skipped" "—" "0s"
    printf '  skipped — no Postgres at 127.0.0.1:55432 (docker compose up -d db)\n'
  fi
fi

has web    && run_lane web "Vitest (TypeScript)" npx vitest run
has rust   && run_lane rust "Rust — rulemind-core-rs" bash -c "cd '$ROOT/packages/rulemind-core-rs' && cargo test --no-default-features"
has rust   && run_lane rust-decide "Rust — decide-service" bash -c "cd '$ROOT/packages/rulemind-decide-service' && cargo test"
has flutter && command -v flutter >/dev/null 2>&1 \
  && run_lane flutter "Flutter SDK" bash -c "cd '$ROOT/packages/sdk-flutter' && flutter test"
has kotlin && run_lane kotlin "Kotlin — rulemind-core" bash -c "cd '$ROOT/packages/sdk-android' && ./gradlew :rulemind-core:test --console=plain"

if has contracts; then
  say "Contract gates"
  for gate in "docs/parity/check_parity.py" "scripts/verify_chart.py" "scripts/check_audit_ignores.py"; do
    if env -u DATABASE_URL "$VENV" "$ROOT/$gate" > "$OUT/$(basename "$gate").log" 2>&1; then
      record "$(basename "$gate" .py)" "$(basename "$gate")" "pass" "—" "—"
      printf '  pass — %s\n' "$(basename "$gate")"
    else
      record "$(basename "$gate" .py)" "$(basename "$gate")" "FAIL" "—" "—"
      printf '  FAIL — %s\n' "$(basename "$gate")"
    fi
  done
fi

say "Report"
env -u DATABASE_URL "$VENV" "$ROOT/scripts/render_test_report.py" --summary "$SUMMARY" --out "$REPORT"
printf '  wrote %s\n\n' "$REPORT"

grep -q "	FAIL	" "$SUMMARY" && exit 1
exit 0
