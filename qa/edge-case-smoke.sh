#!/bin/bash
# RuleMind AI — Exhaustive QA Edge-Case Smoke Test
# Tests ~60 backend API edge cases via curl against running FastAPI server
set -euo pipefail

BASE="${1:-http://localhost:8080}"
PASS=0
FAIL=0
ERRORS=()

assert_status() {
  local desc="$1" expected="$2" method="$3" url="$4"
  shift 4
  local body="${1:-}"
  local actual
  if [ -n "$body" ]; then
    actual=$(curl -s -o /dev/null -w "%{http_code}" -X "$method" -H "Content-Type: application/json" -d "$body" "$url" 2>/dev/null)
  else
    actual=$(curl -s -o /dev/null -w "%{http_code}" -X "$method" "$url" 2>/dev/null)
  fi
  if [ "$actual" = "$expected" ]; then
    echo "  PASS: $desc ($actual)"
    PASS=$((PASS+1))
  else
    echo "  FAIL: $desc (expected $expected, got $actual)"
    FAIL=$((FAIL+1))
    ERRORS+=("$desc: expected $expected got $actual")
  fi
}

assert_body_contains() {
  local desc="$1" needle="$2" method="$3" url="$4"
  shift 4
  local body="${1:-}"
  local response
  if [ -n "$body" ]; then
    response=$(curl -s -X "$method" -H "Content-Type: application/json" -d "$body" "$url" 2>/dev/null)
  else
    response=$(curl -s -X "$method" "$url" 2>/dev/null)
  fi
  if echo "$response" | grep -q "$needle"; then
    echo "  PASS: $desc (contains '$needle')"
    PASS=$((PASS+1))
  else
    echo "  FAIL: $desc (missing '$needle' in response)"
    FAIL=$((FAIL+1))
    ERRORS+=("$desc: missing '$needle'")
  fi
}

echo "============================================"
echo " RuleMind QA Edge-Case Smoke Test"
echo " Target: $BASE"
echo "============================================"
echo ""

# ── SECTION 1: Health & Bootstrap ──
echo "── 1. Health & Bootstrap ──"
assert_status "Health endpoint returns 200" "200" GET "$BASE/health"
assert_status "Ready endpoint returns 200" "200" GET "$BASE/ready"
assert_status "Bootstrap returns 200" "200" GET "$BASE/api/v1/bootstrap"
assert_body_contains "Bootstrap has connectors" "connectors" GET "$BASE/api/v1/bootstrap"
echo ""

# ── SECTION 2: Entity Validation Edge Cases ──
echo "── 2. Entity Validation Edge Cases ──"

# Variable with unknown source
assert_status "Variable unknown source_id → 422" "422" POST "$BASE/api/v1/variables" \
  '{"name":"Ghost Src","category":"Custom","source_id":"nonexistent_connector","code":"def run(p,c): return 1","status":"dev"}'

# Rule with empty nodes
assert_status "Rule with empty nodes → 422" "422" POST "$BASE/api/v1/rules" \
  '{"name":"Empty Rule","nodes":[],"status":"dev"}'

# Rule with invalid operator
assert_status "Rule with invalid operator → 422" "422" POST "$BASE/api/v1/rules" \
  '{"name":"Bad Operator","nodes":[{"id":"c1","type":"condition","variable":"bureau_score","operator":"LIKE","value":"700"}],"status":"dev"}'

# Scorecard with empty bins
assert_status "Scorecard with empty bins → 422" "422" POST "$BASE/api/v1/scorecards" \
  '{"name":"Empty Bins","variable_bins":[],"status":"dev"}'

# Policy with empty steps
assert_status "Policy with empty steps → 422" "422" POST "$BASE/api/v1/policies" \
  '{"name":"Empty Steps","steps":[],"defaultOutcome":"review","status":"dev"}'

echo ""

# ── SECTION 3: GET/DELETE Nonexistent Entities → 404 ──
echo "── 3. Nonexistent Entity Access → 404 ──"
assert_status "GET nonexistent variable → 404" "404" GET "$BASE/api/v1/variables/ghost_var_id"
assert_status "GET nonexistent rule → 404" "404" GET "$BASE/api/v1/rules/ghost_rule_id"
assert_status "GET nonexistent policy → 404" "404" GET "$BASE/api/v1/policies/ghost_policy_id"
assert_status "GET nonexistent scorecard → 404" "404" GET "$BASE/api/v1/scorecards/ghost_sc_id"
assert_status "DELETE nonexistent variable → 404" "404" DELETE "$BASE/api/v1/variables/ghost_var_id"
assert_status "DELETE nonexistent rule → 404" "404" DELETE "$BASE/api/v1/rules/ghost_rule_id"
assert_status "DELETE nonexistent policy → 404" "404" DELETE "$BASE/api/v1/policies/ghost_policy_id"
echo ""

# ── SECTION 4: Decision Execution Edge Cases ──
echo "── 4. Decision Execution Edge Cases ──"
assert_status "Decide nonexistent policy → 404" "404" POST "$BASE/api/v1/decide" \
  '{"policy_id":"ghost_policy","payload":{}}'

assert_status "Batch decide missing targetId → 422" "422" POST "$BASE/api/v1/decide/batch" \
  '{"payloads":[{"score":700}]}'

# Execute policy with sample payload (should work with seeded policy)
POLICIES=$(curl -s "$BASE/api/v1/policies" 2>/dev/null)
POLICY_ID=$(echo "$POLICIES" | python3 -c "import sys,json; ps=json.load(sys.stdin); print(ps[0]['id'] if ps else '')" 2>/dev/null || echo "")
if [ -n "$POLICY_ID" ]; then
  assert_status "Execute seeded policy → 200" "200" POST "$BASE/api/v1/policies/$POLICY_ID/execute" \
    '{"payload":{"bureau":{"score":750},"bank":{"avgBalance":45000}}}'
  assert_body_contains "Execute returns outcome" "outcome" POST "$BASE/api/v1/policies/$POLICY_ID/execute" \
    '{"payload":{"bureau":{"score":750},"bank":{"avgBalance":45000}}}'
fi
echo ""

# ── SECTION 5: Variable Python Sandbox Edge Cases ──
echo "── 5. Python Sandbox Edge Cases ──"

# Get a valid source_id from bootstrap
SOURCE_ID=$(curl -s "$BASE/api/v1/connectors" 2>/dev/null | python3 -c "import sys,json; cs=json.load(sys.stdin); print(cs[0]['id'] if cs else '')" 2>/dev/null || echo "")

if [ -n "$SOURCE_ID" ]; then
  # Variable with syntax error
  assert_body_contains "Variable syntax error returns error" "error" POST "$BASE/api/v1/test/variables" \
    "{\"code\":\"def run(p,c):\\n  return ((\",\"sample_payload\":{\"bureau\":{\"score\":750}},\"source_id\":\"$SOURCE_ID\"}"

  # Variable importing blocked module
  assert_body_contains "Variable blocked import returns error" "error" POST "$BASE/api/v1/test/variables" \
    "{\"code\":\"import os\\ndef run(p,c):\\n  return os.listdir('/')\",\"sample_payload\":{},\"source_id\":\"$SOURCE_ID\"}"

  # Variable returning None
  assert_body_contains "Variable returning None" "result" POST "$BASE/api/v1/test/variables" \
    "{\"code\":\"def run(p,c):\\n  return None\",\"sample_payload\":{},\"source_id\":\"$SOURCE_ID\"}"

  # Variable with very long output
  assert_body_contains "Variable long output" "result" POST "$BASE/api/v1/test/variables" \
    "{\"code\":\"def run(p,c):\\n  return 'x' * 10000\",\"sample_payload\":{},\"source_id\":\"$SOURCE_ID\"}"
fi
echo ""

# ── SECTION 6: Promotion Edge Cases ──
echo "── 6. Promotion Edge Cases ──"

# Create a dev variable, try to promote without testing
if [ -n "$SOURCE_ID" ]; then
  CREATE_RESP=$(curl -s -X POST -H "Content-Type: application/json" \
    -d "{\"name\":\"QA Edge Var\",\"category\":\"Custom\",\"source_id\":\"$SOURCE_ID\",\"code\":\"def run(p,c):\\n  return 42\",\"status\":\"dev\"}" \
    "$BASE/api/v1/variables" 2>/dev/null)
  QA_VAR_ID=$(echo "$CREATE_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null || echo "")

  if [ -n "$QA_VAR_ID" ]; then
    assert_status "Promote untested variable → 409" "409" POST "$BASE/api/v1/variables/$QA_VAR_ID/promote" \
      '{"promoted_by":"qa-tester","reason":"edge case test"}'

    # Now test it, then promote
    curl -s -X POST -H "Content-Type: application/json" \
      -d "{\"sample_payload\":{\"bureau\":{\"score\":750}},\"source_id\":\"$SOURCE_ID\"}" \
      "$BASE/api/v1/variables/$QA_VAR_ID/test" > /dev/null 2>&1

    assert_status "Promote tested variable → 200" "200" POST "$BASE/api/v1/variables/$QA_VAR_ID/promote" \
      '{"promoted_by":"qa-tester","reason":"edge case test"}'

    # Try to delete non-dev (now UAT) variable
    assert_status "Delete UAT variable → 409" "409" DELETE "$BASE/api/v1/variables/$QA_VAR_ID"
  fi
fi
echo ""

# ── SECTION 7: SDK Endpoint Edge Cases ──
echo "── 7. SDK Endpoint Edge Cases ──"

assert_status "SDK health → 200" "200" GET "$BASE/sdk/v1/health"

# SDK bundle with no prod assets — should be 404 or compile on-the-fly
# (depends on whether any assets have been promoted to prod)
SDK_BUNDLE_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/sdk/v1/bundle" 2>/dev/null)
if [ "$SDK_BUNDLE_STATUS" = "404" ] || [ "$SDK_BUNDLE_STATUS" = "200" ]; then
  echo "  PASS: SDK bundle returns valid status ($SDK_BUNDLE_STATUS)"
  PASS=$((PASS+1))
else
  echo "  FAIL: SDK bundle unexpected status ($SDK_BUNDLE_STATUS)"
  FAIL=$((FAIL+1))
  ERRORS+=("SDK bundle unexpected status: $SDK_BUNDLE_STATUS")
fi

assert_status "SDK decide nonexistent policy → 404" "404" POST "$BASE/sdk/v1/decide" \
  '{"policyId":"ghost_policy","payload":{}}'

assert_status "SDK events empty array → 200" "200" POST "$BASE/sdk/v1/events" \
  '{"events":[]}'

assert_body_contains "SDK events returns received count" "received" POST "$BASE/sdk/v1/events" \
  '{"events":[]}'

assert_status "SDK get nonexistent execution → 404" "404" GET "$BASE/sdk/v1/executions/ghost_exec_id"

assert_status "SDK resume nonexistent execution → 404" "404" POST "$BASE/sdk/v1/executions/ghost_exec_id/resume" \
  '{"decision":"approve","reviewerId":"qa","response":{}}'

assert_body_contains "SDK experience manifest has structure" "journeys" GET "$BASE/sdk/v1/experience-manifest"
echo ""

# ── SECTION 8: Export/Import Edge Cases ──
echo "── 8. Export/Import Edge Cases ──"
assert_status "Export JSON → 200" "200" GET "${BASE}/api/v1/export?format=json"
EXPORT_CHECK=$(curl -s "${BASE}/api/v1/export?format=json" 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print('ok' if 'variables' in d else 'fail')" 2>/dev/null)
if [ "$EXPORT_CHECK" = "ok" ]; then
  echo "  PASS: Export contains variables key"
  PASS=$((PASS+1))
else
  echo "  FAIL: Export missing variables key"
  FAIL=$((FAIL+1))
  ERRORS+=("Export missing variables key")
fi

# Round-trip: export then import
EXPORT_DATA=$(curl -s "$BASE/api/v1/export?format=json" 2>/dev/null)
assert_status "Import round-trip → 200" "200" POST "$BASE/api/v1/import" "$EXPORT_DATA"
echo ""

# ── SECTION 9: Connector Edge Cases ──
echo "── 9. Connector Edge Cases ──"

# Get first active connector
CONN_ID=$(curl -s "$BASE/api/v1/connectors" 2>/dev/null | python3 -c "import sys,json; cs=[c for c in json.load(sys.stdin) if c.get('active')]; print(cs[0]['id'] if cs else '')" 2>/dev/null || echo "")
if [ -n "$CONN_ID" ]; then
  assert_status "Delete active connector → 409" "409" DELETE "$BASE/api/v1/connectors/$CONN_ID"
fi
echo ""

# ── SECTION 10: Audit & Logs Edge Cases ──
echo "── 10. Audit & Logs ──"
assert_status "Audit decisions → 200" "200" GET "$BASE/api/v1/audit/decisions"
assert_status "Audit promotions → 200" "200" GET "$BASE/api/v1/audit/promotions"
assert_status "Audit decisions returns 200" "200" GET "$BASE/api/v1/audit/decisions"
echo ""

# ── SECTION 11: Rule Engine Operator Edge Cases ──
echo "── 11. Rule Engine Operator Validation ──"

# Create valid dev variables for rule testing
if [ -n "$SOURCE_ID" ]; then
  # Test rule creation with valid operators (condition + outcome nodes required)
  # Use a single representative rule to prove the engine accepts conditions
  RULE_STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "Content-Type: application/json" \
    -d '{"name":"Op EQ Test","nodes":[{"id":"c1","type":"condition","variable":"bureau_score","operator":"==","value":"700","children":["a1"]},{"id":"a1","type":"approve"}],"status":"dev"}' \
    "$BASE/api/v1/rules" 2>/dev/null)
  if [ "$RULE_STATUS" = "200" ] || [ "$RULE_STATUS" = "201" ]; then
    echo "  PASS: Rule with == operator + outcome created ($RULE_STATUS)"
    PASS=$((PASS+1))
  else
    echo "  FAIL: Rule with == operator rejected ($RULE_STATUS)"
    FAIL=$((FAIL+1))
    ERRORS+=("Rule == operator: expected 200, got $RULE_STATUS")
  fi

  # Backend ALLOWED_OPERATORS = {>=, <=, ==, >, <, !=} — only 6 comparison operators
  # Extended operators (between, in, not_in, regex, exists, !exists) are supported
  # by the TypeScript engine and SDKs but NOT by the Python backend validator.
  # This is a known gap (FINDING: SDK/engine operator superset vs backend subset).
  for OP in "!=" ">" ">=" "<" "<="; do
    RULE_STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "Content-Type: application/json" \
      -d "{\"name\":\"Op $OP\",\"nodes\":[{\"id\":\"c1\",\"type\":\"condition\",\"variable\":\"bureau_score\",\"operator\":\"$OP\",\"value\":\"700\",\"children\":[\"a1\"]},{\"id\":\"a1\",\"type\":\"approve\"}],\"status\":\"dev\"}" \
      "$BASE/api/v1/rules" 2>/dev/null)
    if [ "$RULE_STATUS" = "200" ] || [ "$RULE_STATUS" = "201" ]; then
      echo "  PASS: Rule with '$OP' operator created ($RULE_STATUS)"
      PASS=$((PASS+1))
    else
      echo "  FAIL: Rule with '$OP' operator rejected ($RULE_STATUS)"
      FAIL=$((FAIL+1))
      ERRORS+=("Rule '$OP': expected 200, got $RULE_STATUS")
    fi
  done

  # Verify extended operators are correctly rejected by backend
  assert_status "Extended op 'between' rejected → 422" "422" POST "$BASE/api/v1/rules" \
    '{"name":"Op between","nodes":[{"id":"c1","type":"condition","variable":"bureau_score","operator":"between","value":"600,800","children":["a1"]},{"id":"a1","type":"approve"}],"status":"dev"}'
  assert_status "Extended op 'regex' rejected → 422" "422" POST "$BASE/api/v1/rules" \
    '{"name":"Op regex","nodes":[{"id":"c1","type":"condition","variable":"bureau_score","operator":"regex","value":".*","children":["a1"]},{"id":"a1","type":"approve"}],"status":"dev"}'

  # AND/OR group nesting
  RULE_STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "Content-Type: application/json" \
    -d '{"name":"Nested AND-OR","nodes":[{"id":"g1","type":"and","children":["c1","c2"]},{"id":"c1","type":"condition","variable":"bureau_score","operator":">","value":"600","children":["a1"]},{"id":"c2","type":"condition","variable":"bureau_score","operator":"<","value":"800"},{"id":"a1","type":"approve"}],"status":"dev"}' \
    "$BASE/api/v1/rules" 2>/dev/null)
  if [ "$RULE_STATUS" = "200" ] || [ "$RULE_STATUS" = "201" ]; then
    echo "  PASS: Rule with AND group nesting created ($RULE_STATUS)"
    PASS=$((PASS+1))
  else
    echo "  FAIL: Rule with AND group nesting rejected ($RULE_STATUS)"
    FAIL=$((FAIL+1))
    ERRORS+=("Rule AND nesting: expected 200, got $RULE_STATUS")
  fi
fi
echo ""

# ── SECTION 12: Concurrent/Idempotency Edge Cases ──
echo "── 12. Concurrent & Idempotency ──"

# Same variable name twice → second gets suffix
if [ -n "$SOURCE_ID" ]; then
  curl -s -X POST -H "Content-Type: application/json" \
    -d "{\"name\":\"Duplicate Test\",\"category\":\"Custom\",\"source_id\":\"$SOURCE_ID\",\"code\":\"def run(p,c): return 1\",\"status\":\"dev\"}" \
    "$BASE/api/v1/variables" > /dev/null 2>&1
  DUP_RESP=$(curl -s -X POST -H "Content-Type: application/json" \
    -d "{\"name\":\"Duplicate Test\",\"category\":\"Custom\",\"source_id\":\"$SOURCE_ID\",\"code\":\"def run(p,c): return 2\",\"status\":\"dev\"}" \
    "$BASE/api/v1/variables" 2>/dev/null)
  DUP_ID=$(echo "$DUP_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null || echo "")
  if [ -n "$DUP_ID" ]; then
    echo "  PASS: Duplicate variable name accepted (deduplication via make_id)"
    PASS=$((PASS+1))
  else
    echo "  FAIL: Duplicate variable name rejected"
    FAIL=$((FAIL+1))
    ERRORS+=("Duplicate name handling")
  fi
fi
echo ""

# ── SECTION 13: Admin Auth Edge Cases (with AUTH_MODE=none) ──
echo "── 13. Admin Auth Endpoints ──"
assert_status "Admin login wrong password → 401" "401" POST "$BASE/api/admin/v1/auth/login" \
  '{"email":"admin@rulemind.local","password":"wrong-password"}'

assert_status "Admin login correct → 200" "200" POST "$BASE/api/admin/v1/auth/login" \
  '{"email":"admin@rulemind.local","password":"rulemind-admin"}'
echo ""

# ── SECTION 14: SDK Execution Sync & Resume Flow ──
echo "── 14. SDK Execution Lifecycle ──"

# Create execution via sync
SYNC_RESP=$(curl -s -X POST -H "Content-Type: application/json" \
  -d '{"executionId":"qa-exec-001","policyId":"test-policy","payload":{"score":750},"outcome":"approve","status":"completed","variables":{},"ruleResults":[],"trace":[],"pendingOperations":[],"latencyMs":25,"source":"sdk_edge"}' \
  "$BASE/sdk/v1/executions/sync" 2>/dev/null)
SYNC_STATUS=$(echo "$SYNC_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")
if [ -n "$SYNC_STATUS" ]; then
  echo "  PASS: SDK execution sync accepted (status=$SYNC_STATUS)"
  PASS=$((PASS+1))
else
  echo "  FAIL: SDK execution sync failed"
  FAIL=$((FAIL+1))
  ERRORS+=("SDK execution sync")
fi

# Fetch the execution back
assert_status "SDK get synced execution → 200" "200" GET "$BASE/sdk/v1/executions/qa-exec-001"

# Sync again with same ID (update)
RESYNC_STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "Content-Type: application/json" \
  -d '{"executionId":"qa-exec-001","policyId":"test-policy","payload":{"score":750},"outcome":"approve","status":"completed","variables":{},"ruleResults":[],"trace":[],"pendingOperations":[],"latencyMs":30,"source":"sdk_edge"}' \
  "$BASE/sdk/v1/executions/sync" 2>/dev/null)
if [ "$RESYNC_STATUS" = "200" ]; then
  echo "  PASS: SDK execution re-sync (update) accepted"
  PASS=$((PASS+1))
else
  echo "  FAIL: SDK execution re-sync failed ($RESYNC_STATUS)"
  FAIL=$((FAIL+1))
  ERRORS+=("SDK execution re-sync: $RESYNC_STATUS")
fi

# Events with multiple items
assert_status "SDK events batch → 200" "200" POST "$BASE/sdk/v1/events" \
  '{"events":[{"type":"decision","timestamp":"2026-04-06T00:00:00Z","data":{"policyId":"p1"}},{"type":"sync","timestamp":"2026-04-06T00:00:01Z","data":{"bundleVersion":1}}]}'

assert_body_contains "SDK events batch returns received=2" "\"received\":2" POST "$BASE/sdk/v1/events" \
  '{"events":[{"type":"decision","timestamp":"2026-04-06T00:00:00Z","data":{"policyId":"p1"}},{"type":"sync","timestamp":"2026-04-06T00:00:01Z","data":{"bundleVersion":1}}]}'
echo ""

# ── SUMMARY ──
echo "============================================"
echo " RESULTS: $PASS passed, $FAIL failed"
echo "============================================"
if [ ${#ERRORS[@]} -gt 0 ]; then
  echo ""
  echo "Failed tests:"
  for err in "${ERRORS[@]}"; do
    echo "  - $err"
  done
fi
echo ""
exit $FAIL
