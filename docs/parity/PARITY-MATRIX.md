# Parity matrix

One row per feature named in the source changelog, assessed against `rulemind-ai` @ `main`.

**Every claim cites a file and line.** A `PRESENT` or `PARTIAL` row points at the code that
implements it; an `ABSENT` row points at the search that returned nothing. That is what makes
this re-verifiable in six months instead of folklore.

| state | count |
|---|---|
| ✅ present | 3 |
| 🟡 partial | 8 |
| ❌ absent | 10 |
| ⛔ cut | 1 |

`⛔ cut` means a deliberate decision not to build it — see the row for why.

## The matrix

| id | feature | state | evidence | gap | phase | size | 5-engine |
|---|---|---|---|---|---|---|---|
| P01 | Datasource management + connection testing | 🟡 partial | `app/routers/authoring.py (connectors CRUD); connection-test exists only for AI providers at app/ai.py:336` | no /test for connectors | 3 | days | no |
| P02 | p12 / PKCS#12 datasource credentials | ❌ absent | `0 hits repo-wide for p12/PKCS12; app/mtls.py is INBOUND auth, not outbound client certs` | — | 4 | ~1 week | no |
| P03 | Outbound webhooks with event-type filters | 🟡 partial | `app/webhooks.py is inbound-only; ad-hoc alertUrl at app/executor.py:468-490` | no subscription model, no event types, no filters | 4 | ~1 week | no |
| P04 | Workflow step-through debugger | ❌ absent | `only post-hoc traces (ctx.trace); /executions/{id} and /executions/{id}/resume already exist but are untested` | — | 5 | ~3 weeks | no |
| P05 | Modelset | ❌ absent | `0 hits for modelset/model_set; models are flat .pkl files in app/routers/models.py` | — | 5 | ~2 weeks | no |
| P06 | Nested-key input validation | 🟡 partial | `app/model_executor.py validates a flat feature list; app/jsonpath.py already resolves dotted paths` | the two are not composed | 3 | days | no |
| P07 | Matrix with dimensions + import | ❌ absent | `'matrix' appears only as a QA harness (simulation/scenario_matrix.py) and a backtest transition_matrix; closest product analog is app/decision_tables.py` | — | 6 | ~4 weeks | **yes** |
| P08 | Child workflow by alias | 🟡 partial | `sub-workflows exist at app/executor.py:462,690-702 with depth + cycle guards` | referenced by ref_id, not alias | 3 | days | no |
| P09 | DLQ / Kafka / exactly-once | ❌ absent | `0 hits for kafka/dlq/dead-letter; messaging is Redis pub/sub in app/decision_bus.py with a DB-polling fallback` | — | 8 | 6+ months | no |
| P10 | ClickHouse / OLAP store | ✅ present | `app/archiver.py ClickHouseArchiver:69, driven by app/scheduler.py:72-88, tested in tests/test_decision_archiver.py` | stores archived decisions, not predictor values | 4 | n/a | no |
| P11 | Canary deployment with delays | ❌ absent | `0 hits for canary; app/champion_challenger.py already has a day-indexed traffic ramp` | — | 5 | 2-3 weeks | no |
| P12 | Feature factory | ❌ absent | `0 hits; app/backtest.py and app/analytics.py are the seeds` | — | 8 | multi-month | **yes** |
| P13 | Monaco / embedded code editor | ❌ absent | `0 hits for monaco/codemirror/ace; all authoring is plain <textarea>, e.g. app/workflow-builder/page.tsx:472` | — | 6 | ~2 days syntax highlighting, ~2 weeks completion | no |
| P14 | SQL editor | ⛔ cut | `no SQL datasource exists to edit against` | — | — | n/a | no |
| P15 | Journey as a first-class entity | ❌ absent | `no Journey model or table; only fixtures in app/experience_studio.py:292, demo-app/lib/journeys.dart, qa/e2e-full/journey_expectations.json` | — | 7 | 3-6 months | **yes** |
| P16 | Partitioning / cold storage / retention in months | 🟡 partial | `audit_retention_days default 90 at app/models.py:458; S3/Parquet sink in app/archiver.py` | days not months; no table partitioning | 3 | days | no |
| P17 | Excel functions (CUMIPMT, DAYS360, EDATE, EOMONTH) | ✅ present | `app/excel_functions.py:791,1297,1311,1326; ~230 functions; TS twin at packages/rule-engine/src/excel-functions.ts` | — | — | n/a | **yes** |
| P18 | Rate limiting on resource APIs | 🟡 partial | `app/rate_limit.py Redis token bucket, per-tenant -> per-plan -> global` | not scoped per resource class | 3 | days | no |
| P19 | Superflows | ❌ absent | `0 hits repo-wide` | — | 7 | 3-6 months | no |
| P20 | Predictor entity + values + reports | 🟡 partial | `'predictor' is only a synonym for scorecard at app/ai.py:475-497` | no table, no values store, no reports | 4 | ~3 weeks | no |
| P21 | Evaluation timeouts + 504 handling | 🟡 partial | `timeouts exist at sandbox (app/sandbox.py:224-242), HTTP action and AI layers; app/routers/evaluations.py is fully synchronous with none` | no 504 anywhere | 3 | days | no |
| P22 | Node-based workflow builder (branch / loop) | ✅ present | `app/workflow-builder/page.tsx PALETTE; app/executor.py:437-466 dispatches 13 step types; BranchEditor and LoopEditor exist` | a nested step list, not a free-form graph; no 'rule set' node; decision_table is dispatched but MISSING from the palette | 7 | 3-6 months for the graph canvas; the palette gap is hours | no |

## Reading the columns

**evidence** — the file and line that settles the claim. Written so a reader can check it
rather than trust it.

**5-engine** — whether the feature touches the operator-semantics contract. This is the
decisive cost multiplier: `CLAUDE.md` names five conformance suites that must move together
(Python, TypeScript, Rust, Kotlin, Dart), so a feature marked **yes** costs roughly 5× a
backend-only one. Tag this on the way in, not after estimating.

**phase** — the wave that owns it, from `ROADMAP.md`. A machine check asserts every
`ABSENT`/`PARTIAL` row has both a phase and a named E2E flow in `parity.json`, so an
unowned gap fails rather than drifting.

## Notes worth carrying forward

- **P22 hides a shipping bug, not just a gap.** `app/executor.py` dispatches 13 step types
  but the builder palette exposes 12 — `decision_table` is implemented and unreachable from
  the UI. Hours to fix, and unrelated to the graph-canvas work in the same row.
- **P14 (SQL editor) is cut on purpose.** There is no SQL datasource in this product to edit
  against. Building an editor to match a changelog line would be cargo-culting.
- **P19 (superflows) may not be a capability gap at all** — it reads like a naming artifact
  of the other platform's model. It needs a product definition before any engineering.
- **P09 (Kafka/exactly-once) has a much cheaper 80% answer.** Phase 4 ships a DLQ plus an
  `Idempotency-Key` convention on the existing Redis bus and WAL. Real exactly-once is a
  multi-month distributed-systems commitment with permanent operational cost; it should be
  built against a paying requirement, not left on a roadmap as a checkbox.
- **The three `PRESENT` rows are genuinely done**, not generously scored. The Excel row was
  checked function by function: `CUMIPMT`, `DAYS360`, `EDATE` and `EOMONTH` all exist, inside
  a ~230-function library with a parallel TypeScript implementation for the edge engine.

