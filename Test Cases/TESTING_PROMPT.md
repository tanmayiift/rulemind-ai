# RULEMIND — COMPLETE QA & TESTING PROMPT

> Use this prompt with any coding agent to systematically test every surface of the RuleMind
> Rule Builder. The test suite spreadsheet (rulemind_test_suite.xlsx) contains 240+ test cases
> across 9 sheets. This prompt tells you HOW to execute each category.

---

## PART 1: PROTOTYPE UI TESTING (In-Browser)

Open the `rule-builder.jsx` artifact in the browser. Execute every test manually or via
automated browser testing (Playwright/Cypress).

### 1A. Operator Matrix (Sheet 1 — 53 test cases)

For each operator test case OP-001 through OP-053:

1. **Setup:** Drag a Condition or Score node onto the canvas
2. **Configure:** Click the node → set Field Type, Field Name, Operator, and Value(s)
   as specified in columns C-F
3. **Add outcome:** Drag an Approve node (for PASS cases) or Reject node (for FAIL cases)
4. **Connect:** Select condition → Connect → click outcome
5. **Switch to Test Console tab**
6. **Enter test input** from column G
7. **Click "Run Test"**
8. **Verify:** Compare the outcome against column H (Expected Result)
9. **Record:** Fill in columns I (Actual Result) and J (Pass/Fail)

**Critical edge cases to watch for:**
- OP-004: String `"Approved"` vs `"approved"` — must be case-sensitive FAIL
- OP-005: Trailing whitespace `"approved "` — must FAIL
- OP-018: Decimal `0.50` vs threshold `0.5` — boundary precision
- OP-032: Between with same lower and upper bound (zero-width range)
- OP-042: Invalid regex pattern `[` — must not crash the engine
- OP-044: Exists with empty string — must FAIL (empty is not "exists")
- OP-050: Non-numeric input `"abc"` to `>` operator — must result in NaN → FAIL
- OP-051: Negative zero `-0` compared to `>= 0` — must PASS

**JSON Mode testing:** For each operator, also test by switching Test Console to "JSON Mode"
and entering the equivalent JSON object. Verify identical results.

### 1B. Logic & Topology (Sheet 2 — 24 test cases)

For each topology test LT-001 through LT-024:

1. **Build the topology** described in column C by dragging nodes and connecting them
2. **Configure each condition** with the fields/operators implied by the test
3. **Switch to Test Console** → enter the JSON from column E
4. **Run Test** → verify outcome matches column F and pass/fail matches column G

**Critical topology tests:**
- LT-009/010: NOT operator — verify it correctly inverts true→false and false→true
- LT-011/012: Nested logic — AND inside OR, OR inside AND
- LT-013: 3-level deep: AND(OR(a,b), NOT(c)) — the hardest nesting case
- LT-017/018: Empty AND/OR with no children — AND must return false, OR must return false
- LT-021: **Circular reference** — create A→B and B→A connections, verify validation
  shows "Circular reference detected" error
- LT-023: Missing field in test input — condition must fail with reason string
- LT-024: Outcome-only rule (no conditions) — should always approve

### 1C. UI & Interaction (Sheet 3 — 51 test cases)

Execute each UI test case manually:

**Canvas tests (UI-001 through UI-009):**
- Drag each of the 10 node types and verify icon, color, label
- Test canvas panning by dragging the background
- **UI-006 is a critical regression:** Pan the canvas first, THEN drag a node — verify
  the node doesn't jump to an incorrect position

**Connection tests (UI-010 through UI-015):**
- Create connections via the Connect button workflow
- Try self-connections (should be blocked)
- Try duplicate connections (should be deduplicated)
- Click on a connection bezier curve to delete it
- Press Escape during connection mode

**Config panel tests (UI-016 through UI-027):**
- For each operator that changes the UI (between shows value2, exists hides value,
  in shows helper text), verify the config panel updates dynamically
- Verify label editing updates the node on canvas in real-time
- Test the Group node's AND/OR dropdown

**Keyboard tests (UI-028 through UI-034):**
- **UI-030 is critical:** While typing in a config input field, press Delete — it must
  delete the character in the input, NOT delete the node. Verify `document.activeElement`
  guard works.
- Test Ctrl+Z undo after adding, deleting, and moving nodes
- Test undo history limit (50 steps)

**Theme tests (UI-035 through UI-038):**
- Toggle dark→light→dark and verify all colors change
- Verify config panel readability in both themes
- Verify expression panel syntax highlighting in both themes

**Validation tests (UI-046 through UI-051):**
- Build an incomplete rule and verify each validation message appears
- Build a complete valid rule and verify zero warnings

### 1D. Import / Export / Save (Sheet 4 — 18 test cases)

Use the provided test fixture JSON files:

1. **rule_simple_credit.json** — Import and verify nodes load correctly
2. **rule_complex_underwriting.json** — Import and verify all node types, connections,
   configs are preserved
3. **rule_malformed.json** — Import and verify no crash, canvas unchanged
4. **rule_partial_no_connections.json** — Import and verify nodes load without connections

**Round-trip fidelity test (IO-007):**
1. Build a complex rule with 8+ nodes and 6+ connections
2. Click Export → save the JSON
3. Click Clear → verify canvas is empty
4. Click Import → load the same JSON
5. **Compare:** Every node ID, type, label, position, config, and connection must be identical
6. **Expression must match** the pre-export expression exactly

**Save/Load cycle (IO-008 through IO-016):**
1. Build rule → Save with name → verify it appears in Rules tab
2. Clear → go to Rules tab → click the saved rule → verify it loads
3. Save 3 different rules → verify all 3 listed in reverse chronological order
4. Check Version History tab → verify version entries with correct counts

### 1E. Test Console (Sheet 5 — 15 test cases)

- **TC-002:** Build a condition with type=boolean → verify Test Console renders a dropdown
  with true/false options (not a text input)
- **TC-003:** Build a condition with type=date → verify Test Console renders a date picker
- **TC-007/008/009:** Toggle JSON mode, enter valid JSON, enter invalid JSON
- **TC-011:** Two conditions using the same field name "score" — verify both appear and
  both are evaluated against the same input value

---

## PART 2: SDK TESTING

### 2A. JavaScript SDK

```bash
# Setup
mkdir rulemind-sdk-test && cd rulemind-sdk-test
npm init -y
npm install @rulemind/sdk  # or the local build

# Create test file
cat > test.js << 'EOF'
const { RuleEngineClient } = require("@rulemind/sdk");

async function runTests() {
  const client = new RuleEngineClient({
    baseUrl: "http://localhost:8080",
    apiKey: "re_test_key_123",
    timeout: 5000,
    retries: 2,
  });

  // SDK-003: Create rule
  console.log("--- SDK-003: Create Rule ---");
  const rule = await client.createRule({
    name: "Test Rule",
    environment: "dev",
    nodes: [
      { id: "c1", type: "condition", label: "Score", x: 100, y: 100,
        config: { fieldType: "number", field: "credit_score", operator: ">=", value: "700" }},
      { id: "a1", type: "approve", label: "Approve", x: 400, y: 100, config: {} },
    ],
    connections: [{ from: "c1", to: "a1" }],
  });
  console.log("Created:", rule); // Expect: { id: "rule_xxx", version: 1 }

  // SDK-004: Evaluate — should approve
  console.log("\n--- SDK-004: Evaluate (should APPROVE) ---");
  const result1 = await client.evaluate(rule.id, { credit_score: 750 });
  console.log("Result:", result1);
  console.assert(result1.outcome === "approve", "Expected approve");
  console.assert(result1.passed === true, "Expected passed=true");

  // SDK-004b: Evaluate — should reject
  console.log("\n--- SDK-004b: Evaluate (should REJECT) ---");
  const result2 = await client.evaluate(rule.id, { credit_score: 600 });
  console.log("Result:", result2);
  console.assert(result2.outcome === "reject", "Expected reject");

  // SDK-005: Batch evaluate
  console.log("\n--- SDK-005: Batch Evaluate ---");
  const inputs = Array.from({ length: 10 }, (_, i) => ({ credit_score: 650 + i * 20 }));
  const batchResults = await client.evaluateBatch(rule.id, inputs);
  console.log("Batch count:", batchResults.length); // Expect: 10
  console.assert(batchResults.length === 10, "Expected 10 results");

  // SDK-006: Invalid API key
  console.log("\n--- SDK-006: Invalid API Key ---");
  const badClient = new RuleEngineClient({ baseUrl: "http://localhost:8080", apiKey: "bad_key" });
  try {
    await badClient.evaluate(rule.id, { credit_score: 750 });
    console.assert(false, "Should have thrown");
  } catch (e) {
    console.log("Error (expected):", e.message); // Expect 401
  }

  // SDK-008: Timeout
  console.log("\n--- SDK-008: Timeout ---");
  const slowClient = new RuleEngineClient({ baseUrl: "http://localhost:8080", apiKey: "re_test_key_123", timeout: 1 });
  try {
    await slowClient.evaluate(rule.id, { credit_score: 750 });
  } catch (e) {
    console.log("Timeout error (expected):", e.message);
  }

  console.log("\n=== All SDK tests complete ===");
}

runTests().catch(console.error);
EOF

node test.js
```

### 2B. Python SDK

```bash
pip install rulemind-sdk  # or local build

python3 << 'EOF'
from rulemind import RuleEngineClient

client = RuleEngineClient(
    base_url="http://localhost:8080",
    api_key="re_test_key_123",
)

# SDK-011: Evaluate
result = client.evaluate("rule_xxx", {"credit_score": 750})
print(f"Outcome: {result.outcome}")      # approve
print(f"Passed: {result.passed}")         # True
print(f"Duration: {result.execution_time_ms}ms")

# SDK-012: Non-existent rule
try:
    client.evaluate("nonexistent_id", {})
except Exception as e:
    print(f"Error (expected): {e}")       # RuleNotFoundError
EOF
```

### 2C. Embedded Component

```jsx
// test-embed.jsx — Mount in any React app
import { RuleBuilder } from "@rulemind/ui";

function TestEmbed() {
  return (
    <div>
      {/* SDK-013: Basic render */}
      <RuleBuilder
        theme="dark"
        environment="dev"
        onSave={(rule) => {
          // SDK-014: Verify callback shape
          console.assert(rule.nodes, "nodes present");
          console.assert(rule.connections, "connections present");
          console.assert(rule.expression, "expression present");
          console.log("onSave fired:", rule);
        }}
        onLoad={async () => {
          // SDK-015: Return saved rule
          return await fetch("/api/v1/rules/rule_xxx").then(r => r.json());
        }}
      />

      {/* SDK-016: Read-only mode */}
      <RuleBuilder readOnly={true} theme="light" />

      {/* SDK-017: Theme prop */}
      <RuleBuilder theme="light" />
    </div>
  );
}
```

---

## PART 3: REST API TESTING

Start the server:
```bash
DATABASE_ADAPTER=sqlite AUTH_MODE=none LOG_LEVEL=debug npm start
```

Execute each API test case:

```bash
BASE="http://localhost:8080/api/v1"

# API-001: Create rule
curl -s -X POST "$BASE/rules" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "API Test Rule",
    "environment": "dev",
    "nodes": [
      {"id":"c1","type":"condition","label":"Score","x":100,"y":100,
       "config":{"fieldType":"number","field":"credit_score","operator":">=","value":"700"}},
      {"id":"a1","type":"approve","label":"Approve","x":400,"y":100,"config":{}}
    ],
    "connections": [{"from":"c1","to":"a1"}]
  }' | jq .
# Expect: { "id": "rule_xxx", "version": 1 }

RULE_ID="<paste id from above>"

# API-002: Invalid body
curl -s -X POST "$BASE/rules" \
  -H "Content-Type: application/json" \
  -d '{"name": "Missing nodes"}' | jq .
# Expect: 400 with validation errors

# API-003: List rules
curl -s "$BASE/rules" | jq .
# Expect: Array with the created rule

# API-004: Get specific rule
curl -s "$BASE/rules/$RULE_ID" | jq .
# Expect: Full rule definition

# API-005: Get non-existent
curl -s -o /dev/null -w "%{http_code}" "$BASE/rules/nonexistent"
# Expect: 404

# API-008: Evaluate — approve
curl -s -X POST "$BASE/rules/$RULE_ID/evaluate" \
  -H "Content-Type: application/json" \
  -d '{"credit_score": 750}' | jq .
# Expect: { "outcome": "approve", "passed": true, "conditionResults": {...}, "executionTimeMs": ... }

# API-008b: Evaluate — reject
curl -s -X POST "$BASE/rules/$RULE_ID/evaluate" \
  -H "Content-Type: application/json" \
  -d '{"credit_score": 600}' | jq .
# Expect: { "outcome": "reject", "passed": false }

# API-009: Evaluate with empty body
curl -s -X POST "$BASE/rules/$RULE_ID/evaluate" \
  -H "Content-Type: application/json" \
  -d '{}' | jq .
# Expect: All conditions fail with "not provided" reason

# API-010: Batch evaluate
curl -s -X POST "$BASE/rules/$RULE_ID/evaluate/batch" \
  -H "Content-Type: application/json" \
  -d '[{"credit_score":750},{"credit_score":600},{"credit_score":700},{"credit_score":699}]' | jq .
# Expect: 4 results: approve, reject, approve, reject

# API-006: Update rule
curl -s -X PUT "$BASE/rules/$RULE_ID" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Updated Rule",
    "nodes": [
      {"id":"c1","type":"condition","label":"Score","x":100,"y":100,
       "config":{"fieldType":"number","field":"credit_score","operator":">=","value":"750"}},
      {"id":"a1","type":"approve","label":"Approve","x":400,"y":100,"config":{}}
    ],
    "connections": [{"from":"c1","to":"a1"}]
  }' | jq .
# Expect: { "version": 2 }

# API-011: List versions
curl -s "$BASE/rules/$RULE_ID/versions" | jq .
# Expect: [{ version: 2, ... }, { version: 1, ... }]

# API-013: Rollback
curl -s -X POST "$BASE/rules/$RULE_ID/rollback" \
  -H "Content-Type: application/json" \
  -d '{"targetVersion": 1}' | jq .
# Expect: Rule restored to v1 (threshold back to 700)

# API-012: Promote
curl -s -X POST "$BASE/rules/$RULE_ID/promote" \
  -H "Content-Type: application/json" \
  -d '{"fromEnv": "dev", "toEnv": "staging"}' | jq .

# API-007: Delete
curl -s -X DELETE "$BASE/rules/$RULE_ID" | jq .
# Expect: { isActive: false }

# API-014: Health
curl -s "http://localhost:8080/health" | jq .
# Expect: { "status": "ok" }

# API-016: Metrics
curl -s "http://localhost:8080/metrics"
# Expect: Prometheus text format with rule_evaluations_total, etc.

# API-012: Audit log
curl -s "$BASE/rules/$RULE_ID/audit" | jq .
# Expect: Array of evaluation audit entries
```

---

## PART 4: DEPLOYMENT TESTING

### 4A. Docker

```bash
# DEP-001: Build
docker build -t rulemind:test .

# DEP-002: Compose up
docker-compose up -d

# DEP-003: Health check
sleep 5
curl -s http://localhost:8080/health | jq .
# Expect: { "status": "ok" }

# Run evaluation through Docker
curl -s -X POST http://localhost:8080/api/v1/rules/test/evaluate \
  -H "Content-Type: application/json" \
  -d '{"credit_score": 750}' | jq .

docker-compose down
```

### 4B. Database Adapters

```bash
# DB-001: PostgreSQL
DATABASE_ADAPTER=postgres DATABASE_URL="postgresql://user:pass@localhost:5432/rules" npm start &
# Run API-001, API-008, API-012 tests, then kill

# DB-002: SQLite
DATABASE_ADAPTER=sqlite npm start &
# Run same tests

# DB-003: MongoDB
DATABASE_ADAPTER=mongodb DATABASE_URL="mongodb://localhost:27017/rules" npm start &
# Run same tests

# DB-004: File-based
DATABASE_ADAPTER=file npm start &
# Check ./data/ directory for JSON files after creating rules

# DB-006/007: Cache behavior
REDIS_URL="redis://localhost:6379" npm start &
# Evaluate rule → check Redis: redis-cli GET "compiled:rule_xxx:v1"
# Evaluate again → check logs for cache hit

# DB-008: Redis down
# Stop Redis, then evaluate → should still work via DB
docker stop redis
curl -s -X POST http://localhost:8080/api/v1/rules/test/evaluate \
  -H "Content-Type: application/json" \
  -d '{"credit_score": 750}' | jq .
# Must still return valid result
```

### 4C. Auth Testing

```bash
# AUTH-010: No auth mode
AUTH_MODE=none npm start &
curl -s http://localhost:8080/api/v1/rules | jq . # Should work without auth

# AUTH-001: JWT mode
AUTH_MODE=jwt ISSUER_BASE_URL="https://yourorg.okta.com" npm start &
# Generate valid JWT and test
curl -s -H "Authorization: Bearer <valid_jwt>" http://localhost:8080/api/v1/rules | jq .

# AUTH-003: No auth header
curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/api/v1/rules
# Expect: 401

# AUTH-004: API key
AUTH_MODE=apikey npm start &
curl -s -H "x-api-key: re_live_validkey" http://localhost:8080/api/v1/rules | jq .

# AUTH-006: RBAC — viewer tries to create
curl -s -o /dev/null -w "%{http_code}" \
  -X POST http://localhost:8080/api/v1/rules \
  -H "x-api-key: re_viewer_key" \
  -H "Content-Type: application/json" \
  -d '{"name":"test","nodes":[],"connections":[]}'
# Expect: 403
```

---

## PART 5: OBSERVABILITY VERIFICATION

### 5A. Structured Logs

```bash
LOG_LEVEL=debug npm start 2>&1 | head -50

# After evaluating a rule, verify log output contains:
# {
#   "event": "rule.evaluated",
#   "ruleId": "rule_xxx",
#   "version": 1,
#   "outcome": "approve",
#   "passed": true,
#   "durationMs": 2.3,
#   "traceId": "tr_xxx",
#   "conditionsChecked": 1,
#   "conditionsPassed": 1
# }

# OBS-020: Debug level
LOG_LEVEL=debug npm start 2>&1 | grep '"level":"debug"' | head -5
# Should see debug entries

# OBS-021: Error level only
LOG_LEVEL=error npm start 2>&1 | grep -v '"level":"error"' | head -5
# Should see nothing (only errors pass through)
```

### 5B. Metrics

```bash
# OBS-018: Prometheus endpoint
curl -s http://localhost:8080/metrics | grep rule_evaluations
# Expected lines:
# rule_evaluations_total{rule_id="rule_xxx",environment="dev"} 5
# rule_evaluations_duration_ms_bucket{le="1",...} 2
# rule_evaluations_duration_ms_bucket{le="5",...} 5
# rule_evaluations_outcome{outcome="approve"} 3
# rule_evaluations_outcome{outcome="reject"} 2

# OBS-008: Cache metrics
curl -s http://localhost:8080/metrics | grep rule_cache
# rule_cache_hits 4
# rule_cache_misses 1
```

### 5C. Distributed Tracing

```bash
# Start with OTLP exporter
OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:4317" npm start &

# Evaluate a rule
curl -s -X POST http://localhost:8080/api/v1/rules/test/evaluate \
  -H "Content-Type: application/json" \
  -d '{"credit_score": 750}'

# Check Jaeger UI at http://localhost:16686
# Verify trace spans:
#   1. HTTP POST /api/v1/rules/:id/evaluate
#   2. auth.verify
#   3. cache.get (compiled rule)
#   4. db.getRule (on cache miss)
#   5. engine.evaluate
#   6. audit.log
```

### 5D. Audit Log Verification

```bash
# Evaluate several times with different inputs
for score in 600 650 700 750 800; do
  curl -s -X POST http://localhost:8080/api/v1/rules/$RULE_ID/evaluate \
    -H "Content-Type: application/json" \
    -d "{\"credit_score\": $score}" > /dev/null
done

# OBS-012/013: Check audit log
curl -s http://localhost:8080/api/v1/rules/$RULE_ID/audit | jq '.[0]'
# Each entry must have:
# - ruleId, ruleVersion, inputData, outcome, passed
# - conditionResults (per-condition breakdown)
# - executionMs, traceId, evaluatedAt

# OBS-014: Verify count
curl -s http://localhost:8080/api/v1/rules/$RULE_ID/audit | jq 'length'
# Expect: 5
```

---

## PART 6: LOAD TESTING

```bash
# Using k6 (install: brew install k6)
cat > load_test.js << 'EOF'
import http from "k6/http";
import { check, sleep } from "k6";

export const options = {
  stages: [
    { duration: "10s", target: 50 },   // Ramp up
    { duration: "30s", target: 200 },   // Sustain
    { duration: "10s", target: 0 },     // Ramp down
  ],
  thresholds: {
    http_req_duration: ["p(99)<100"],    // P99 under 100ms
    http_req_failed: ["rate<0.01"],      // Error rate under 1%
  },
};

const RULE_ID = __ENV.RULE_ID || "rule_xxx";
const BASE = __ENV.BASE_URL || "http://localhost:8080";

export default function () {
  const scores = [500, 600, 650, 700, 720, 750, 800, 850];
  const score = scores[Math.floor(Math.random() * scores.length)];

  const res = http.post(
    `${BASE}/api/v1/rules/${RULE_ID}/evaluate`,
    JSON.stringify({ credit_score: score }),
    { headers: { "Content-Type": "application/json" } }
  );

  check(res, {
    "status 200": (r) => r.status === 200,
    "has outcome": (r) => JSON.parse(r.body).outcome !== undefined,
    "under 50ms": (r) => r.timings.duration < 50,
  });
}
EOF

k6 run --env RULE_ID=$RULE_ID --env BASE_URL=http://localhost:8080 load_test.js
```

**Expected results:**
- P99 latency < 100ms for single condition rules
- P99 latency < 250ms for complex 10+ condition rules
- Zero errors under 200 concurrent users
- Throughput > 1000 evaluations/second on modern hardware

---

## PART 7: EXPORT FLOW VERIFICATION

### 7A. JSON Export Schema Validation

After exporting any rule, validate the JSON against this schema:

```javascript
// Every exported JSON must contain these fields:
const schema = {
  nodes: "array of objects, each with: id (string), type (string), label (string), x (number), y (number), config (object)",
  connections: "array of objects, each with: from (string), to (string)",
  expression: "string — the generated DSL expression",
  exportedAt: "ISO 8601 timestamp string",
};

// Validate:
const exported = JSON.parse(fs.readFileSync("rule_xxx.json"));
console.assert(Array.isArray(exported.nodes), "nodes is array");
console.assert(Array.isArray(exported.connections), "connections is array");
console.assert(typeof exported.expression === "string", "expression is string");
console.assert(typeof exported.exportedAt === "string", "exportedAt is string");

exported.nodes.forEach((n, i) => {
  console.assert(typeof n.id === "string", `node[${i}].id is string`);
  console.assert(typeof n.type === "string", `node[${i}].type is string`);
  console.assert(typeof n.x === "number", `node[${i}].x is number`);
  console.assert(typeof n.y === "number", `node[${i}].y is number`);
  console.assert(typeof n.config === "object", `node[${i}].config is object`);
});

exported.connections.forEach((c, i) => {
  console.assert(typeof c.from === "string", `conn[${i}].from is string`);
  console.assert(typeof c.to === "string", `conn[${i}].to is string`);
  // Verify references exist
  const nodeIds = new Set(exported.nodes.map(n => n.id));
  console.assert(nodeIds.has(c.from), `conn[${i}].from references valid node`);
  console.assert(nodeIds.has(c.to), `conn[${i}].to references valid node`);
});
```

### 7B. Expression DSL Verification

For the complex underwriting rule, the expression must contain:
- `WHEN on_application_submit {` — trigger
- `AND (` — logical operator
- `credit_score BETWEEN 650 AND 850` — between operator
- `annual_income > 50000` — numeric comparison
- `employment_status IN (employed, self_employed)` — in list
- `NOT (` — negation
- `has_bankruptcy EXISTS` — exists check
- `debt_to_income <= 0.43` — decimal comparison
- `=> APPROVE` — outcome

Verify each keyword is present in the generated expression.

---

## PART 8: CROSS-CUTTING CONCERNS

### 8A. No Demo Data Verification

```bash
# Search the entire codebase for hardcoded demo data
grep -ri "acme\|demo\|example\.com\|john\|jane\|test@\|sample\|dummy\|lorem\|ipsum" \
  --include="*.jsx" --include="*.ts" --include="*.js" --include="*.json" \
  src/ frontend/

# Expect: ZERO results (no demo data anywhere)
```

### 8B. Environment Variable Completeness

Verify .env.example contains ALL of these:

```
PORT=8080
NODE_ENV=production
DATABASE_ADAPTER=postgres
DATABASE_URL=postgresql://user:password@localhost:5432/rulemind
REDIS_URL=redis://localhost:6379
AUTH_MODE=jwt
ISSUER_BASE_URL=
CLIENT_ID=
SESSION_SECRET=
JWT_SECRET=
CORS_ORIGINS=http://localhost:3000
RATE_LIMIT_RPM=100
CACHE_TTL=300
LOG_LEVEL=info
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
EVAL_TIMEOUT_MS=5000
MAX_REQUEST_SIZE=1mb
```

### 8C. Graceful Degradation Matrix

| Component Down | Expected Behavior                              | Test Command |
|----------------|------------------------------------------------|--------------|
| Redis          | Cache bypassed, DB-only, slightly slower       | Stop Redis → evaluate |
| Auth service   | 503 on auth endpoints, health degrades         | Stop OIDC provider → login |
| DB             | 503 on all data endpoints, /health returns 503 | Stop DB → GET /health/ready |
| OTLP collector | Tracing silently disabled, app continues       | Stop collector → evaluate |

---

## EXECUTION CHECKLIST

Fill this out as you complete each section:

- [ ] Sheet 1: All 53 operator tests executed and recorded
- [ ] Sheet 2: All 24 logic/topology tests executed
- [ ] Sheet 3: All 51 UI interaction tests executed
- [ ] Sheet 4: All 18 import/export/save tests executed
- [ ] Sheet 5: All 15 test console tests executed
- [ ] Sheet 6: SDK tests (JS + Python + Embedded)
- [ ] Sheet 6: REST API tests (17 endpoints)
- [ ] Sheet 6: Deployment tests (Docker, K8s, Serverless, Bare Metal)
- [ ] Sheet 6: Database adapter tests (Postgres, SQLite, MongoDB, File)
- [ ] Sheet 6: Auth/SSO tests (JWT, API Key, RBAC, No-Auth)
- [ ] Sheet 7: Structured log verification
- [ ] Sheet 7: Prometheus metrics verification
- [ ] Sheet 7: Distributed tracing verification
- [ ] Sheet 7: Audit log verification
- [ ] Sheet 7: Alerting rules verification
- [ ] Load test: P99 < 100ms, error rate < 1%, throughput > 1000 eval/s
- [ ] Export schema validation
- [ ] Expression DSL verification
- [ ] No demo data scan
- [ ] Environment variable completeness
- [ ] Graceful degradation matrix

**Total test cases: 240+**

Update the Execution Summary sheet (Sheet 9) with pass/fail counts after completing all tests.
