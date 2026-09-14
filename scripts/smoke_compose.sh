#!/usr/bin/env bash
# Full-stack smoke test against docker compose — the "it actually runs" half of the
# deployability gate.
#
# scripts/verify_chart.py asserts the SHAPE of the rendered manifests. It cannot tell you that
# the image boots, that the migration chain applies, or that the browser bundle can reach the
# API. Those need something running, and compose is the only working deploy path in this repo
# (there is no cluster, no registry, and no deploy automation).
#
# Each check below exists because the corresponding thing was actually broken:
#
#   * the API entrypoint swallowed a failed `alembic upgrade` and booted against a stale schema,
#   * liveness pointed at an endpoint that 503s on a DB blip,
#   * NEXT_PUBLIC_API_BASE_URL is inlined at BUILD time, so the runtime env: block was a no-op
#     and the shipped bundle pointed at http://localhost:8080,
#   * `.dockerignore` excluded `.venv` but the real directory is `.venv-rulemind` (482MB), and
#     did not exclude `.env` — which exists with real secrets and was baked into the web image
#     by `COPY . .`,
#   * RULEMIND_ENV was set in no artifact, so the fail-closed secret gate always skipped.
#
# Usage:  scripts/smoke_compose.sh [--keep]
#         --keep leaves the stack up for manual poking.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 2

KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1

PASS=0
FAIL=0
API="http://localhost:8080"
WEB="http://localhost:3000"

ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL+1)); }
note() { printf '        %s\n' "$1"; }

cleanup() {
  if [ "$KEEP" -eq 1 ]; then
    echo; echo "  stack left running (--keep). Tear down with: docker compose down -v"
  else
    echo; echo "  tearing down…"
    docker compose down -v >/dev/null 2>&1
  fi
}
trap cleanup EXIT

echo
echo "  docker compose smoke test"
echo "  ========================="
echo

# ---------------------------------------------------------------- build + boot
# ---------------------------------------------------------------- pre-flight
# Anything already bound to a port this stack publishes will answer the checks below INSTEAD of
# the container, and every one of them would grade that other server. A stale uvicorn left on
# 127.0.0.1:8080 by the QA harness did exactly that here: two checks failed against a process
# that had never seen the image, and the two that "passed" against it were just as meaningless.
# A deployability gate that can silently test something other than the deployment is worse than
# no gate, so this refuses to start rather than reporting on the wrong server.
PORTS_IN_USE=""
for port in 8080 3000 55432 6380; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    PORTS_IN_USE="$PORTS_IN_USE $port"
  fi
done
if [ -n "$PORTS_IN_USE" ]; then
  bad "ports already in use:$PORTS_IN_USE — the checks would grade whatever is listening there"
  for port in $PORTS_IN_USE; do
    lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | tail -n +2 | sed 's/^/        /'
  done
  note "stop those first (docker compose down, or kill the stray process)."
  exit 2
fi
ok "no conflicting listener on 8080/3000/55432/6380"

echo "  building (this is also the .dockerignore check — watch the context size)…"
BUILD_LOG=$(mktemp)
if ! docker compose build >"$BUILD_LOG" 2>&1; then
  bad "compose build failed"
  tail -30 "$BUILD_LOG" | sed 's/^/        /'
  exit 1
fi
CONTEXT=$(grep -oE 'transferring context: [0-9.]+[kMG]?B' "$BUILD_LOG" | tail -1 || true)
[ -n "$CONTEXT" ] && note "build ${CONTEXT}"
ok "images build"

# The end-to-end decision check below needs a policy, and compose seeds nothing by default --
# correctly, since a compose stack may be someone's real environment. Opt in for this run only.
# This check used to "pass" against a stale uvicorn on the host that happened to have seeded
# data, so the decision path through the actual compose stack had never been exercised.
export RULEMIND_SEED_DEMO=1

echo "  starting stack…"
UP_LOG=$(mktemp)
if ! docker compose up -d >"$UP_LOG" 2>&1; then
  bad "compose up failed"
  # `up`'s OWN output, not container logs. This discarded stderr and printed
  # `docker compose logs` instead, which on a port conflict shows forty lines of stale Redis
  # background-save chatter from a previous run and never the words "port is already allocated".
  tail -20 "$UP_LOG" | sed 's/^/        /'
  if grep -q "already allocated\|address already in use" "$UP_LOG"; then
    note "a port this stack publishes is taken. Running containers:"
    docker ps --format '        {{.Names}}  {{.Ports}}' | sed 's/^/  /'
  fi
  exit 1
fi

# ---------------------------------------------------------------- readiness
echo "  waiting for the API…"
UP=0
for _ in $(seq 1 90); do
  if curl -fsS "$API/health" >/dev/null 2>&1; then UP=1; break; fi
  sleep 2
done
if [ "$UP" -eq 1 ]; then ok "API answers /health"; else
  bad "API never became healthy"
  docker compose logs api --tail 60 | sed 's/^/        /'
fi

# ---------------------------------------------------------------- the checks

# Liveness must not depend on the database. Stopping Postgres and re-probing is the only honest
# way to assert it: a liveness probe that 503s on a DB blip restarts the whole fleet at once.
if [ "$UP" -eq 1 ]; then
  docker compose stop db >/dev/null 2>&1
  sleep 3
  LIVE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$API/health" 2>/dev/null || echo "000")
  READY=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$API/ready" 2>/dev/null || echo "000")
  docker compose start db >/dev/null 2>&1
  if [ "$LIVE" = "200" ]; then
    ok "liveness (/health) survives the database being down — got $LIVE"
  else
    bad "liveness (/health) returned $LIVE with the DB down; a failover would restart every pod"
  fi
  if [ "$READY" != "200" ]; then
    ok "readiness (/ready) correctly reports the dependency — got $READY"
  else
    bad "readiness (/ready) returned 200 with the DB down; it is not checking dependencies"
  fi
  # Give it a moment to reconnect before the remaining checks.
  for _ in $(seq 1 30); do curl -fsS "$API/ready" >/dev/null 2>&1 && break; sleep 2; done
fi

# The sandbox must actually spawn INSIDE the container. This is the one platform assumption a
# new host has to be tested for: if the runtime forbids `spawn`, every user-authored Python
# variable fails and the product does not work -- while /ready used to answer 200 because it
# only looked at the database and Redis. Rehearsing it here means the same curl answers the
# question on any new host.
if [ "$UP" -eq 1 ]; then
  READY_BODY=$(curl -fsS "$API/ready" 2>/dev/null || echo '{}')
  SANDBOX=$(echo "$READY_BODY" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("sandbox","missing"))' 2>/dev/null || echo "missing")
  MODE=$(echo "$READY_BODY" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("sandbox_mode","?"))' 2>/dev/null || echo "?")
  if [ "$SANDBOX" = "ok" ]; then
    ok "the sandbox spawns inside the container (mode=$MODE)"
  elif [ "$SANDBOX" = "missing" ]; then
    bad "/ready does not report the sandbox at all"
  else
    bad "sandbox is '$SANDBOX' (mode=$MODE) — every Python variable will fail closed"
    echo "$READY_BODY" | head -c 300 | sed 's/^/        /'
  fi
fi

# The migration chain must have applied — a stale schema used to boot silently.
if docker compose exec -T api python3 -m alembic -c alembic.ini current 2>/dev/null | grep -q "(head)"; then
  ok "alembic reports the schema at head"
else
  bad "alembic is not at head — the entrypoint may still be swallowing a failed migration"
  docker compose exec -T api python3 -m alembic -c alembic.ini current 2>&1 | tail -5 | sed 's/^/        /'
fi

# RULEMIND_ENV drives the fail-closed secret gate and the Secure cookie flag.
ENVVAL=$(docker compose exec -T api sh -c 'echo "$RULEMIND_ENV"' 2>/dev/null | tr -d '\r\n')
if [ -n "$ENVVAL" ]; then
  ok "RULEMIND_ENV is set in the API container ($ENVVAL)"
else
  bad "RULEMIND_ENV is unset — the fail-closed secret gate silently skips"
fi

# The web image must not contain the repo's .env. It exists locally with real secrets, and
# `COPY . .` baked it in.
WEB_SVC_IMAGE=$(docker compose images web --format json 2>/dev/null | head -1)
if docker compose exec -T web sh -c 'test -f /app/.env' >/dev/null 2>&1; then
  bad "the web image contains /app/.env — real secrets are baked into the image"
else
  ok "no .env inside the web image"
fi

# Containers must not run as root; this service executes untrusted customer Python.
API_UID=$(docker compose exec -T api id -u 2>/dev/null | tr -d '\r\n')
if [ "$API_UID" = "0" ]; then
  bad "the API container runs as root (uid 0)"
elif [ -n "$API_UID" ]; then
  ok "API container runs as non-root (uid $API_UID)"
fi

# The browser bundle must point at a URL a browser can actually resolve — not a cluster-internal
# service name, and not a stale build-time literal.
for _ in $(seq 1 45); do curl -fsS "$WEB" >/dev/null 2>&1 && break; sleep 2; done
if curl -fsS "$WEB" >/dev/null 2>&1; then
  ok "web app serves"
  BUNDLE=$(curl -fsS "$WEB" 2>/dev/null || true)
  if echo "$BUNDLE" | grep -q "rulemind-api:8080"; then
    bad "the served page references cluster-internal DNS (rulemind-api:8080) a browser cannot resolve"
  else
    ok "no cluster-internal API host in the served page"
  fi
else
  bad "web app did not serve"
fi

# End to end: a decision through the real stack, not TestClient.
KEY=$(docker compose exec -T api sh -c 'echo "$RULEMIND_DEV_API_KEY"' 2>/dev/null | tr -d '\r\n')
KEY=${KEY:-rm_live_devlocaltenantkey000000000000}
POLICY=$(curl -fsS "$API/api/v1/policies" -H "x-api-key: $KEY" 2>/dev/null \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print((([p for p in d if p.get("status")=="prod"] or d)[0])["id"])' 2>/dev/null || true)
if [ -n "$POLICY" ]; then
  DEC=$(curl -fsS -X POST "$API/api/v1/decide" -H "x-api-key: $KEY" -H 'content-type: application/json' \
    -d "{\"policyId\":\"$POLICY\",\"payload\":{\"age\":34}}" 2>/dev/null || true)
  if echo "$DEC" | grep -q '"outcome"'; then
    ok "a decision runs end to end through the compose stack"
    if echo "$DEC" | grep -q '"variable_errors"'; then
      ok "/decide exposes variable_errors (T1.2 contract)"
    else
      bad "/decide response has no variable_errors — a caller cannot tell a real null from a failure"
    fi
  else
    bad "decide failed: $(echo "$DEC" | head -c 200)"
  fi
else
  bad "could not list policies to decide against"
fi

echo
echo "  ---------------------------------------"
printf '  %d passed, %d failed\n' "$PASS" "$FAIL"
echo
[ "$FAIL" -eq 0 ] || exit 1
