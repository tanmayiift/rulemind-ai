# RuleMind AI

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-5b5bd6.svg)](LICENSE)
[![Built on RuleMind](https://img.shields.io/badge/Built%20on-RuleMind-5b5bd6.svg)](https://github.com/tanmayiift/rulemind-ai)

Open-source, source-agnostic decisioning engine. Build rules, scorecards, and policies from any JSON data source — then deploy them through `dev > uat > prod` with full audit trails.

RuleMind accepts JSON payloads from pluggable connectors (bureau, bank, GST, device, KYC, or custom APIs), lets teams author Python variables, click-built rules, scorecards, and multi-step policies — all from a visual dashboard.

**License:** Apache 2.0 — fork it, self-host it, extend it. No approval or payment needed. If you build on RuleMind, please **credit it visibly** ("Built on RuleMind", with a link) — a request, not a condition. The **RuleMind name and logo are trademarks**; the license covers the code, not the brand. See [NOTICE](NOTICE) and the [attribution section](#license-attribution--trademark).

## Features

- **Connectors** — plug in any JSON data source (bureau, bank, device, KYC, custom)
- **Variables** — Python functions that extract and compute values from connector payloads
- **Rules** — visual rule builder with 12 operators, AND/OR/NOT logic, nested groups
- **Scorecards** — point-based risk scoring with weighted attributes and score bands
- **WoE Scoring** — Weight of Evidence scoring with logistic regression coefficients, Information Value, and configurable PDO/target score
- **Weighted Bins** — per-bin weight multipliers, WoE ranges, and coefficient-based scoring
- **Metric Computation** — configurable output metrics (discount %, loan amount, interest rate, custom formulas)
- **Excel Functions** — 230+ Excel-compatible functions (Math, Statistical, Financial, Logical, Text, Lookup, Date, Info, Engineering, Database) available in variable sandbox and formulas
- **Policies** — multi-step workflows chaining variables, rules, scorecards, actions, ML models, and human review gates
- **ML Model Hosting** — upload, store, and execute Python .pkl models as policy steps with prediction and probability outputs
- **CSV Export** — export full configuration as JSON or CSV with section-based layout
- **Import Validation** — import with per-entity validation report highlighting which rules, variables, scorecards are valid vs invalid
- **Environment Promotion** — dev > uat > prod with test-gating before promotion
- **SDKs** — Android (Kotlin), Flutter (Dart), JavaScript, Python — with encrypted edge bundles
- **Audit & Explainability** — full decision traces, rule-level explainability, audit summaries
- **Multi-Tenant** — API key isolation, per-tenant rate limiting, role-based access

## Architecture

```
apps/
  web/                  Next.js 14 dashboard (visual rule builder, scorecard editor, policy designer)
  python-executor/      FastAPI backend (canonical runtime, sandbox, ML model hosting)
  api/                  Legacy Fastify backend (retained, not active)
  worker/               Background job worker

packages/
  rule-engine/          Core rule evaluation engine (TypeScript) + Excel functions + WoE scoring
  shared/               Shared TypeScript types (WoE, metrics, models, import validation)
  sdk-js/               JavaScript SDK
  sdk-python/           Python SDK
  sdk-android/          Android SDK (Kotlin) + sample app
  sdk-flutter/          Flutter SDK (Dart) + sample app
  schemas/              Shared JSON schemas
  widget/               Embeddable widget

e2e/                    Playwright end-to-end tests (43 edge-case + 5 workflow tests)
test/                   TypeScript unit tests (34 tests: engine, SDK, auth, parity)
```

## Quick Start

### Prerequisites

- **Node.js** >= 18 and **pnpm** >= 9
- **Python** >= 3.10 with pip
- (Optional) Docker and Docker Compose for production-like setup

### 1. Clone and Install

```bash
git clone https://github.com/tanmayiift/rulemind-ai.git
cd rulemind-ai
pnpm install
pip install -r apps/python-executor/requirements.txt
```

### 2. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` to set your secrets. For local development, defaults work out of the box:

```bash
AUTH_MODE=none              # Use 'apikey' or 'jwt' in production
DATABASE_URL=sqlite:///.runtime/rulemind_v4.db
```

### 3. Start Development Servers

```bash
pnpm dev
```

This starts:
- **Web dashboard** at http://localhost:3000
- **API** at http://localhost:8080
- **API docs** at http://localhost:8080/docs

The backend auto-seeds a realistic sample tenant on first startup (connectors, ~55 variables, rules, scorecards, and several policies) so every screen has data immediately. The web app authenticates with the dev API key `rm_live_devlocaltenantkey000000000000` — set `NEXT_PUBLIC_RULEMIND_DEV_API_KEY` (or paste it in **Settings** in the dashboard).

## Docker Compose (Production)

For a full production deployment with Postgres and Redis:

```bash
# Set production secrets
export RULEMIND_CONFIG_KEY=$(openssl rand -hex 32)
export RULEMIND_ADMIN_JWT_SECRET=$(openssl rand -hex 32)
export RULEMIND_ADMIN_PASSWORD=$(openssl rand -base64 24)
export POSTGRES_PASSWORD=$(openssl rand -base64 24)

docker compose up -d
```

This runs:
- `web` on port 3000 (Next.js dashboard)
- `api` on port 8080 (FastAPI backend)
- `worker` (background job processor)
- `db` (Postgres 16)
- `redis` (Redis 7)

### Environment Variables

| Variable | Default | Description |
| --- | --- | --- |
| `AUTH_MODE` | `apikey` | `none`, `apikey`, or `jwt` |
| `DATABASE_URL` | `sqlite:///.runtime/rulemind_v3.db` | SQLite or PostgreSQL connection string |
| `REDIS_URL` | `redis://localhost:6379` | Redis for caching and rate limiting |
| `RULEMIND_CONFIG_KEY` | dev default | Encryption key for config at rest |
| `RULEMIND_ADMIN_JWT_SECRET` | dev default | JWT signing secret for admin sessions |
| `RULEMIND_ADMIN_EMAIL` | `admin@rulemind.local` | Default admin login |
| `RULEMIND_ADMIN_PASSWORD` | dev default | Default admin password |
| `CORS_ORIGINS` | `http://localhost:3000` | Comma-separated allowed origins |
| `PYTHON_SANDBOX_TIMEOUT` | `2000` | Variable execution timeout (ms) |
| `PYTHON_SANDBOX_MEMORY` | `128` | Variable execution memory limit (MB) |
| `RATE_LIMIT_RPM` | `100` | Requests per minute per tenant |

See `.env.example` for the full list.

## Operating RuleMind (setup by team)

### The golden path — a live decision in 5 steps
1. **Start it** — `pnpm dev` (local) or `docker compose up -d` (prod).
2. **Get a key** — dev uses `rm_live_devlocaltenantkey000000000000`; prod: create one in the Admin Console (below).
3. **Author** — in the dashboard: add a **Connector**, write **Variables**, build a **Rule** (12 operators), optionally a **Scorecard**, then compose a **Policy** (which can `branch`, call sub-**workflows**, run async provider **actions**, and **monitor**). The **MECE** gate blocks a policy with overlaps/gaps.
4. **Test & promote** — use **Test Console** / **Simulation** (batch), then move the policy through its **Lifecycle** (Draft → In Review → Ready → Live) and **Deploy** dev → uat → prod.
5. **Decide** — `POST /api/v1/decide` (or the SDKs). Inspect every decision in **Decision Explorer** (node-by-node trace + reason codes).

### 👩‍💻 Developers
- `pnpm dev` runs web (`:3000`) + API (`:8080`, docs at `/docs`). Auto-seeded data on first run.
- Auth in dev: `AUTH_MODE=none`, or use the dev API key above. Point the web app at the API with `NEXT_PUBLIC_API_BASE_URL` and `NEXT_PUBLIC_RULEMIND_DEV_API_KEY`.
- Tests: `pnpm test` (TS/vitest), `python -m unittest discover -s apps/python-executor/tests` (Python), `cargo test --no-default-features` in `packages/rulemind-core-rs` (Rust core).
- Fast local decisions: set `FAST_DECIDE=1` + `SANDBOX_MODE=inline` to serve pure-compute policies from the cached-bundle core (the Rust core is used automatically when built via `maturin develop`).

### 🛠️ Platform / Infra
- **Any SQL database** — set `DATABASE_URL` (SQLite for dev, Postgres in compose/prod; MySQL etc. via SQLAlchemy). Run migrations with `alembic upgrade head`.
- **Kubernetes** — a Helm chart ships in `helm/rulemind/` (Deployments for web/api/worker, Service, Ingress, ConfigMap, Secret, migrate Job, HPA + PDB on the api). `helm install rulemind ./helm/rulemind -f my-values.yaml`.
- **Scaling** — the decision path is **stateless**; run N api replicas behind the Ingress with the HPA on RPS/latency. For maximum throughput enable `FAST_DECIDE=1` (cached-bundle core) and, at the edge, the WASM core (`packages/rulemind-core-rs`, `./build-wasm.sh`).
- **Secrets** — set `RULEMIND_CONFIG_KEY` (config-at-rest encryption), `RULEMIND_ADMIN_JWT_SECRET`, `RULEMIND_ADMIN_PASSWORD`, `POSTGRES_PASSWORD` (see the Docker Compose block for `openssl rand` examples). Connector credentials are Fernet-encrypted at rest.
- **Observability** — Prometheus metrics are always at `/metrics`. For traces + dashboards in **one command**, run **`make observability`**: it turns tracing on (sets the OTLP endpoint) and brings up **Grafana** (`:3001`), **Prometheus** (`:9090`), **Tempo**, and the OTel collector with a pre-provisioned dashboard. (Equivalent to `OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317 docker compose --profile observability up`.) See [`observability/README.md`](observability/README.md).

### ⚡ Throughput & scaling (what to expect)
Sizing guidance for buyers — measured on the reference stack, honest about the trade-offs:

| Policy shape | Path | Throughput / node | Notes |
|---|---|---|---|
| Rules-only (lean) | `FAST_DECIDE=1`, Rust core | **thousands of decisions/s** | Cached-bundle serving via the native core; no per-request DB writes on the hot path. |
| Rules + scorecards / decision tables | `FAST_DECIDE=1`, Python core | **high hundreds/s** | Pure-compute, served from the cached bundle. |
| Heavy multi-step (connector I/O, review gates, ML) | full `PolicyExecutor` | **~100/s** (GIL-bound) | Per-step trace + live I/O; scale **horizontally**. |

- **Scale out, not up.** The decision path is **stateless**, so real scale is N api replicas behind the Ingress with the **HPA** on RPS/latency (Helm chart ships it). A single Python process is GIL-bound (~100 TPS for heavy policies); ten pods ≈ ten times that.
- **Async by default.** Decision logging, email, and AI calls run off the request path; the DB pool is tunable (`DB_POOL_SIZE` / `DB_MAX_OVERFLOW`) and SQLite uses WAL — so the request thread isn't blocked on I/O.
- **Fast path & edge.** `FAST_DECIDE=1` serves pure-compute policies from the cached-bundle core; the same core compiles to **WASM** (`packages/rulemind-core-rs`, `./build-wasm.sh`) for edge/on-device evaluation.

### 📊 Business / Risk Ops
- **Admin Console** (`/admin`) — log in with `RULEMIND_ADMIN_EMAIL` / `RULEMIND_ADMIN_PASSWORD` to create **tenants** and generate **API keys** (shown once — see *Authentication & Credentials* below).
- **Lifecycle & governance** — move policies Draft → In Review → Ready → Live; every change is in the **Audit Log**; promotions use maker/checker.
- **Human-in-the-loop** — the **Review Queue** shows cases routed by queue/role with SLA flags; approve/reject with notes.
- **A/B & Champion/Challenger** — run experiments with traffic ramps, guardrails, significance, and one-click promote/rollback.
- **Monitoring** — decision volume, outcome mix, latency, and drift/anomaly signals.

### 🏢 Hosting on your internal browser (enterprise)
The dashboard is a standalone Next.js app that talks to the API over HTTP — host it on any internal URL:
1. Build: `pnpm --filter @rulemind/web build` (or use the `web` image from `docker compose`).
2. Point it at your API: `NEXT_PUBLIC_API_BASE_URL=https://rulemind-api.internal`.
3. Set `CORS_ORIGINS` on the API to your dashboard's origin.
4. Serve it behind your SSO/reverse proxy. Theme tokens (accent/CTA colours, background, surfaces) are centralised as CSS variables (`--rm-*`) for straightforward white-labelling; an admin-only branding panel to edit them without a rebuild is on the roadmap.

## API Overview

### Admin API (`/api/v1/`)

| Endpoint | Methods | Description |
| --- | --- | --- |
| `/api/v1/connectors` | GET, POST, PUT, DELETE | Manage data source connectors |
| `/api/v1/variables` | GET, POST, PUT, DELETE | Manage computed variables |
| `/api/v1/rules` | GET, POST, PUT, DELETE | Manage decision rules |
| `/api/v1/scorecards` | GET, POST, PUT | Manage risk scorecards |
| `/api/v1/policies` | GET, POST, PUT, DELETE | Manage multi-step policies |
| `/api/v1/test/variables` | POST | Test a variable with sample payload |
| `/api/v1/test/rule/{id}` | POST | Test a saved rule |
| `/api/v1/test/policy/{id}` | POST | Execute a policy |
| `/api/v1/decide` | POST | Run a decision with full payload |
| `/api/v1/export` | GET | Export full configuration as JSON |
| `/api/v1/import` | POST | Import configuration from JSON |
| `/api/v1/audit/decisions` | GET | Query decision audit log |
| `/api/v1/audit/promotions` | GET | Query promotion audit log |

### SDK API (`/sdk/v1/`)

Used by mobile and edge SDKs:

| Endpoint | Method | Description |
| --- | --- | --- |
| `/sdk/v1/health` | GET | Health check with latest bundle version |
| `/sdk/v1/bundle` | GET | Fetch encrypted policy bundle for edge execution |
| `/sdk/v1/decide` | POST | Server-side policy evaluation (fallback) |
| `/sdk/v1/executions/sync` | POST | Sync execution telemetry from device |
| `/sdk/v1/executions/{id}` | GET | Fetch execution details |
| `/sdk/v1/executions/{id}/resume` | POST | Resume a paused execution (human review) |
| `/sdk/v1/events` | POST | Batch upload SDK analytics events |
| `/sdk/v1/experience-manifest` | GET | Get admin/studio metadata |

| `/api/v1/excel-functions` | GET | List all 230+ available Excel functions |
| `/api/v1/models` | GET, POST, DELETE | Manage hosted .pkl ML models |
| `/api/v1/models/{id}/predict` | POST | Run prediction on a hosted model |
| `/api/v1/models/{id}/test` | POST | Test model with sample data |
| `/api/v1/import/validate` | POST | Validate import payload without importing |

### Authentication & Credentials

RuleMind supports three authentication modes configured via `AUTH_MODE`:

| Mode | Header | Description |
| --- | --- | --- |
| `none` | — | No authentication (development only) |
| `apikey` | `X-API-Key: rm_live_...` | API key authentication (default) |
| `jwt` | `Authorization: Bearer <token>` | JWT-based authentication |

**API Key Generation:**

API keys are generated via the admin API. Each key follows the format `rm_live_` + 32 random alphanumeric characters.

```bash
# Admin login (returns JWT session cookie)
curl -X POST http://localhost:8080/api/admin/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "admin@rulemind.local", "password": "your-admin-password"}'

# Create a new tenant
curl -X POST http://localhost:8080/api/admin/v1/tenants \
  -H "Content-Type: application/json" \
  -b "rulemind_admin_session=<jwt>" \
  -d '{"name": "My Tenant", "plan": "standard"}'

# Generate API key for tenant (returns plaintext key — save it, shown only once)
curl -X POST http://localhost:8080/api/admin/v1/tenants/{tenant_id}/keys \
  -b "rulemind_admin_session=<jwt>"

# List tenant API keys (masked)
curl http://localhost:8080/api/admin/v1/tenants/{tenant_id}/keys \
  -b "rulemind_admin_session=<jwt>"

# Revoke an API key
curl -X DELETE http://localhost:8080/api/admin/v1/tenants/{tenant_id}/keys/{kid} \
  -b "rulemind_admin_session=<jwt>"
```

**Key Storage & Security:**
- Raw API keys are **never stored** — only a SHA-256 lookup hash and bcrypt verification hash
- Keys are masked in API responses (e.g., `rm_live_****xyz`)
- Each key has a unique `kid` (key ID) for revocation
- Rate limiting: 1,000 RPM (standard), 5,000 RPM (enterprise)
- Key rotation: revoke old key, generate new one — no downtime

**For local development**, a default API key is auto-provisioned:
```bash
RULEMIND_DEV_API_KEY=rm_live_devlocaltenantkey000000000000
```

**Using API keys:**
```bash
curl -H "X-API-Key: rm_live_..." http://localhost:8080/api/v1/connectors
```

**Admin Console** uses JWT session cookies (HttpOnly, 12-hour expiry) via `/api/admin/v1/auth/login`.

## SDKs

### JavaScript / TypeScript

```bash
npm install @rulemind/sdk
```

```typescript
import { RuleMind } from "@rulemind/sdk";

const client = new RuleMind({
  baseUrl: "https://your-rulemind-server.com",
  apiKey: "rm_live_...",
});

const decision = await client.evaluate("policy_id", {
  bureau: { score: 750 },
  bank: { avgBalance: 45000 },
});

console.log(decision.outcome); // "approve" | "reject" | "review"
```

### Python

```bash
pip install rulemind
```

```python
from rulemind import RuleMind

client = RuleMind(
    base_url="https://your-rulemind-server.com",
    api_key="rm_live_...",
)

decision = client.evaluate("policy_id", {
    "bureau": {"score": 750},
    "bank": {"avgBalance": 45000},
})

print(decision.outcome)  # "approve" | "reject" | "review"
```

### Android (Kotlin)

Add the dependency to your `build.gradle.kts`:

```kotlin
implementation("com.rulemind:rulemind-android:4.1.0-beta.1")
```

```kotlin
val config = RuleMindConfig(
    baseUrl = "https://your-rulemind-server.com",
    apiKey = "rm_live_...",
)
RuleMind.initialize(config)

val decision = RuleMind.evaluate(
    policyId = "policy_id",
    payload = mapOf(
        "bureau" to mapOf("score" to 750),
        "bank" to mapOf("avgBalance" to 45000),
    ),
)

println(decision.outcome) // "approve" | "reject" | "review"
```

The Android SDK includes:
- Encrypted edge bundle sync for offline evaluation
- Background sync via WorkManager
- Pending operation queue for callbacks
- Human review pause/resume flow
- Full audit trail and explainability

### Flutter (Dart)

Add to your `pubspec.yaml`:

```yaml
dependencies:
  rulemind: ^4.1.0-beta.1
```

```dart
final config = RuleMindConfig(
  baseUrl: "https://your-rulemind-server.com",
  apiKey: "rm_live_...",
);
await RuleMind.initialize(config);

final decision = await RuleMind.evaluate(
  policyId: "policy_id",
  payload: {
    "bureau": {"score": 750},
    "bank": {"avgBalance": 45000},
  },
);

print(decision.outcome); // "approve" | "reject" | "review"
```

### SDK Sample Apps

Both Android and Flutter include full sample apps with:

- **Demo Mode** — runs locally with hardcoded fixtures, no backend needed
- **Live Mode** — connects to a running backend, full admin console with CRUD for all entities
- **3 sample journeys** — Travel Guard (fraud), Instant Personal Loan (credit), SME Underwriting (risk)
- **Admin Console** — create/edit/delete connectors, variables, rules, scorecards, policies
- **Review/Resume** — human-in-the-loop decision flows
- **Callback Queue** — async operation dispatch and tracking
- **Audit Trail** — full explainability for every decision

To run the Android sample:

```bash
cd packages/sdk-android
./gradlew :sample-app:installDebug
# Launch on emulator or device — switch to Live Mode and point to your backend
```

To run the Flutter sample:

```bash
cd packages/sdk-flutter/example
flutter pub get
flutter run
```

## Rules Engine

### Operators

The rule engine supports 12 operators:

| Operator | Description | Example |
| --- | --- | --- |
| `==` | Equal | `bureau_score == 700` |
| `!=` | Not equal | `status != "blocked"` |
| `>` | Greater than | `income > 50000` |
| `>=` | Greater than or equal | `score >= 700` |
| `<` | Less than | `risk < 0.5` |
| `<=` | Less than or equal | `age <= 65` |
| `between` | Range (inclusive) | `score between [600, 800]` |
| `in` | In set | `state in ["CA", "NY"]` |
| `not_in` | Not in set | `category not_in ["blocked"]` |
| `regex` | Regular expression | `email regex ".*@corp.com"` |
| `exists` | Field exists | `phone exists` |
| `!exists` | Field does not exist | `override !exists` |

### Node Types

Rules are built as trees with these node types:

- **condition** — a single comparison (variable, operator, value)
- **and** — all children must pass
- **or** — any child must pass
- **not** — inverts child result
- **group** — logical grouping
- **approve / review / reject** — terminal outcomes

### Policies

Policies are ordered step sequences:

1. **Variable evaluation** — compute values from source payloads
2. **Rule checks** — evaluate decision trees
3. **Scorecard scoring** — calculate weighted risk scores
4. **Actions** — HTTP callbacks to external systems
5. **Review gates** — pause for human approval
6. **Transforms** — aggregate and transform outputs

Policies support pause/resume: when a review gate is hit, the execution pauses and can be resumed later with an approve/reject decision.

## Excel Functions

The platform includes 230+ Excel-compatible functions available in:
- **Variable sandbox** — use `SUM()`, `IF()`, `PMT()`, etc. directly in Python variable code
- **Scorecard metric formulas** — Excel-style expressions for computed metrics
- **TypeScript engine** — cross-platform parity for edge/SDK evaluation

### Function Categories

| Category | Count | Examples |
| --- | --- | --- |
| Math & Trig | 50+ | `SUM`, `ROUND`, `ABS`, `SQRT`, `MOD`, `POWER`, `CEILING`, `FLOOR` |
| Statistical | 35+ | `AVERAGE`, `MEDIAN`, `STDEV`, `PERCENTILE`, `CORREL`, `FORECAST`, `RANK` |
| Financial | 30+ | `PMT`, `FV`, `PV`, `NPV`, `IRR`, `XIRR`, `RATE`, `NPER`, `IPMT`, `PPMT` |
| Logical | 10+ | `IF`, `AND`, `OR`, `NOT`, `IFS`, `SWITCH`, `IFERROR`, `XOR` |
| Text | 30+ | `CONCATENATE`, `LEFT`, `RIGHT`, `MID`, `TRIM`, `UPPER`, `LOWER`, `FIND` |
| Lookup | 15+ | `VLOOKUP`, `HLOOKUP`, `INDEX`, `MATCH`, `CHOOSE`, `XLOOKUP` |
| Date & Time | 25+ | `DATE`, `TODAY`, `NOW`, `YEAR`, `MONTH`, `DAY`, `DATEDIF`, `NETWORKDAYS` |
| Information | 15+ | `ISBLANK`, `ISNUMBER`, `ISTEXT`, `ISERROR`, `TYPE` |
| Engineering | 13 | `BIN2DEC`, `DEC2HEX`, `HEX2OCT`, `CONVERT` |
| Database | 7 | `DAVERAGE`, `DCOUNT`, `DSUM`, `DMAX`, `DMIN` |

### Usage in Variables

```python
@variable(source="bureau")
def risk_adjusted_score(payload, variables, apis):
    score = payload.get("score", 0)
    return IF(score >= 700, ROUND(score * 1.1, 0), MAX(score - 50, 0))
```

### List Available Functions

```bash
curl http://localhost:8080/api/v1/excel-functions
# Returns: {total_functions: 232, categories: {...}, all_functions: [...]}
```

## Scorecard Scoring Methods

Scorecards support three scoring methods:

### 1. Points-Based (Default)

Traditional additive scoring with optional weight multipliers:

```json
{
  "scoring_method": "points",
  "bins": [
    {"variable_id": "bureau_score", "ranges": [{"min": 700, "max": 900, "points": 50}], "weight": 2.0},
    {"variable_id": "income", "ranges": [{"min": 50000, "max": 999999, "points": 30}], "weight": 1.0}
  ]
}
```

### 2. Weight of Evidence (WoE)

Logistic regression-based scoring with WoE values per bin:

```json
{
  "scoring_method": "woe",
  "intercept": -1.5,
  "pdo": 20,
  "target_score": 600,
  "target_odds": 50,
  "bins": [{
    "variable_id": "bureau_score",
    "coefficient": 1.2,
    "woe_values": [
      {"min": 0, "max": 500, "woe": -0.8, "iv": 0.10, "event_rate": 0.30, "non_event_rate": 0.10},
      {"min": 500, "max": 700, "woe": 0.2, "iv": 0.05, "event_rate": 0.40, "non_event_rate": 0.50},
      {"min": 700, "max": 900, "woe": 0.9, "iv": 0.15, "event_rate": 0.30, "non_event_rate": 0.40}
    ]
  }]
}
```

**WoE Formula:** `score = target_score + (PDO / ln(2)) x (intercept + SUM(coefficient x WoE))`

### 3. Formula-Based

Custom scoring using Excel-style formulas:

```json
{
  "scoring_method": "formula",
  "formula": "factor_total * 2 + IF(factors.bureau_score > 0, 100, 0)"
}
```

### Metric Computation

Attach computed metrics to any scorecard:

```json
{
  "metrics": [
    {"name": "discount_pct", "formula": "IF(score >= 700, 0.15, IF(score >= 600, 0.10, 0.05))", "unit": "%", "category": "financial"},
    {"name": "loan_amount", "formula": "ROUND(score * 500, -3)", "unit": "INR", "category": "financial"},
    {"name": "interest_rate", "formula": "MAX(8, 18 - score / 100)", "unit": "%", "category": "financial"},
    {"name": "risk_tier", "formula": "IF(score >= 750, 1, IF(score >= 600, 2, 3))", "category": "non_financial"}
  ]
}
```

## ML Model Hosting

Upload and execute Python `.pkl` models as part of policy pipelines:

### Upload a Model

```bash
# Base64-encode your pickle file and POST it
curl -X POST http://localhost:8080/api/v1/models \
  -H "Content-Type: application/json" \
  -H "X-API-Key: rm_live_..." \
  -d '{
    "name": "Credit Risk Model v1",
    "model_type": "sklearn",
    "model_blob_base64": "<base64-encoded-pickle>",
    "input_schema": {"features": ["bureau_score", "income", "age"]},
    "description": "Logistic regression credit risk model"
  }'
```

### Run Predictions

```bash
curl -X POST http://localhost:8080/api/v1/models/{model_id}/predict \
  -H "Content-Type: application/json" \
  -d '{"input_data": {"bureau_score": 750, "income": 60000, "age": 35}}'
# Returns: {prediction: 0, probabilities: [0.85, 0.15], model_type: "LogisticRegression"}
```

### Use in Policy Steps

```json
{
  "type": "model",
  "id": "credit_model_step",
  "ref_id": "credit_risk_model_v1",
  "config": {
    "inputMapping": {
      "bureau_score": "$.variables.bureau_score",
      "income": "$.variables.salary"
    },
    "outputVariable": "model_prediction"
  }
}
```

**Security:** Models are deserialized only in the isolated sandbox process pool (never in the main process). Max size: 50MB.

## Export & Import

### Export Formats

```bash
# JSON export (default)
curl http://localhost:8080/api/v1/export?format=json

# CSV export (section-based layout)
curl http://localhost:8080/api/v1/export?format=csv
```

CSV format uses section headers (`## CONNECTORS`, `## VARIABLES`, `## RULES`, `## SCORECARDS`, `## POLICIES`) with pipe-delimited fields.

### Import with Validation

```bash
# Validate before importing (dry run)
curl -X POST http://localhost:8080/api/v1/import/validate \
  -H "Content-Type: application/json" \
  -d @config.json

# Returns per-entity validation report:
# {
#   "rules": [{"id": "r1", "valid": true, "issues": []}, {"id": "r2", "valid": false, "issues": ["Invalid operator: LIKE"]}],
#   "variables": [...],
#   "scorecards": [...],
#   "policies": [...],
#   "summary": {"total": 50, "valid": 48, "invalid": 2}
# }

# Import (includes validation report in response)
curl -X POST http://localhost:8080/api/v1/import \
  -H "Content-Type: application/json" \
  -d @config.json
```

**Validation checks:**
- Rules: operator validity, variable references, outcome nodes present, tree depth limits
- Variables: Python syntax (AST parse), source connector references
- Scorecards: bin variable references, range overlap detection
- Policies: step type validity, rule/scorecard reference existence

## Testing

```bash
# TypeScript unit tests (34 tests across 8 files)
pnpm test

# Python backend tests (224 tests)
cd apps/python-executor && python3 -m unittest discover -s tests -p 'test_*.py'

# Playwright E2E tests — full server stack (43 edge-case + 5 workflow tests)
pnpm test:e2e

# Type checking
pnpm typecheck

# Linting
pnpm lint

# Android instrumented tests
cd packages/sdk-android && ./gradlew :sample-app:connectedDebugAndroidTest
```

## Project Structure

| Path | Description |
| --- | --- |
| `apps/web/` | Next.js 14 dashboard with visual rule builder |
| `apps/python-executor/` | FastAPI backend — all business logic, API, sandbox |
| `apps/python-executor/app/excel_functions.py` | 230+ Excel-compatible functions (Python) |
| `apps/python-executor/app/model_executor.py` | ML model hosting — pickle deserialization and prediction |
| `apps/python-executor/app/sandbox.py` | Isolated variable execution with Excel function builtins |
| `apps/python-executor/app/logic.py` | Scorecard evaluation (points/WoE/formula), CSV export, metric computation |
| `apps/python-executor/app/auth.py` | API key generation, JWT creation, HMAC signing, bcrypt hashing |
| `apps/python-executor/app/middleware.py` | Tenant context, API key validation, rate limiting |
| `packages/rule-engine/` | Core rule evaluation engine (TypeScript) + Excel functions + WoE scoring |
| `packages/shared/` | Shared types — WoE, metrics, models, import validation |
| `packages/sdk-js/` | JavaScript/TypeScript SDK |
| `packages/sdk-python/` | Python SDK |
| `packages/sdk-android/` | Android SDK + sample app |
| `packages/sdk-flutter/` | Flutter SDK + sample app |
| `packages/schemas/` | Shared JSON schemas for rule definitions |
| `e2e/` | Playwright E2E tests (48 tests across 2 spec files) |
| `test/` | TypeScript unit tests (34 tests across 8 files) |
| `docker-compose.yml` | Production deployment with Postgres + Redis |
| `.env.example` | Environment variable template |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development workflow and coding guidelines.

## Security

See [SECURITY.md](SECURITY.md) for vulnerability reporting and security architecture details.

## License, attribution & trademark

**Code:** [Apache License 2.0](LICENSE) — use, modify, self-host, and distribute freely, **no permission or payment required**.

**Please credit (a request, not a condition):** if you build a product or service on RuleMind, credit it visibly — e.g. **"Built on RuleMind"** or **"Powered by RuleMind"**, with a link to this repo. It costs you nothing and helps the project. Retaining the [NOTICE](NOTICE) file in redistributions *is* required by Apache-2.0.

```
Built on RuleMind — https://github.com/tanmayiift/rulemind-ai
```

**Trademark:** the **RuleMind** name and logo are trademarks of the project owner. Apache-2.0 grants rights to the *code*, not the *brand* — don't use the name/logo to imply endorsement or to pass a derivative off as RuleMind. You may always state truthfully that your product is *built on* RuleMind. See [NOTICE](NOTICE).
