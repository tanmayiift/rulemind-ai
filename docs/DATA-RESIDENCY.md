# Data residency and data-subject rights

For procurement and privacy reviews of a self-hosted RuleMind install. Everything here can be checked
against the running install: `GET /api/v1/data-residency` reports it from live configuration.

## Where the data is

RuleMind is self-hosted. **Every workspace on an install stores its data in that install's database,
in the place you run it.** There is no RuleMind-operated service in the data path, no per-region
routing and no shared multi-tenant cloud. To keep EU and Indian data apart, run two installs.

Declare where an install runs with `RULEMIND_DATA_REGION` (for example `in-mumbai-1` or
`eu-frankfurt`). It is reported as declared, never inferred, so it is only as true as the operator
makes it.

| Store | What it holds | Where |
|---|---|---|
| Database (`DATABASE_URL`) | Workspaces, policies, decisions, audit trail, reviews | Your PostgreSQL |
| Decision WAL | Decisions made while the database was unreachable, until replayed | Local disk on each API host |
| Decision archive (`DECISION_ARCHIVE_SINK`) | Decisions past the retention window | Your object storage, if configured |
| Backups | Whatever your backup job copies | Wherever you send them |

Decisions and the audit log can be encrypted at rest: `DECISION_ENCRYPT_AT_REST` and
`AUDIT_ENCRYPT_AT_REST`. Connector credentials are always encrypted at rest.

## How workspaces on one install are kept apart

On PostgreSQL the database enforces it, not only the application. Every table that holds a
workspace's data has **row-level security** (ENABLE + FORCE) and one policy: a transaction
acting for a workspace sees only that workspace's rows and cannot write anyone else's, whatever
query the application sends. Every authenticated request runs that way. Work that is not on
behalf of one workspace — startup, migrations, the scheduler, the platform-admin console, and
the sign-in lookups that establish which workspace a caller belongs to — runs in system scope.

PostgreSQL never applies row security to a superuser or a `BYPASSRLS` role, and the database
user of a first install usually is one. So workspace transactions switch to `rulemind_tenant`, a
`NOLOGIN NOSUPERUSER NOBYPASSRLS` role the migration creates. That needs the migrating user to
hold `CREATEROLE`; without it the API logs an error at startup naming what is missing, and
isolation rests on the application's own checks alone. `GET /api/v1/data-residency` reports
which of the two is in force on the running install.

SQLite, for local development, has no row security.

## Where it can go

Data leaves the install only to destinations you configure. The residency report lists each one it
finds in the live configuration:

| Destination | Configured by | Sends |
|---|---|---|
| Connector providers (bureaus, bank data, KYC) | Connectors with `fetch_at_decision` | Decision inputs, per the request mapping |
| Connector token endpoints | OAuth2 connectors' `token_url` | Client credentials only |
| Webhook subscriptions | Settings → Webhooks | Decision id, outcome, subject id. Never the payload |
| AI providers (Anthropic, OpenAI, Gemini) | A provider key in AI settings | Prompts built from what an author asks about, which can include decision data |
| Kafka | `KAFKA_BOOTSTRAP_SERVERS` | Decision events |
| Decision archive | `DECISION_ARCHIVE_SINK` | Full decisions past retention |
| Tracing | `OTEL_EXPORTER_OTLP_ENDPOINT` | Request spans; payloads are not attached |

If a destination is outside your permitted region, the report is where you will see it.

## Data-subject rights

The subject is the `userId` sent with a decision (`user_id` on the stored decision).

**Access.** `GET /api/v1/data-subjects/{subject_id}` returns every decision about the subject, in
full, plus any paused executions that name them. Admin only.

**Erasure.** `POST /api/v1/data-subjects/{subject_id}/erase` with `{"reason": "..."}`. Admin only.

A lender usually must not delete a credit decision outright, because adverse-action records carry
their own retention duties (for example ECOA/Reg B, or RBI record-keeping). So erasure **redacts the
personal data and keeps the record**:

| Kept | Removed |
|---|---|
| Decision id, outcome, policy, policy version, bundle, timestamps | The payload |
| Adverse-action reason codes: code, reason, variable, threshold | Computed variables, the trace, scorecard detail |
| A keyed pseudonym in place of the subject id | The applicant's own values, including inside the reason-code text |

It also redacts the subject's paused executions and their review tasks, where it keeps the reviewer's
verdict. It pseudonymises the subject id in queued webhook deliveries, redacts queued Kafka outbox
events, deletes cached idempotent responses, and redacts linked error-event details. All of this
happens in **one transaction**. The erasure is written to the tamper-evident audit trail **under the
pseudonym**, because an audit log that names the erased person would undo the erasure. Erasing twice
is safe.

Whether a request must be honoured, or is overridden by a retention obligation, is your legal call.

**What erasure cannot reach**, listed on every receipt:

- archived decisions already in object storage: delete those objects with the storage provider;
- database backups, which age out on your backup schedule;
- events already delivered to Kafka consumers and webhook receivers;
- a decision still waiting in the WAL after a database outage;
- data already sent to connector providers or AI providers.
