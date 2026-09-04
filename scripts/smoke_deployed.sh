#!/usr/bin/env bash
# Smoke test against the DEPLOYED instance — the half `smoke_compose.sh` cannot cover.
#
# smoke_compose.sh boots the stack locally and proves the image works. It cannot tell you what
# your response headers look like from the public internet, who can read your API docs, whether
# CORS is scoped to the origin you think it is, or whether the sandbox spawns on the platform
# you actually deployed to. Every one of those is invisible until something is running somewhere
# and you ask it from outside — and every one of them has been wrong here at least once.
#
# Usage:
#   cp .env.deployed.example .env.deployed   # then paste the key (see below)
#   scripts/smoke_deployed.sh
#
# .env.deployed is gitignored and holds a LIVE API key. Nothing in this script ever echoes it:
# it goes into curl via a header and is never printed, not even on failure.
#
#   RULEMIND_DEPLOYED_API=https://rulemind-api.onrender.com
#   RULEMIND_DEPLOYED_WEB=https://rulemind-dashboard.onrender.com
#   RULEMIND_DEPLOYED_API_KEY=<Render -> rulemind-api -> Environment -> RULEMIND_DEV_API_KEY>
set -uo pipefail

cd "$(dirname "$0")/.." || exit 2

ENV_FILE="${RULEMIND_DEPLOYED_ENV:-.env.deployed}"
if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  set -a; . "./$ENV_FILE"; set +a
fi

API="${RULEMIND_DEPLOYED_API:-https://rulemind-api.onrender.com}"
WEB="${RULEMIND_DEPLOYED_WEB:-https://rulemind-dashboard.onrender.com}"
KEY="${RULEMIND_DEPLOYED_API_KEY:-}"

PASS=0
FAIL=0
SKIP=0

ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL+1)); }
skip() { printf '  \033[33mSKIP\033[0m  %s\n' "$1"; SKIP=$((SKIP+1)); }
note() { printf '        %s\n' "$1"; }

# The free tier sleeps after 15 minutes idle and takes ~50s to wake, so every timeout here is
# generous. A short timeout would report a cold start as an outage.
CURL=(curl -sS --max-time 90)

echo
echo "  deployed smoke test"
echo "  ==================="
note "api $API"
note "web $WEB"
echo

# ---------------------------------------------------------------- liveness and dependencies
READY=$("${CURL[@]}" "$API/ready" 2>/dev/null || echo '{}')
for dep in db redis sandbox; do
  got=$(printf '%s' "$READY" | python3 -c "import sys,json;print(json.load(sys.stdin).get('$dep','missing'))" 2>/dev/null || echo missing)
  if [ "$got" = "ok" ]; then
    ok "/ready reports $dep ok"
  else
    bad "/ready reports $dep=$got"
  fi
done

# The one platform assumption the whole deployment rests on. If the runtime forbids `spawn`,
# every user-authored Python variable fails and the product does not work -- while /ready would
# still have answered 200 before this field existed.
MODE=$(printf '%s' "$READY" | python3 -c "import sys,json;print(json.load(sys.stdin).get('sandbox_mode','?'))" 2>/dev/null || echo '?')
if [ "$MODE" = "pool" ]; then
  ok "sandbox runs in a real process pool (mode=$MODE)"
elif [ "$MODE" = "inline_fallback" ]; then
  bad "sandbox fell back to INLINE — untrusted code is running in the API process"
else
  bad "sandbox mode is '$MODE'"
fi

# ---------------------------------------------------------------- security headers
HDRS=$("${CURL[@]}" -D- -o /dev/null "$API/health" 2>/dev/null || true)
check_header() {
  if printf '%s' "$HDRS" | grep -qi "^$1:"; then ok "header $1"; else bad "missing header $1"; fi
}
check_header "x-content-type-options"
check_header "x-frame-options"
check_header "referrer-policy"
check_header "content-security-policy"
# HSTS only matters over TLS, and is only correct to send there.
case "$API" in
  https://*) check_header "strict-transport-security" ;;
  *)         skip "HSTS (target is not https)" ;;
esac

# ---------------------------------------------------------------- docs must not be public
for p in /docs /redoc /openapi.json; do
  code=$("${CURL[@]}" -o /dev/null -w '%{http_code}' "$API$p" 2>/dev/null || echo 000)
  if [ "$code" = "404" ]; then
    ok "$p is not published ($code)"
  else
    bad "$p is publicly readable ($code) — that is the endpoint map and every schema"
  fi
done

# ---------------------------------------------------------------- auth is enforced
for p in /api/v1/policies /api/v1/rules /api/v1/decide; do
  code=$("${CURL[@]}" -o /dev/null -w '%{http_code}' "$API$p" 2>/dev/null || echo 000)
  [ "$code" = "401" ] && ok "$p rejects an unauthenticated caller" \
                      || bad "$p returned $code unauthenticated, expected 401"
done
code=$("${CURL[@]}" -o /dev/null -w '%{http_code}' -H "x-api-key: rm_live_not_a_real_key" "$API/api/v1/policies" 2>/dev/null || echo 000)
[ "$code" = "401" ] && ok "a wrong API key is rejected" || bad "a wrong API key returned $code"

# ---------------------------------------------------------------- CORS is scoped, not a wildcard
# allow_credentials is on and the member session is an httpOnly cookie, so a wildcard would let
# any site on the internet make credentialed calls with a logged-in user's session.
ACAO=$("${CURL[@]}" -D- -o /dev/null -H "Origin: $WEB" "$API/health" 2>/dev/null \
        | grep -i '^access-control-allow-origin:' | tr -d '\r' | awk '{print $2}')
[ "$ACAO" = "$WEB" ] && ok "CORS echoes the dashboard origin exactly" \
                     || bad "CORS returned '${ACAO:-<none>}' for the dashboard origin"
EVIL=$("${CURL[@]}" -D- -o /dev/null -H "Origin: https://evil.example.com" "$API/health" 2>/dev/null \
        | grep -ci '^access-control-allow-origin:' || true)
[ "$EVIL" = "0" ] && ok "CORS refuses an unlisted origin" \
                  || bad "CORS answered an unlisted origin — check for a wildcard"

# ---------------------------------------------------------------- the dashboard
code=$("${CURL[@]}" -o /dev/null -w '%{http_code}' "$WEB/" 2>/dev/null || echo 000)
[ "$code" = "200" ] && ok "dashboard serves" || bad "dashboard returned $code"
# A static export emits a real index.html per route; a deep link proves it is not a SPA shell
# silently serving the wrong page.
code=$("${CURL[@]}" -o /dev/null -w '%{http_code}' "$WEB/policies/" 2>/dev/null || echo 000)
[ "$code" = "200" ] && ok "a deep link serves its own page" || bad "/policies/ returned $code"
# The shipped bundle must point at the deployed API. It pointed at localhost for a long time,
# because NEXT_PUBLIC_* is inlined at BUILD time and the runtime env: block was a no-op.
if "${CURL[@]}" "$WEB/" 2>/dev/null | grep -q "$(printf '%s' "$API" | sed 's#https\?://##')"; then
  ok "the served page references the deployed API host"
else
  bad "the served page does not mention $API — check NEXT_PUBLIC_API_BASE_URL at build time"
fi

# ---------------------------------------------------------------- authenticated: a real decision
if [ -z "$KEY" ]; then
  echo
  note "no RULEMIND_DEPLOYED_API_KEY set — skipping the authenticated checks."
  note "put it in $ENV_FILE (gitignored); Render -> rulemind-api -> Environment."
  skip "authenticated: workspace is reachable"
  skip "authenticated: a decision runs end to end"
  skip "authenticated: the decision lands in the audit trail"
else
  AUTH=(-H "x-api-key: $KEY")

  POLICIES=$("${CURL[@]}" "${AUTH[@]}" "$API/api/v1/policies" 2>/dev/null || echo '[]')
  COUNT=$(printf '%s' "$POLICIES" | python3 -c "import sys,json;d=json.load(sys.stdin);print(len(d) if isinstance(d,list) else 0)" 2>/dev/null || echo 0)
  if [ "$COUNT" -gt 0 ]; then
    ok "authenticated read works — $COUNT policies"
  else
    bad "authenticated read returned $COUNT policies"
    note "an empty workspace is expected on a fresh deploy: POST /api/v1/onboarding/load-samples"
  fi

  if [ "$COUNT" -gt 0 ]; then
    POLICY=$(printf '%s' "$POLICIES" | python3 -c "
import sys,json
d=json.load(sys.stdin)
prod=[p for p in d if p.get('status')=='prod'] or d
print(prod[0]['id'])" 2>/dev/null || echo "")
    DEC=$("${CURL[@]}" -X POST "${AUTH[@]}" -H 'content-type: application/json' \
      -d "{\"policy_id\":\"$POLICY\",\"payload\":{\"user_id\":\"deployed-smoke\"}}" \
      "$API/api/v1/decide" 2>/dev/null || echo '{}')

    OUTCOME=$(printf '%s' "$DEC" | python3 -c "import sys,json;print(json.load(sys.stdin).get('outcome','<none>'))" 2>/dev/null || echo '<none>')
    case "$OUTCOME" in
      approve|reject|review|pending) ok "a decision runs end to end (outcome=$OUTCOME)" ;;
      *) bad "decide returned outcome='$OUTCOME'"; note "$(printf '%s' "$DEC" | head -c 200)" ;;
    esac

    # T1.2: a failed variable is None, which is indistinguishable from a legitimate null unless
    # the response says so. Without this a caller cannot tell a real value from a failure.
    if printf '%s' "$DEC" | grep -q '"variable_errors"'; then
      ok "/decide exposes variable_errors (T1.2 contract)"
    else
      bad "/decide response has no variable_errors"
    fi

    DEC_ID=$(printf '%s' "$DEC" | python3 -c "import sys,json;print(json.load(sys.stdin).get('decision_id') or '')" 2>/dev/null || echo "")
    if [ -n "$DEC_ID" ]; then
      if "${CURL[@]}" "${AUTH[@]}" "$API/api/v1/audit/decisions?limit=25" 2>/dev/null | grep -q "$DEC_ID"; then
        ok "the decision is queryable in the audit trail"
      else
        bad "decision $DEC_ID is not in the audit trail"
      fi
    else
      bad "/decide returned no decision_id — nothing to deep-link or audit"
    fi
  else
    skip "a decision runs end to end (no policy to decide against)"
    skip "the decision lands in the audit trail"
  fi
fi

echo
echo "  ---------------------------------------"
printf '  %d passed, %d failed, %d skipped\n' "$PASS" "$FAIL" "$SKIP"
echo
[ "$FAIL" -eq 0 ] || exit 1
