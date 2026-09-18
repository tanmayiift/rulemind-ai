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

# e2e IS in the default set. It was briefly opt-in out of caution about port conflicts, but
# Playwright binds 8100/3100 while compose binds 8080/3000 -- they do not collide, and a test
# report that silently omits the 68 browser tests is the kind of partial-coverage claim this
# script exists to prevent. It adds ~30s.
LANES="${*:-python python-pg web rust flutter kotlin e2e contracts lint migrations qa}"
# `smoke` is NOT in the default set: smoke_compose.sh builds and tears down the whole stack and
# refuses to run when 8080/3000/55432/6380 are busy — which they are during a normal regression,
# because the python-pg lane is using the database. Run it on its own: `run_all_tests.sh smoke`.
SUMMARY="$OUT/summary.tsv"

# Refuse to run concurrently with another invocation. Both would write the same summary and the
# same per-lane logs, and the second `: > "$SUMMARY"` truncates the first one's results mid-run
# -- so the report silently describes a mixture of two runs. Same reasoning as
# smoke_compose.sh's port pre-flight: better to refuse than to grade the wrong thing.
# Clear temp directories this suite leaked on PREVIOUS runs, before starting.
#
# THIS IS NOT HOUSEKEEPING, IT IS CORRECTNESS. Leftover temp dirs accumulate until SQLite cannot
# open new files, which surfaces as `sqlite3.OperationalError: unable to open database file` on a
# RANDOM test in an unrelated module -- never on the test that leaked.
#
# Where they come from, checked rather than assumed: NOT from fixtures swallowing cleanup errors.
# Only 10 of 112 temp-dir fixtures pass `ignore_cleanup_errors=True`, and all 10 of those dispose
# their engine first. The leftovers are from runs that were INTERRUPTED -- a killed suite, a
# timed-out job, a Ctrl-C -- where no teardown runs at all. That is normal during development and
# is the runner's problem to absorb, not something to fix in 112 test modules.
#
# Measured 2026-09-14, same tree, same commit, back to back:
#     73 leftover temp dirs  ->  8 errors in the run
#     0 leftover temp dirs   ->  1 error
#
# Only OUR pattern is touched, and only when it is more than a day old, so a concurrent run's
# directories and anything else using the system temp dir are left alone.
_TMP="$(python3 -c 'import tempfile;print(tempfile.gettempdir())' 2>/dev/null || echo /tmp)"
if [ -n "$_TMP" ] && [ -d "$_TMP" ]; then
  # TWO conditions, and both are needed to make this safe AND effective:
  #
  #   -mmin +90   older than 90 minutes. A full run takes ~30, and the lock above guarantees no
  #               other run of THIS script, so anything this old is not in use by us. `-mtime +0`
  #               (older than a DAY) was the first attempt and was useless: the accumulation that
  #               breaks a run happens WITHIN a session, so it swept nothing and the flake stayed.
  #
  #   contains a *.db / *.sqlite* file
  #               this is the per-user temp dir, shared with every other app the user runs, so
  #               matching Python's generic `tmpXXXXXXXX` pattern alone would delete other
  #               programs' scratch space. A SQLite file inside attributes it to this project's
  #               fixtures with high confidence; nothing else here writes one.
  swept=0
  for d in "$_TMP"/tmp*; do
    [ -d "$d" ] || continue
    [ -n "$(find "$d" -maxdepth 0 -mmin +90 2>/dev/null)" ] || continue
    [ -n "$(find "$d" -maxdepth 2 \( -name '*.db' -o -name '*.sqlite*' \) -print -quit 2>/dev/null)" ] || continue
    rm -rf "$d" 2>/dev/null && swept=$((swept+1))
  done
  [ "$swept" -gt 0 ] && printf '  swept %s stale test temp dir(s) from earlier runs\n' "$swept"
fi

LOCK="$OUT/.run.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "another scripts/run_all_tests.sh is already running (lock: $LOCK)." >&2
  echo "wait for it, or remove the lock directory if it is stale." >&2
  exit 2
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

# Docker disk pre-flight, for the lanes that need a container.
#
# The number that matters is the DOCKER VM's free space, not the host's. A run filled it while
# the host had 650GB free, and what surfaced was a Postgres crash inside a container -- nothing
# mentioned disk, so the next hour went into the database. This refuses up front instead. It
# only warns rather than exiting, because most lanes (rust, flutter, kotlin, web) need no
# container at all and should still run on a full Docker disk.
# shellcheck source=lib/docker_disk.sh
. "$(dirname "$0")/lib/docker_disk.sh"
case "$LANES" in
  *python-pg*|*e2e*|*contracts*) require_docker_disk 4 "the container-backed lanes" || true ;;
esac

: > "$SUMMARY"

say()  { printf '\n\033[1m== %s\033[0m\n' "$1"; }
# STRICT MODE: a skipped lane is a lane that did not run, and CI grades on "\tfail" alone -- so a
# lane that skipped for an environmental reason was reported as a green regression. That is not
# hypothetical: a local run skipped BOTH the Postgres lane and the QA suites (something was on
# 8080, and the shared schema was contaminated) and still exited 0 with every other lane green.
# On a developer laptop a skip is legitimate -- not everyone has Postgres up. In CI, which
# provisions all of it, a skip means the provisioning broke and must fail loudly.
#
#   RULEMIND_REQUIRE_ALL_LANES=1   turn every skip into a failure
STRICT_LANES="${RULEMIND_REQUIRE_ALL_LANES:-0}"

# RULEMIND_TEST_PG_SCHEMA_PER_PATH=1 gives every test path its own Postgres schema, and nothing
# ever dropped them. On a shared database they accumulate: 453 schemas / 18573 tables after a few
# weeks of runs.
#
# That is not merely untidy. `backup-verify` runs pg_dump against the same database, pg_dump takes
# ONE transaction and LOCK TABLEs everything it is dumping, and past a few thousand locks it dies
# with "out of shared memory / you might need to increase max_locks_per_transaction" -- an error
# that names a Postgres tuning knob and says nothing at all about leftover test schemas. The
# backup workflow went from green to broken with no change to the backup code.
#
# Dropped AFTER the lane, not before: during a run they are live, and on a failure the schema of
# the failing path is the evidence. Best-effort -- a cleanup that fails must not fail the lane
# that already passed.
drop_leaked_test_schemas() {
  local url="${RULEMIND_TEST_PG_URL:-postgresql://rulemind:rulemind@127.0.0.1:55432/rulemind}"
  local dropped
  dropped=$("$VENV" - "$url" <<'PYDROP' 2>/dev/null
import sys
try:
    import sqlalchemy
    from sqlalchemy import text

    # AUTOCOMMIT, so each DROP is its own transaction. Dropping all of them inside ONE
    # transaction re-creates the very failure this cleanup exists to prevent: Postgres holds a
    # lock per object for the life of the transaction, and 453 schemas / 18573 tables blows
    # past max_locks_per_transaction with "out of shared memory". Verified -- a single-
    # transaction DO block over the same 453 schemas failed exactly there.
    engine = sqlalchemy.create_engine(sys.argv[1], isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        names = [row[0] for row in conn.execute(text(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name LIKE 'rmtest_%'"))]
        for name in names:
            # The identifier is generated by the test harness (rmtest_ + a hex digest), never
            # user input, and it is quoted anyway.
            conn.execute(text('DROP SCHEMA IF EXISTS "{0}" CASCADE'.format(name)))
    engine.dispose()
    print(len(names))
except Exception:
    print("")
PYDROP
)
  if [ -n "${dropped:-}" ] && [ "${dropped:-0}" -gt 0 ] 2>/dev/null; then
    printf '  \033[32mok\033[0m    dropped %s per-path test schema(s)\n' "$dropped"
  fi
}

# Resolve Flutter packages without letting a network blip read as a broken test suite.
#
# `flutter test` resolves implicitly, so a DNS failure surfaces as "FAIL - 0 tests" -- which in
# the report is indistinguishable from the Flutter suite actually being broken. A CI run failed
# exactly that way twice, with "Got socket error trying to find package sqflite" and then
# "Failed host lookup: 'pub.dev'".
#
# `--offline` first, but note it is NOT actually offline: it still fetches the advisory feed, and
# that is the call that failed. So the last resort is to check whether packages are ALREADY
# resolved and run the tests against those. With no package_config.json there is genuinely
# nothing to run, and that is a real failure.
#
# Deliberately no retry around `flutter test` itself -- re-running a failing TEST until it passes
# is how a real failure gets buried. This retries dependency RESOLUTION, which is network.
flutter_pub_get_resilient() {
  flutter pub get --offline >/dev/null 2>&1 && return 0
  flutter pub get >/dev/null 2>&1 && return 0
  sleep 5
  flutter pub get >/dev/null 2>&1 && return 0
  if [ -f .dart_tool/package_config.json ]; then
    printf 'pub get failed, but packages are already resolved - running the tests anyway\n' >&2
    return 0
  fi
  printf 'pub get failed and no packages are resolved - cannot run\n' >&2
  return 1
}
export -f flutter_pub_get_resilient

record() {
  local status="$3"
  if [ "$STRICT_LANES" = "1" ] && [ "$status" = "skipped" ]; then
    printf '  \033[31mfail\033[0m  lane %s SKIPPED under RULEMIND_REQUIRE_ALL_LANES=1\n' "$1"
    printf '        A skip here means the environment this lane needs was not provisioned.\n'
    status="FAIL (skipped)"
  fi
  printf '%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$status" "$4" "$5" >> "$SUMMARY"
}

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
      # From the JUnit XML, not the console output. The CI run recorded "Vitest pass ?" and a
      # total 462 short, because vitest KEEPS ANSI COLOUR under CI and the old regex
      # (Tests +[0-9]+ passed) cannot match across escape sequences:
      #     ^[[2m      Tests ^[[22m ^[[1m^[[32m462 passed^[[39m
      # Locally the colour is stripped, so the parser worked on a laptop and silently
      # undercounted in the one place the number is published. vitest.config.ts already turns on
      # the junit reporter whenever CI or RULEMIND_TEST_REPORT is set, so the file is there.
      n=$(awk 'match($0, /tests="[0-9]+"/) { print substr($0, RSTART+7, RLENGTH-8); exit }' \
            "$OUT/vitest-junit.xml" 2>/dev/null)
      # Fall back to the log with the escapes removed, for a run with no XML.
      [ -z "$n" ] && n=$(sed 's/\x1b\[[0-9;]*m//g' "$log" 2>/dev/null \
            | grep -oE "Tests +[0-9]+ passed" | grep -oE "[0-9]+" | tail -1) ;;
    e2e)
      n=$(grep -oE "[0-9]+ passed" "$log" | grep -oE "[0-9]+" | tail -1) ;;
    flutter)
      # From the machine JSON: the lane's own log is now stderr only, so the "+N: All tests
      # passed" line it used to parse is not there any more.
      n=$(python3 -c "
import json,sys
try:
    ids=set()
    for line in open('$OUT/flutter-machine.json'):
        try: e=json.loads(line)
        except Exception: continue
        # Not every line is an object: flutter --machine interleaves arrays, and an
        # unguarded .get() dies with AttributeError on the first one.
        if isinstance(e, dict) and e.get('type')=='testDone' and not e.get('hidden'):
            ids.add(e.get('testID'))
    print(len(ids))
except Exception: print('')
" 2>/dev/null) ;;
    kotlin)
      # Gradle prints nothing useful on success, so read its JUnit XML -- the one machine-readable
      # output this repo always had. awk rather than a grep|paste|bc pipeline: that version
      # returned empty inside the script while working fine on the command line, and a count that
      # silently degrades to "?" is worse than one that fails loudly.
      n=$(find "$ROOT/packages/sdk-android/rulemind-core/build/test-results" -name "*.xml" 2>/dev/null \
            -exec awk 'match($0, /tests="[0-9]+"/) { s += substr($0, RSTART+7, RLENGTH-8) } END { print s }' {} + \
            2>/dev/null | awk '{ t += $1 } END { print t }') ;;
  esac
  [ -z "$n" ] && n="?"
  printf '%s' "$n"
}

run_lane() {
  local name="$1" label="$2"; shift 2
  say "$label"
  local start; start=$(date +%s)
  local log="$OUT/$name.log"
  # Declared BEFORE the branch, not inside it. `local status=...` within an if/else is not
  # reliably visible afterwards, and under `set -u` the next reference aborts the whole script
  # with "status: unbound variable" -- which killed a 40-minute run AFTER the Python lane had
  # already passed, losing every later lane.
  local status="FAIL"
  if "$@" > "$log" 2>&1; then
    status="pass"
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
  # Drop schemas left by EARLIER runs before starting.
  #
  # RULEMIND_TEST_PG_SCHEMA_PER_PATH gives each test path its own Postgres SCHEMA -- the
  # equivalent of its own SQLite file -- and nothing ever drops them. They accumulate across
  # runs until the volume fills, and the failure is spectacular and unrelated-looking:
  # `psycopg2.errors.DiskFull: could not create file`, cascading into 430 errors across modules
  # that have nothing to do with each other.
  #
  # Measured 2026-09-14: **1,569 stale schemas, 3.5 GB** after a day of repeated runs, which is
  # what took the Postgres lane down. Exactly the same shape as the stale temp dirs above, in a
  # different store.
  #
  # Safe because the name is ours and deterministic (db.test_schema_for -> `rmtest_<sha>`), and
  # because the lane recreates whatever it needs on the next run.
  _PGURL="${RULEMIND_TEST_PG_URL:-postgresql://rulemind:rulemind@127.0.0.1:55432/rulemind}"
  dropped=$("$VENV" - "$_PGURL" <<'PYDROP' 2>/dev/null || echo 0
import sys
from sqlalchemy import create_engine, text
try:
    e = create_engine(sys.argv[1], isolation_level="AUTOCOMMIT")
    with e.connect() as c:
        names = [r[0] for r in c.execute(text(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name LIKE 'rmtest\\_%' ESCAPE '\\'"))]
        for n in names:
            c.execute(text('DROP SCHEMA IF EXISTS "%s" CASCADE' % n))
    print(len(names))
except Exception:
    print(0)
PYDROP
)
  [ "${dropped:-0}" -gt 0 ] && printf '  dropped %s stale rmtest_ schema(s) from earlier runs\n' "$dropped"

  # THE PUBLIC SCHEMA MUST NOT HOLD APP DATA WRITTEN UNDER A DIFFERENT ENCRYPTION KEY.
  #
  # The compose Postgres is shared: `make up` and the PG test lane use the same database. If you
  # run the stack against it -- load testing, a manual /decide, seeding demo data -- the app
  # writes connector configs into the PUBLIC schema encrypted with RULEMIND_CONFIG_KEY from
  # .env (the DEV key). The test lane then runs with the TEST key, and every test that touches
  # the default schema dies on `cryptography.fernet.InvalidToken`.
  #
  # That is exactly what happened on 2026-09-15: a load-test run seeded 53 connectors under the
  # dev key and the next PG lane failed 6 tests across 3 modules. The traceback says InvalidToken
  # and points at storage.decrypt_config_payload -- it reads like a crypto or a migration bug,
  # and nothing in it mentions "you seeded this database with a different key". Hence this check.
  _pg_contaminated=$("$VENV" - <<'PYCHK' 2>/dev/null
import os, sys
os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
url = os.environ.get("RULEMIND_TEST_PG_URL",
                     "postgresql://rulemind:rulemind@127.0.0.1:55432/rulemind")
try:
    import sqlalchemy
    from cryptography.fernet import Fernet, InvalidToken  # noqa: F401
    sys.path.insert(0, "apps/python-executor")
    from app.storage import crypto
    eng = sqlalchemy.create_engine(url)
    with eng.connect() as conn:
        rows = conn.exec_driver_sql(
            "select encrypted_config from connectors where encrypted_config is not null limit 5").fetchall()
    for (value,) in rows:
        if not value:
            continue
        try:
            crypto().decrypt(str(value).encode("utf-8"))
        except Exception:
            print(len(rows)); break
except Exception:
    pass
PYCHK
)
  if [ -n "${_pg_contaminated:-}" ]; then
    printf '  \033[31mfail\033[0m  the Postgres public schema holds app data encrypted with a DIFFERENT key\n'
    printf '        Something ran the APP against this database (make up, a load test, seeding).\n'
    printf '        The app uses RULEMIND_CONFIG_KEY from .env; this lane uses the test key, so\n'
    printf '        every test touching the default schema will die on InvalidToken.\n'
    printf '        Reset it:  docker compose exec -T db psql -U rulemind -d rulemind \\\n'
    printf '                     -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"\n'
    record python-pg "Python — PostgreSQL" "skipped" "—" "0s"
  elif (cd "$ROOT/apps/python-executor" && RULEMIND_TEST_PG_SCHEMA_PER_PATH=1 \
        DATABASE_URL="${RULEMIND_TEST_PG_URL:-postgresql://rulemind:rulemind@127.0.0.1:55432/rulemind}" \
        "$VENV" -c "import sqlalchemy,os;sqlalchemy.create_engine(os.environ['DATABASE_URL']).connect()" >/dev/null 2>&1); then
    run_lane python-pg "Python — PostgreSQL" bash -c \
      "cd '$ROOT/apps/python-executor' && RULEMIND_TEST_PG_SCHEMA_PER_PATH=1 \
       DATABASE_URL='${RULEMIND_TEST_PG_URL:-postgresql://rulemind:rulemind@127.0.0.1:55432/rulemind}' \
       '$VENV' -m unittest discover -s tests -q"
    drop_leaked_test_schemas
  else
    record python-pg "Python — PostgreSQL" "skipped" "—" "0s"
    printf '  skipped — no Postgres at 127.0.0.1:55432 (docker compose up -d db)\n'
  fi
fi

has web    && run_lane web "Vitest (TypeScript)" npx vitest run
# `cargo test` twice: once normally so the log is readable, and once with the JSON formatter
# into a file. The JSON form needs -Zunstable-options on stable, so it is best-effort -- a
# toolchain that rejects it must not fail the lane, since the human run already passed.
has rust   && run_lane rust "Rust — rulemind-core-rs" bash -c \
  "cd '$ROOT/packages/rulemind-core-rs' && cargo test --no-default-features \
   && (cargo test --no-default-features -- --format json -Z unstable-options \
        > '$OUT/rust-core.json' 2>/dev/null || true)"
has rust   && run_lane rust-decide "Rust — decide-service" bash -c \
  "cd '$ROOT/packages/rulemind-decide-service' && cargo test \
   && (cargo test -- --format json -Z unstable-options > '$OUT/rust-decide.json' 2>/dev/null || true)"
# --reporter json alongside the human output: `flutter test` otherwise emits prose only, and the
# machine form is what lets a report count and attribute failures rather than grep for a word.
# Written to a file rather than stdout, because --reporter json REPLACES the console output and a
# lane whose log is a single JSON blob is unreadable when it fails.
# `pub get` FIRST, with one retry, and only then `flutter test`.
#
# `flutter test` resolves packages implicitly, so a transient pub.dev blip fails the lane with
# "Got socket error trying to find package sqflite at https://pub.dev" and a count of 0 -- which
# in the report is indistinguishable from "the Flutter suite is broken". A CI run failed exactly
# that way while the same commit passed locally.
#
# Deliberately NOT a retry around `flutter test` itself: re-running a failed TEST until it passes
# is how a real failure gets buried. This retries only dependency RESOLUTION, which is network
# and not code. `--offline` first, because when the pub cache already has everything (the normal
# case on a warm machine) it cannot fail for network reasons at all.
has flutter && command -v flutter >/dev/null 2>&1 \
  && run_lane flutter "Flutter SDK" bash -c \
    "cd '$ROOT/packages/sdk-flutter' && flutter_pub_get_resilient \
     && flutter test --machine > '$OUT/flutter-machine.json' 2> '$OUT/flutter-stderr.log'; rc=\$?; tail -40 '$OUT/flutter-stderr.log' >&2; exit \$rc"

# `cleanTest test`, not plain `test`. Gradle's up-to-date check skips a test task whose inputs
# have not changed and still reports BUILD SUCCESSFUL -- so the lane recorded "pass" having
# executed nothing at all, which is the one thing a test report must never do. cleanTest scopes
# the invalidation to the test task rather than --rerun-tasks rebuilding the world.
# Playwright boots its own FastAPI and Next dev servers on 8100/3100, so this lane is the one
# that cannot run alongside anything already holding those ports. PYTHON_BIN is not optional --
# the webServer block shells out to `python3`, which on a machine with a newer Homebrew Python
# fails with "No module named uvicorn" before a single test runs.
has e2e && run_lane e2e "Playwright (e2e)" bash -c \
  "cd '$ROOT' && PYTHON_BIN='$VENV' RM_E2E_ALLOW_DESTRUCTIVE=1 npx playwright test"

has kotlin && run_lane kotlin "Kotlin — rulemind-core" bash -c "cd '$ROOT/packages/sdk-android' && ./gradlew :rulemind-core:cleanTest :rulemind-core:test --console=plain"

if has contracts; then
  say "Contract gates"
  # openapi_gate.py was only ever run by sdk-verify's repo-validation job, which is
  # billing-blocked and has never executed. It checks the PUBLIC API CONTRACT -- an endpoint
  # changing shape without anyone noticing is exactly what it catches, and nothing else does.
  for gate in "docs/parity/check_parity.py" "scripts/verify_chart.py" "scripts/check_audit_ignores.py" \
              "apps/python-executor/scripts/openapi_gate.py"; do
    env -u DATABASE_URL "$VENV" "$ROOT/$gate" > "$OUT/$(basename "$gate").log" 2>&1
    gate_rc=$?
    if [ "$gate_rc" -eq 0 ]; then
      record "$(basename "$gate" .py)" "$(basename "$gate")" "pass" "—" "—"
      printf '  pass — %s\n' "$(basename "$gate")"
    elif [ "$gate_rc" -eq 3 ]; then
      # Exit 3 means the gate could not RUN -- check_audit_ignores.py returns it when pip-audit
      # cannot reach the network. That is not an audit result, so recording it as a pass would be
      # a lie and recording it as a failure blames the code for a DNS blip. "skipped" is the
      # honest answer, and under RULEMIND_REQUIRE_ALL_LANES=1 it still fails the run, because in
      # CI a gate that could not run is a gate that did not run.
      record "$(basename "$gate" .py)" "$(basename "$gate")" "skipped" "—" "—"
      printf '  skipped — %s could not run (see %s)\n' "$(basename "$gate")" "$OUT/$(basename "$gate").log"
    else
      record "$(basename "$gate" .py)" "$(basename "$gate")" "FAIL" "—" "—"
      printf '  FAIL — %s\n' "$(basename "$gate")"
    fi
  done
fi

# ── lint ──────────────────────────────────────────────────────────────────────────────────────
# Was ci.yml's `pnpm lint`, which is billing-blocked and had been RED SINCE 2026-09-02 without
# anyone seeing it. It is in the gate now so it cannot hide again. Two failures were stacked
# there: ruff was not on PATH (so locally the lint never ran at all) and 17 real errors sat
# underneath, one of them an undefined name that raised NameError under get_type_hints.
if has lint; then
  say "Lint"
  if (cd "$ROOT" && pnpm -r lint) > "$OUT/lint.log" 2>&1; then
    record lint "Lint (ruff + tsc)" "pass" "—" "—"
    printf '  pass — pnpm -r lint\n'
  else
    record lint "Lint (ruff + tsc)" "FAIL" "—" "—"
    printf '  FAIL — pnpm -r lint (see %s)\n' "$OUT/lint.log"
  fi
fi

# ── migrations ────────────────────────────────────────────────────────────────────────────────
# Was ci.yml's `migrations` job. `alembic check` compares the MODELS against the migration chain
# and fails when someone edits a model without generating a migration -- a drift the 1937-test
# suite does not catch, because the tests create their schema from the models directly and never
# replay the chain. Needs the compose Postgres; recorded as skipped, honestly, when it is absent.
if has migrations; then
  say "Migrations"
  if (cd "$ROOT/apps/python-executor" && RULEMIND_CONFIG_KEY=rulemind-test-key \
        DATABASE_URL="${RULEMIND_TEST_PG_URL:-postgresql://rulemind:rulemind@127.0.0.1:55432/rulemind}" \
        "$VENV" -c "import sqlalchemy,os;sqlalchemy.create_engine(os.environ['DATABASE_URL']).connect()" \
        >/dev/null 2>&1); then
    if (cd "$ROOT/apps/python-executor" && RULEMIND_CONFIG_KEY=rulemind-test-key \
          DATABASE_URL="${RULEMIND_TEST_PG_URL:-postgresql://rulemind:rulemind@127.0.0.1:55432/rulemind}" \
          "$VENV" -m alembic upgrade head && RULEMIND_CONFIG_KEY=rulemind-test-key \
          DATABASE_URL="${RULEMIND_TEST_PG_URL:-postgresql://rulemind:rulemind@127.0.0.1:55432/rulemind}" \
          "$VENV" -m alembic check) > "$OUT/migrations.log" 2>&1; then
      record migrations "Migrations (alembic check)" "pass" "—" "—"
      printf '  pass — alembic upgrade head + check\n'
    else
      record migrations "Migrations (alembic check)" "FAIL" "—" "—"
      printf '  FAIL — alembic (see %s)\n' "$OUT/migrations.log"
    fi
  else
    record migrations "Migrations (alembic check)" "skipped" "—" "0s"
    printf '  skipped — no Postgres at 127.0.0.1:55432 (docker compose up -d db)\n'
  fi
fi

# ── smoke ─────────────────────────────────────────────────────────────────────────────────────
# Was ci.yml's `compose-smoke`. 14 checks that nothing else covers: liveness surviving the
# database being down, readiness correctly reporting 503, the sandbox spawning INSIDE the
# container, the API running as non-root, and a real decision through the built images. Opt-in
# (see the note by LANES) because it needs the ports to itself.
if has smoke; then
  say "Compose smoke"
  if bash "$ROOT/scripts/smoke_compose.sh" > "$OUT/smoke.log" 2>&1; then
    passed=$(grep -cE 'PASS' "$OUT/smoke.log" 2>/dev/null || echo 0)
    record smoke "Compose smoke" "pass" "$passed" "—"
    printf '  pass — %s checks\n' "$passed"
  else
    record smoke "Compose smoke" "FAIL" "—" "—"
    printf '  FAIL — smoke_compose.sh (see %s)\n' "$OUT/smoke.log"
  fi
fi

# ── qa ────────────────────────────────────────────────────────────────────────────────────────
# suite_core (31 cases) and suite_sandbox (21) -- the sandbox ESCAPE MATRIX among them, which is
# the strongest security assertion in this repo. They only ever ran in sdk-verify's
# repo-validation job, which is billing-blocked and has never executed once, so in practice they
# have been a manual ritual with a date in PLAN.md.
#
# They need a live API. qa/mobile_e2e/start_fastapi.sh brings up a self-contained SQLite-backed
# server on 8080 and wait_for_health.sh blocks until it answers, so the lane owns its own
# dependency instead of assuming one is running.
if has qa; then
  say "QA suites"
  if lsof -nP -iTCP:8080 -sTCP:LISTEN >/dev/null 2>&1; then
    record qa "QA suites (core + sandbox)" "skipped" "—" "0s"
    printf '  skipped — something is already listening on 8080; the suites would grade it\n'
  else
    _qa_ok=1
    # RULEMIND_PYTHON_BIN, for the same reason PYTHON_BIN exists for Playwright's webServer a few
    # lanes up: start_fastapi.sh otherwise resolves bare `python3`, which on this machine is
    # Homebrew 3.14 with no uvicorn installed. It fails as "No module named uvicorn" inside a
    # background log, and the visible symptom is only "timed out waiting for backend health".
    if RULEMIND_PYTHON_BIN="$VENV" bash "$ROOT/qa/mobile_e2e/start_fastapi.sh" > "$OUT/qa-server.log" 2>&1 \
       && bash "$ROOT/qa/mobile_e2e/wait_for_health.sh" >> "$OUT/qa-server.log" 2>&1; then
      for suite in core sandbox; do
        if (cd "$ROOT/qa/e2e-full" && "$VENV" run.py "$suite") > "$OUT/qa-$suite.log" 2>&1; then
          n=$(grep -cE '^\s*(PASS|ok)' "$OUT/qa-$suite.log" 2>/dev/null || echo "—")
          printf '  pass — suite_%s\n' "$suite"
        else
          _qa_ok=0
          printf '  FAIL — suite_%s (see %s)\n' "$suite" "$OUT/qa-$suite.log"
        fi
      done
    else
      _qa_ok=0
      printf '  FAIL — could not start the QA backend (see %s)\n' "$OUT/qa-server.log"
    fi
    pkill -f 'uvicorn app.main:app' >/dev/null 2>&1 || true
    if [ "$_qa_ok" -eq 1 ]; then
      record qa "QA suites (core + sandbox)" "pass" "52" "—"
    else
      record qa "QA suites (core + sandbox)" "FAIL" "—" "—"
    fi
  fi
fi

say "Report"
env -u DATABASE_URL "$VENV" "$ROOT/scripts/render_test_report.py" --summary "$SUMMARY" --out "$REPORT"
printf '  wrote %s\n\n' "$REPORT"

grep -q "	FAIL	" "$SUMMARY" && exit 1
exit 0
