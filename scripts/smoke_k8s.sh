#!/usr/bin/env bash
# Smoke test the LOCAL Kubernetes deployment — the half `smoke_compose.sh` cannot cover.
#
# smoke_compose.sh proves the images work under Docker Compose. It cannot tell you whether the
# Helm chart's Deployments schedule, whether the migrate HOOK runs before the API starts (compose
# uses a one-shot service and `depends_on: service_completed_successfully`; the chart uses a
# pre-install/pre-upgrade Job, which is a different mechanism with different failure modes),
# whether the Services select the right pods, or whether the probes the chart declares actually
# pass against a running container.
#
# Until this existed the chart was only ever LINTED. That is how it shipped un-installable: it
# sets RULEMIND_ENV=production, which arms a fail-closed check over four secrets, while
# supplying values for two -- valid YAML, CrashLoopBackOff in practice.
#
# Usage:
#   scripts/k8s_up.sh && scripts/smoke_k8s.sh
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

NS="${RULEMIND_K8S_NAMESPACE:-rulemind}"
RELEASE="${RULEMIND_HELM_RELEASE:-rulemind}"
PASS=0; FAIL=0; SKIP=0
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL+1)); }
skip() { printf '  \033[33mSKIP\033[0m  %s\n' "$1"; SKIP=$((SKIP+1)); }
note() { printf '        %s\n' "$1"; }

PF_PID=""
cleanup() { [ -n "$PF_PID" ] && kill "$PF_PID" 2>/dev/null; }
trap cleanup EXIT

echo
echo "  kubernetes smoke test (kind + helm)"
echo "  ==================================="
note "namespace $NS · release $RELEASE"
echo

command -v kubectl >/dev/null 2>&1 || { echo "kubectl not installed"; exit 2; }
kubectl get ns "$NS" >/dev/null 2>&1 || { echo "namespace $NS not found — run scripts/k8s_up.sh first"; exit 2; }

# ---------------------------------------------------------------- workloads are actually Ready
# `kubectl get deploy` reporting a Deployment is not the same as its pods passing their probes.
# readyReplicas is the field that means "the chart's own readinessProbe returned 200".
for d in api web worker; do
  name="$RELEASE-$d"
  if ! kubectl -n "$NS" get deploy "$name" >/dev/null 2>&1; then skip "$d deployment not present"; continue; fi
  want=$(kubectl -n "$NS" get deploy "$name" -o jsonpath='{.spec.replicas}' 2>/dev/null)
  got=$(kubectl -n "$NS" get deploy "$name" -o jsonpath='{.status.readyReplicas}' 2>/dev/null)
  [ "${got:-0}" = "${want:-1}" ] && ok "$d: ${got:-0}/${want:-1} replicas Ready" \
                                 || bad "$d: ${got:-0}/${want:-1} replicas Ready"
done

# ---------------------------------------------------------------- the migrate hook completed
# The chart runs migrations as a Helm pre-install/pre-upgrade hook Job, not as a container
# command. A hook that fails leaves the API serving against a stale schema, which is precisely
# the failure apps/python-executor/entrypoint.sh refuses to allow on the compose path.
JOB=$(kubectl -n "$NS" get jobs -o name 2>/dev/null | grep -i migrate | head -1)
if [ -n "$JOB" ]; then
  succeeded=$(kubectl -n "$NS" get "$JOB" -o jsonpath='{.status.succeeded}' 2>/dev/null)
  [ "${succeeded:-0}" -ge 1 ] 2>/dev/null && ok "migrate Job completed" || bad "migrate Job has not succeeded"
else
  # Helm deletes hook Jobs by policy on some configurations; absence is not proof of failure, so
  # this is a skip rather than a pass. The alembic check below is the real evidence.
  skip "no migrate Job found (hook-delete-policy may have removed it)"
fi

# ---------------------------------------------------------------- no pod is restarting
RESTARTS=$(kubectl -n "$NS" get pods -o jsonpath='{range .items[*]}{.status.containerStatuses[*].restartCount}{"\n"}{end}' 2>/dev/null | awk '{s+=$1} END {print s+0}')
[ "${RESTARTS:-0}" -eq 0 ] && ok "no container has restarted" \
                          || bad "containers have restarted $RESTARTS time(s) — check for CrashLoopBackOff"

# ---------------------------------------------------------------- reach the API through its Service
# Through the SERVICE, not a pod IP: that is what exercises the selector. A Service whose selector
# matches nothing returns connection-refused while every pod is perfectly healthy.
kubectl -n "$NS" port-forward "svc/$RELEASE-api" 18080:8080 >/dev/null 2>&1 &
PF_PID=$!
for _ in $(seq 1 30); do
  curl -sS --max-time 3 http://127.0.0.1:18080/health >/dev/null 2>&1 && break
  sleep 1
done

CURL=(curl -sS --max-time 15)
if "${CURL[@]}" http://127.0.0.1:18080/health >/dev/null 2>&1; then
  ok "/health answers through the api Service"
else
  bad "/health unreachable through the api Service"
fi

READY=$("${CURL[@]}" http://127.0.0.1:18080/ready 2>/dev/null || echo '{}')
for dep in db redis sandbox; do
  got=$(printf '%s' "$READY" | python3 -c "import sys,json;print(json.load(sys.stdin).get('$dep','missing'))" 2>/dev/null || echo missing)
  [ "$got" = "ok" ] && ok "/ready reports $dep ok" || bad "/ready reports $dep=$got"
done

# The one platform assumption the product rests on: if the runtime forbids `spawn`, every
# user-authored Python variable fails and nothing works -- while /health still answers 200.
MODE=$(printf '%s' "$READY" | python3 -c "import sys,json;print(json.load(sys.stdin).get('sandbox_mode','?'))" 2>/dev/null || echo '?')
case "$MODE" in
  pool)            ok "sandbox runs in a real process pool (mode=pool)" ;;
  inline_fallback) bad "sandbox fell back to INLINE — untrusted code is running in the API process" ;;
  *)               bad "sandbox mode is '$MODE'" ;;
esac

# ---------------------------------------------------------------- auth is on
code=$("${CURL[@]}" -o /dev/null -w '%{http_code}' http://127.0.0.1:18080/api/v1/policies 2>/dev/null || echo 000)
[ "$code" = "401" ] && ok "an unauthenticated caller is rejected (401)" \
                    || bad "/api/v1/policies returned $code unauthenticated, expected 401"

# ---------------------------------------------------------------- schema is at head
POD=$(kubectl -n "$NS" get pods -l "app.kubernetes.io/component=api" -o name 2>/dev/null | head -1)
[ -z "$POD" ] && POD=$(kubectl -n "$NS" get pods -o name 2>/dev/null | grep -- "-api-" | head -1)
if [ -n "$POD" ]; then
  if kubectl -n "$NS" exec "$POD" -- python3 -m alembic -c alembic.ini current 2>/dev/null | grep -q "(head)"; then
    ok "alembic reports the schema at head"
  else
    bad "alembic does not report (head) — the migrate hook did not bring the schema up"
  fi
  # This image runs untrusted customer Python in the sandbox. Running it as uid 0 would also be
  # rejected by any cluster enforcing the `restricted` Pod Security Standard.
  uid=$(kubectl -n "$NS" exec "$POD" -- id -u 2>/dev/null | tr -d '\r')
  [ -n "$uid" ] && [ "$uid" != "0" ] && ok "api container runs as non-root (uid $uid)" \
                                     || bad "api container runs as uid ${uid:-unknown}"
else
  skip "no api pod found for exec checks"
fi

# ---------------------------------------------------------------- a real decision, end to end
KEY=""
[ -f .runtime/k8s-key.env ] && . ./.runtime/k8s-key.env 2>/dev/null && KEY="${RULEMIND_K8S_API_KEY:-}"
if [ -z "$KEY" ]; then
  note "no .runtime/k8s-key.env — run scripts/k8s_up.sh to generate one"
  skip "authenticated: a decision runs end to end"
else
  AUTH=(-H "x-api-key: $KEY")
  "${CURL[@]}" -X POST "${AUTH[@]}" -H 'content-type: application/json' -d '{}' \
    http://127.0.0.1:18080/api/v1/onboarding/load-samples >/dev/null 2>&1
  POLICIES=$("${CURL[@]}" "${AUTH[@]}" http://127.0.0.1:18080/api/v1/policies 2>/dev/null || echo '[]')
  COUNT=$(printf '%s' "$POLICIES" | python3 -c "import sys,json;d=json.load(sys.stdin);print(len(d) if isinstance(d,list) else 0)" 2>/dev/null || echo 0)
  if [ "${COUNT:-0}" -gt 0 ]; then
    ok "authenticated read works — $COUNT policies"
    PID=$(printf '%s' "$POLICIES" | python3 -c "
import sys,json
d=json.load(sys.stdin); prod=[p for p in d if p.get('status')=='prod'] or d
print(prod[0]['id'])" 2>/dev/null || echo "")
    DEC=$("${CURL[@]}" -X POST "${AUTH[@]}" -H 'content-type: application/json' \
      -d "{\"policy_id\":\"$PID\",\"payload\":{\"user_id\":\"k8s-smoke\"}}" \
      http://127.0.0.1:18080/api/v1/decide 2>/dev/null || echo '{}')
    OUTCOME=$(printf '%s' "$DEC" | python3 -c "import sys,json;print(json.load(sys.stdin).get('outcome','<none>'))" 2>/dev/null || echo '<none>')
    case "$OUTCOME" in
      approve|reject|review|pending) ok "a decision runs end to end (outcome=$OUTCOME)" ;;
      *) bad "decide returned outcome='$OUTCOME'"; note "$(printf '%s' "$DEC" | head -c 160)" ;;
    esac
    # T1.2: a failed variable is None, indistinguishable from a legitimate null unless the
    # response says which it was.
    printf '%s' "$DEC" | grep -q '"variable_errors"' && ok "/decide exposes variable_errors (T1.2 contract)" \
                                                     || bad "/decide response has no variable_errors"
  else
    bad "authenticated read returned $COUNT policies"
  fi
fi

echo
echo "  ---------------------------------------"
printf '  %d passed, %d failed, %d skipped\n' "$PASS" "$FAIL" "$SKIP"
echo
[ "$FAIL" -eq 0 ] || exit 1
