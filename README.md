RuleMind

RuleMind is an open-source, source-agnostic decisioning engine. It accepts JSON payloads from pluggable connectors such as bureau, bank statements, GST, device, KYC, or custom APIs, then lets teams build Python variables, click-built rules, scorecards, and policies that promote through `dev -> uat -> prod`.

Product Surface

- Dashboard
- Connectors
- Variables
- Rules
- Scorecards
- Policies
- Test Console
- Deploy
- Exports
- Settings

## Runtime

- Frontend: Next.js 14 in [apps/web](/Users/tanmaykumar/Downloads/RuleMind.AI/apps/web)
- Canonical backend: FastAPI in [apps/python-executor](/Users/tanmaykumar/Downloads/RuleMind.AI/apps/python-executor)
- Legacy backend: Fastify in [apps/api](/Users/tanmaykumar/Downloads/RuleMind.AI/apps/api) is retained in the repo but is no longer the runtime source of truth
- Storage: SQLite by default for local V3 development
- Optional infra: Redis, Postgres, MinIO, OpenTelemetry collector

## Seeded V3 Data

The backend seeds the V3 sample configuration on first startup:

- 6 connectors: Bureau, Bank Statement, GST, Device, KYC, Custom
- 15 variables across 5 active sources
- 2 rules
- 1 scorecard
- 1 policy

The seed definitions live in [apps/python-executor/app/seed_data.py](/Users/tanmaykumar/Downloads/RuleMind.AI/apps/python-executor/app/seed_data.py).

## Local Development

1. Install dependencies:

```bash
pnpm install
pip install -r apps/python-executor/requirements.txt
```

2. Start optional infra:

```bash
docker compose up -d postgres redis minio otel-collector
```

3. Initialize the local SQLite database:

```bash
pnpm migrate
```

4. Start the frontend and FastAPI backend:

```bash
pnpm dev
```

5. Open:

- Web: [http://localhost:3000](http://localhost:3000)
- API docs: [http://localhost:8080/docs](http://localhost:8080/docs)
- Health: [http://localhost:8080/health](http://localhost:8080/health)

## Docker Compose

The development compose file runs:

- `web` on `3000`
- `api` on `8080` using FastAPI
- `worker` on the Node worker runtime
- optional infra services: Postgres, Redis, MinIO, and OTEL collector

Run:

```bash
docker compose up -d
```

## Useful Commands

```bash
pnpm typecheck
pnpm lint
pnpm test
pnpm test:e2e
pnpm openapi
```

`pnpm openapi` writes the FastAPI OpenAPI document to [apps/python-executor/openapi.json](/Users/tanmaykumar/Downloads/RuleMind.AI/apps/python-executor/openapi.json).

## API Overview

The V3 API is served under `/api/v1` and includes:

- `GET /api/v1/connectors`
- `GET|POST|PUT|DELETE /api/v1/variables`
- `GET|POST|PUT|DELETE /api/v1/rules`
- `GET|POST|PUT /api/v1/scorecards`
- `GET|POST|PUT /api/v1/policies`
- `POST /api/v1/test/variables`
- `POST /api/v1/test/rule/{id}`
- `POST /api/v1/test/policy/{id}`
- `POST /api/v1/decide`
- `GET /api/v1/deploy/status`
- `GET /api/v1/export`
- `POST /api/v1/import`
- `GET /api/v1/audit/decisions`
- `GET /api/v1/audit/promotions`

## Notes

- Theme defaults to light mode and is persisted in local storage.
- Variables execute in a restricted Python sandbox with import and timeout limits.
- Connector metadata is source-specific, but the engine itself remains generic JSON decisioning.
