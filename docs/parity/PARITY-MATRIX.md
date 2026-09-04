# Parity matrix

One row per feature named in the source changelog, assessed against `rulemind-ai` @ `main`.

**Every claim cites a file and line.** A `PRESENT` or `PARTIAL` row points at the code that
implements it; an `ABSENT` row points at the search that returned nothing. That is what makes
this re-verifiable in six months instead of folklore.

Generated 2026-08-28. **Re-audited 2026-09-04** — every row re-read against `main`. Line numbers
had drifted on most rows; the table below carries the current ones, and `parity.json` carries the
full citation list for each row (the table shows the primary one so it stays readable).

The table and `parity.json` are two views of one assessment and had silently diverged: P08 and
P18 were closed in `parity.json` and left as `partial` here, so the counts disagreed (2/9 versus
4/7) while both files claimed the same audit date. `check_parity.py` read only the JSON, so the
guard could not see the thing it existed to prevent. It now cross-checks the two and resolves
every `file:line` citation, so a drifted line number fails the build rather than reading as
verified.

| state | count |
|---|---|
| ✅ present | 4 |
| 🟡 partial | 7 |
| ❌ absent | 10 |
| ⛔ cut | 1 |

`⛔ cut` means a deliberate decision not to build it — see the row for why.

## The matrix

| id | feature | state | evidence (primary) | gap | phase | size | 5-engine |
|---|---|---|---|---|---|---|---|
| P01 | Datasource management + connection testing | 🟡 partial | `app/routers/authoring.py:96` — the connector `/test` route DOES exist | it never connects: `authoring.py:97-110` echoes stored config and returns `passed:true`; untested | 3 | days | no |
| P02 | p12 / PKCS#12 datasource credentials | ❌ absent | `0 hits for p12/PKCS12/.pfx; app/mtls.py:128 is INBOUND auth, not outbound client certs` | — | 4 | ~1 week | no |
| P03 | Outbound webhooks with event-type filters | 🟡 partial | `app/webhooks.py:30 is inbound-only; ad-hoc alertUrl at app/executor.py:560-576` | no subscription model, no event types, no filters | 4 | ~1 week | no |
| P04 | Workflow step-through debugger | ❌ absent | `only post-hoc traces (ctx.trace) plus a loop-only tracer at app/executor.py:794` | — | 5 | ~3 weeks | no |
| P05 | Modelset | ❌ absent | `0 hits for modelset/model_set; models are flat .pkl files in app/routers/models.py` | — | 5 | ~2 weeks | no |
| P06 | Nested-key input validation | 🟡 partial | `app/model_executor.py:111 validates a flat list; app/jsonpath.py:14 composed only at app/executor.py:1376-1383` | not composed on the direct predict path; `INVALID_INPUT` names the flat target, not the path | 3 | days | no |
| P07 | Matrix with dimensions + import | ❌ absent | `'matrix' is only a QA harness (apps/python-executor/simulation/scenario_matrix.py:1) and a backtest transition_matrix; closest analog is app/decision_tables.py:92` | — | 6 | ~4 weeks | **yes** |
| P08 | Child workflow by alias | ✅ present | `app/executor.py:777-792` — `_execute_subworkflow` resolves `config.alias` through `engine_config.workflow_aliases`, flat or environment-keyed by the parent's status | — | 3 | done | no |
| P09 | DLQ / Kafka / exactly-once | ❌ absent | `0 code hits for kafka/dlq/dead-letter; messaging is Redis pub/sub in app/decision_bus.py` | — | 8 | 6+ months | no |
| P10 | ClickHouse / OLAP store | 🟡 partial | `app/archiver.py:69 ClickHouseArchiver, driven by app/scheduler.py:71-95, scheduled at :409-411` | **no test touches the ClickHouse sink** — `tests/test_decision_archiver.py` uses `MemoryArchiver` only; also stores decisions, not predictor values | 4 | days to cover the sink | no |
| P11 | Canary deployment with delays | ❌ absent | `0 code hits for canary; app/champion_challenger.py:40 already has a day-indexed ramp` | — | 5 | 2-3 weeks | no |
| P12 | Feature factory | ❌ absent | `0 hits in any .py/.ts/.tsx; app/backtest.py and app/analytics.py are the seeds` | — | 8 | multi-month | **yes** |
| P13 | Monaco / embedded code editor | ❌ absent | `0 hits for monaco/codemirror/ace; authoring is plain <textarea>, e.g. apps/web/app/api-console/page.tsx:275` | — | 6 | ~2 days syntax highlighting, ~2 weeks completion | no |
| P14 | SQL editor | ⛔ cut | `no SQL datasource exists to edit against; 0 hits for postgres/mysql/jdbc/snowflake in connector code` | — | — | n/a | no |
| P15 | Journey as a first-class entity | ❌ absent | `no Journey model or table; only fixtures in app/experience_studio.py:292, demo-app/lib/journeys.dart` | — | 7 | 3-6 months | **yes** |
| P16 | Partitioning / cold storage / retention in months | 🟡 partial | `audit_retention_days default 90 at app/models.py:576, settable at app/routers/governance.py:37,67-68; S3/Parquet sink at app/archiver.py:118,143-151` | days not months; 0 partitioning hits | 3 | days | no |
| P17 | Excel functions (CUMIPMT, DAYS360, EDATE, EOMONTH) | 🟡 partial | `app/excel_functions.py:800,1306,1320,1335; EDATE/EOMONTH tested at tests/test_excel_functions.py:609,617 and in the TS twin at packages/rule-engine/src/excel-functions.ts:633,639` | **CUMIPMT and DAYS360 are Python-only and untested** — absent from the TS twin, absent from every conformance suite | 3 | days | **yes** |
| P18 | Rate limiting on resource APIs | ✅ present | `app/rate_limit.py` — `classify()` maps method+path to decision/read/write/ai/export/admin with whole-segment matching; `check_request()` enforces the class bucket then the tenant aggregate | class shares are per tenant/env, not per individual route | 3 | done | no |
| P19 | Superflows | ❌ absent | `0 hits repo-wide` | — | 7 | 3-6 months | no |
| P20 | Predictor entity + values + reports | 🟡 partial | `'predictor' is only a synonym for scorecard at app/ai.py:511,523-532, routed at app/routers/ai.py:239-258` | no table, no values store, no reports | 4 | ~3 weeks | no |
| P21 | Evaluation timeouts + 504 handling | ✅ present | `EvaluationBudgetExceeded at app/executor.py:159, enforced at :449,486,502,866,958-987; 504 handler at app/main.py:105-121; tested at tests/test_evaluation_budget.py:71` | covers the `PolicyExecutor` path only — `app/fast_decide.py` (opt-in `FAST_DECIDE=1`) and `app/routers/evaluations.py:70,87,106` sit outside it | 3 | days for the two remaining paths | no |
| P22 | Node-based workflow builder (branch / loop) | ✅ present | `apps/web/app/workflow-builder/page.tsx:31-45 PALETTE (13) matches the 13 types dispatched at app/executor.py:523-553; guarded by apps/web/test/workflow-builder-palette.test.ts` | a nested step list, not a free-form graph; no 'rule set' node | 7 | 3-6 months for the graph canvas | no |

## Reading the columns

**evidence** — the file and line that settles the claim. Written so a reader can check it
rather than trust it. `parity.json` is the authority; this column is trimmed to stay legible.

**5-engine** — whether the feature touches the operator-semantics contract. This is the
decisive cost multiplier: `CLAUDE.md` names five conformance suites that must move together
(Python, TypeScript, Rust, Kotlin, Dart), so a feature marked **yes** costs roughly 5× a
backend-only one. Tag this on the way in, not after estimating.

**phase** — the wave that owns it, from `ROADMAP.md`. A machine check asserts every
`ABSENT`/`PARTIAL` row has both a phase and a named E2E flow in `parity.json`, so an
unowned gap fails rather than drifting. That check is `docs/parity/check_parity.py`, and as of
2026-09-04 it actually runs — the `contracts` job in `.github/workflows/sdk-verify.yml`. Before
that it was a script nothing invoked, which is its own small parable about this table.

## What the 2026-09-04 re-audit changed

- **P21 promoted to ✅ present.** The evaluation budget shipped: `EvaluationBudgetExceeded`
  (`app/executor.py:159`), a real deadline checked in both the variable and step loops, and an
  app-level handler returning HTTP 504 (`app/main.py:114`) with a documented body. The old gap
  "no 504 anywhere" is now false. Two paths remain outside it and are recorded in the row.
- **P10 demoted to 🟡 partial.** The code is real; the *test* is not. `tests/test_decision_archiver.py`
  exercises `MemoryArchiver` and says so in its own docstring, so the row's e2eFlow —
  "ClickHouse round-trip verified" — is verified by nothing.
- **P17 demoted to 🟡 partial.** Checked function by function this time: `CUMIPMT` and `DAYS360`
  exist only in Python, have no test anywhere, and are missing from the TypeScript twin. The
  previous evidence ("~230 functions; TS twin at …") was true of the library and false of two of
  the four functions the row names.
- **P22's palette bug is fixed.** `decision_table` is in the palette
  (`apps/web/app/workflow-builder/page.tsx:34`) and `apps/web/test/workflow-builder-palette.test.ts`
  now parses both sources so the palette cannot silently fall behind the executor again.
- **P01's gap was simply wrong.** It said "no /test for connectors". The route has always existed
  (`app/routers/authoring.py:96`); what it does not do is connect to anything.
- **P04's "untested" was wrong.** `tests/test_sdk_runtime.py:177,232,333` cover the execution and
  resume endpoints. The row stays ❌ absent — a loop tracer is not a step-through debugger.
- **Line numbers drifted on P03, P06, P08, P13, P16, P20 and P22** and are refreshed.

## Notes worth carrying forward

- **P14 (SQL editor) is cut on purpose.** There is no SQL datasource in this product to edit
  against. Building an editor to match a changelog line would be cargo-culting.
- **P19 (superflows) may not be a capability gap at all** — it reads like a naming artifact
  of the other platform's model. It needs a product definition before any engineering.
- **P09 (Kafka/exactly-once) has a much cheaper 80% answer.** Phase 4 ships a DLQ plus an
  `Idempotency-Key` convention on the existing Redis bus and WAL. Real exactly-once is a
  multi-month distributed-systems commitment with permanent operational cost; it should be
  built against a paying requirement, not left on a roadmap as a checkbox.
- **Two rows were scored on implementing code alone and both were wrong** (P10, P17). The bar
  this table now holds is: `PRESENT` requires code that implements the claim *and* a test that
  covers the claim — not a test of something nearby. Both demotions came from applying it.
- **P18 did not move despite real work landing.** Pre-auth IP rate limiting and login lockout
  shipped in 2026-09 and are genuinely valuable, but this row's gap is *per-resource-class*
  scoping, and the new buckets key on URL surface and client IP. Adjacent work is not the work.
