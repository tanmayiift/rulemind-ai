# RuleMind AI

Open-source, source-agnostic decisioning engine. Build rules, scorecards, and policies from any JSON data source — then deploy them through `dev > uat > prod` with full audit trails.

RuleMind accepts JSON payloads from pluggable connectors (bureau, bank, GST, device, KYC, or custom APIs), lets teams author Python variables, click-built rules, scorecards, and multi-step policies — all from a visual dashboard.

**License:** Apache 2.0 — fork it, self-host it, extend it. No approval needed.

## Features

- **Connectors** — plug in any JSON data source (bureau, bank, device, KYC, custom)
- **Variables** — Python functions that extract and compute values from connector payloads
- **Rules** — visual rule builder with 12 operators, AND/OR/NOT logic, nested groups
- **Scorecards** — point-based risk scoring with weighted attributes and score bands
- **Policies** — multi-step workflows chaining variables, rules, scorecards, actions, and human review gates
- **Environment Promotion** — dev > uat > prod with test-gating before promotion
- **SDKs** — Android (Kotlin), Flutter (Dart), JavaScript, Python — with encrypted edge bundles
- **Audit & Explainability** — full decision traces, rule-level explainability, audit summaries
- **Multi-Tenant** — API key isolation, per-tenant rate limiting, role-based access
- **Export/Import** — full configuration export as JSON, re-import across environments

## Architecture

```
apps/
  web/                  Next.js 14 dashboard
  python-executor/      FastAPI backend (canonical runtime)
  api/                  Legacy Fastify backend (retained, not active)
  worker/               Background job worker

packages/
  engine/               Core rule evaluation engine (TypeScript)
  sdk-js/               JavaScript SDK
  sdk-python/           Python SDK
  sdk-android/          Android SDK (Kotlin) + sample app
  sdk-flutter/          Flutter SDK (Dart) + sample app
  schemas/              Shared JSON schemas
  widget/               Embeddable widget

e2e/                    Playwright end-to-end tests
test/                   TypeScript unit tests
```

## Quick Start

### Prerequisites

- **Node.js** >= 18 and **pnpm** >= 9
- **Python** >= 3.10 with pip
- (Optional) Docker and Docker Compose for production-like setup

### 1. Clone and Install

```bash
git clone https://github.com/your-org/rulemind-ai.git
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
DATABASE_URL=sqlite:///.runtime/rulemind_v3.db
```

### 3. Start Development Servers

```bash
pnpm dev
```

This starts:
- **Web dashboard** at http://localhost:3000
- **API** at http://localhost:8080
- **API docs** at http://localhost:8080/docs

The backend auto-seeds sample data on first startup: 6 connectors, 15 variables, 2 rules, 1 scorecard, 1 policy.

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

### Authentication

API requests require an `X-API-Key` header (when `AUTH_MODE=apikey`):

```bash
curl -H "X-API-Key: your-api-key" http://localhost:8080/api/v1/connectors
```

Admin console uses JWT-based session auth via `/api/admin/v1/auth/login`.

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

## Testing

```bash
# TypeScript unit tests
pnpm test

# Python backend tests
cd apps/python-executor && python3 -m unittest discover -s tests -p 'test_*.py'

# Type checking
pnpm typecheck

# Linting
pnpm lint

# End-to-end browser tests (starts servers automatically)
pnpm test:e2e

# Android instrumented tests
cd packages/sdk-android && ./gradlew :sample-app:connectedDebugAndroidTest
```

## Project Structure

| Path | Description |
| --- | --- |
| `apps/web/` | Next.js 14 dashboard with visual rule builder |
| `apps/python-executor/` | FastAPI backend — all business logic, API, sandbox |
| `apps/python-executor/app/seed_data.py` | V3 sample data seeded on first startup |
| `packages/engine/` | Core rule evaluation engine (TypeScript) |
| `packages/sdk-js/` | JavaScript/TypeScript SDK |
| `packages/sdk-python/` | Python SDK |
| `packages/sdk-android/` | Android SDK + sample app |
| `packages/sdk-flutter/` | Flutter SDK + sample app |
| `packages/schemas/` | Shared JSON schemas for rule definitions |
| `e2e/` | Playwright E2E test specs |
| `test/` | TypeScript unit test suites |
| `docker-compose.yml` | Production deployment with Postgres + Redis |
| `.env.example` | Environment variable template |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development workflow and coding guidelines.

## Security

See [SECURITY.md](SECURITY.md) for vulnerability reporting and security architecture details.

## License

Apache License 2.0 — see [LICENSE](LICENSE) for full text.

You are free to use, modify, and distribute this software without requiring permission from the original authors.
