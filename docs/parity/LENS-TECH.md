# Tech lens

What each gap costs architecturally, and which are cheap because the substrate is already here.

## The cost multiplier that decides everything

`CLAUDE.md` names five conformance suites that must move together — Python, TypeScript, Rust,
Kotlin, Dart. A feature touching operator semantics costs roughly **5×** one that touches only
the backend, because the same policy must evaluate identically in all five engines and the
parity tests enforce it.

Four features carry that multiplier: **P07** (matrix), **P12** (feature factory), **P15**
(journey entity), **P17** (Excel functions, already done). Everything else is backend-only.

Tag this on the way in. Discovering it during estimation is how a two-week item becomes a
quarter.

## Three tiers of cost

### Tier 1 — the substrate exists, only the join is missing (days)

| feature | what already exists | what is missing |
|---|---|---|
| Connector test (P01) | `test_connection` for AI providers at `app/ai.py:336` | the same endpoint for connectors |
| Sub-workflow alias (P08) | resolution + depth guard + cycle detection at `app/executor.py:462,690-702` | an alias → id lookup |
| Nested-key validation (P06) | flat validation in `app/model_executor.py`; dotted paths in `app/jsonpath.py` | composing the two |
| Retention in months (P16) | `audit_retention_days` + archive-then-purge scheduler | a unit change and UI |
| Timeouts / 504 (P21) | timeouts at sandbox, HTTP action and AI layers | `routers/evaluations.py` has none; no 504 contract |
| Per-resource limits (P18) | Redis token bucket with tenant → plan → global resolution | a resource dimension on the key |
| `decision_table` palette (P22) | the executor dispatches it | the UI never lists it — a bug |

### Tier 2 — new entities on existing rails (1–4 weeks)

- **Predictor (P20).** Needs a table, CRUD, a values store and reports. Cheap*er* than it
  looks because `app/archiver.py` already ships decisions to ClickHouse — predictor values
  extend a working pipeline rather than building one.
- **Modelset (P05).** `HostedModel`, `model_evaluation.py` and `champion_challenger.py` all
  exist. A modelset is a versioned collection plus a routing rule.
- **Canary (P11).** `app/release.py`, `app/shadow.py`, `app/experiments.py` and
  `app/champion_challenger.py` already do traffic routing, and the last already has a
  day-indexed ramp. Canary is a percentage split plus an auto-rollback trigger — **medium,
  not large**.
- **Step-through debugger (P04).** Closer than it looks: `/executions/{id}` and
  `/executions/{id}/resume` **already exist**. What is missing is per-step snapshots and a UI.
- **Outbound webhooks (P03).** `app/decision_bus.py` is the natural event source; the missing
  piece is a subscription model with event types and filters.
- **p12 credentials (P02).** `app/mtls.py` has TLS plumbing but for *inbound* auth. Outbound
  client certs are separate work, though secure storage already exists via the
  `RULEMIND_CONFIG_KEY` Fernet path used for AI keys.

### Tier 3 — new subsystems (months)

- **Matrix (P07).** `app/decision_tables.py` is the right substrate and `app/mece.py` gives
  gap/overlap validation for free. The XLSX importer is where the effort really sits. Carries
  the 5× multiplier.
- **Monaco (P13).** Splits three ways: highlighting ~2 days, RuleMind-aware completion
  ~2 weeks, SQL editor cut. Must be dynamically imported — Next.js bundle and SSR cost.
- **Graph canvas (P22).** The builder is a **nested step list, not a graph**: no coordinates,
  no edges. A free-form DAG changes the executor's execution model at
  `app/executor.py:437-466`, rewrites the builder page, and migrates every stored workflow.
  This is a re-architecture.
- **Journey entity (P15).** The largest single item: a table, CRUD, a web route **and** an SDK
  surface across five engines.
- **Kafka / exactly-once (P09).** Today: Redis pub/sub with a DB-polling fallback, plus
  `app/decision_wal.py`. Real exactly-once is a multi-month commitment with permanent
  operational cost.
- **Feature factory (P12).** `app/backtest.py` and `app/analytics.py` are the seeds. Carries
  the 5× multiplier.

## Sequencing constraints

- **Phase 7 must not start before 3–6 are green.** Starting the graph migration early is how
  you end up with a half-migrated executor and a red parity suite.
- **Phase 5 pays the test debt.** `/deploy/status`, `/deploy/promote`,
  `/policies/{id}/whatif|shadow|rollback|releases` and `/executions/{id}[/resume]` are all at
  zero coverage today, and the canary flow exercises every one of them. Cover them there
  rather than as a separate chore nobody schedules.
- **Track B is not optional and not sequential.** B0–B2 (real linting, wiring the unrun
  suites into CI, security scanning) gate the credibility of every test claim in this
  document, and should land alongside Phase 3 rather than after Phase 8.
