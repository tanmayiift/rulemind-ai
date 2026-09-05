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
| ✅ present | 19 |
| 🟡 partial | 1 |
| ❌ absent | 1 |
| ⛔ cut | 1 |

`⛔ cut` means a deliberate decision not to build it — see the row for why.

## The matrix

| id | feature | state | evidence (primary) | gap | phase | size | 5-engine |
|---|---|---|---|---|---|---|---|
| P01 | Datasource management + connection testing | ✅ present | `app/connector_fetch.py` fetches a source at decision time (opt-in per connector); `request_mapping` composes the call via `templates.resolve_template`, `response_mapping` reshapes the reply via `jsonpath.resolve_jsonpath` — neither had a backend reader before; egress-guarded and pinned, tenant+connector-scoped cache, timeouts AND name resolution clamped to the evaluation budget; fails **closed** (default `review_gate`, never sample_payload); `tests/test_connector_fetch.py` (46) + `..._refuted.py` (29) | the fast path declines fetch-configured tenants rather than implementing a second transport | 4 | done | no |
| P02 | p12 / PKCS#12 datasource credentials | ✅ present | `app/client_certs.py` unwraps a PKCS#12 bundle from the connector config and presents it on outbound calls via `connector_fetch`; chain included; server verification unchanged (`create_default_context`, asserted); contexts cached per (tenant, connector, bundle fingerprint) so rotation takes effect without a restart; the decrypted PEM touches only a 0600 temp file unlinked in a `finally`; `tests/test_client_certs.py` (23) | configured through connector config rather than a dedicated upload endpoint, and no expiry warning before the handshake fails | 4 | done | no |
| P03 | Outbound webhooks with event-type filters | ✅ present | `webhook_subscriptions` table + `/api/v1/webhook-subscriptions` CRUD and `/test`; 13 closed event types (typo refused at authoring, not stored inert); ANDed filters through `logic.compare`, and a filter that cannot be evaluated does NOT deliver; HMAC-signed with the same helper `webhooks.py` verifies inbound, over the exact bytes sent; egress-guarded, `follow_redirects=False`; `manage_access`, not `author`; 55 tests + probed in the isolation matrix | no durable outbox — a subscriber down for the whole retry window misses that event (the decision is unaffected) | 4 | done | no |
| P04 | Workflow step-through debugger | ✅ present | session-based stepping at `/api/v1/debug/sessions` (`app/routers/debugger.py`); the whole feature is ONE executor field — `PolicyExecutor.debug_stop_before`, checked at depth 0 in `_run_steps` — so it pauses via the same `ctx.status='paused'` + `paused_at_step` the executor already uses for review gates and resumes through the ordinary `execute(resume_from=…)`, which is why a stepped run reaches the same outcome as production; every step is `simulate=True`, so no decision row, no billed unit, no outbound action; `tests/test_debugger.py` (24) | sessions are in-process, so they do not survive a restart or a second replica; stepping is depth-0 only — a loop or sub-workflow runs to completion inside one step | 5 | done | no |
| P05 | Modelset | ✅ present | `model_sets` table + `app/modelsets.py`; ordered members routed by `evaluate_condition` (the same expression a branch step takes), first match wins, unevaluable conditions SKIPPED not matched; a `model` step naming a set resolves through it and records which member answered; pinning is a sha256 of the blob because nothing increments the `version` column; `/compare` runs every member on one input, `/drift` reports moved members; `tests/test_modelsets.py` (24) | comparison is serial and in-process; no automatic promotion from a comparison | 5 | done | no |
| P06 | Nested-key input validation | ✅ present | `app/input_schema.py` validates against what connectors declare, reporting every issue with its full path incl. array indices; wired into `app/executor.py` AND `app/fast_decide.py`, both raising the same `InputValidationError`; `tests/test_nested_input_validation.py` (31) + `tests/test_fast_path_validation_and_budget_parity.py` assert the two paths agree | the model-predict path (`app/routers/models.py`) is still flat | 3 | days for the remaining model-predict path | no |
| P07 | Matrix with dimensions + import | ✅ present | the matrix is `decision_tables` — `inputs` ARE named dimensions bound to variables, outputs are multiple and unconstrained except the outcome column, and `analyze_decision_table` already reported gaps; import added as `app/decision_table_import.py` + `POST /decision-tables/import`, with the workbook parsed in the BROWSER (SheetJS is already a web dep) so no OOXML parser runs on a 512MB API; `parse_cell` reads `>= 1000` / `100 - 999` / `gold\|silver` / `*`, types are inferred from the data, and an unreadable cell refuses the whole import rather than widening a row; `tests/test_decision_table_import.py` (36) | one sheet per import (first tab); create-or-replace, no row-level merge; no regex or cross-column cells | 6 | done | **yes** |
| P08 | Child workflow by alias | ✅ present | `app/executor.py:777-792` — `_execute_subworkflow` resolves `config.alias` through `engine_config.workflow_aliases`, flat or environment-keyed by the parent's status | — | 3 | done | no |
| P09 | DLQ / Kafka / exactly-once | 🟡 partial | idempotency: `app/idempotency.py` + `idempotency_records` with a (tenant, key) unique constraint; dead letter: a WAL decision that cannot re-insert is bounded by `DECISION_WAL_MAX_REPLAY_ATTEMPTS`, moved out of the boot path (never deleted), counted in `recover()`, logged at ERROR every boot, and requeued by `replay_dead_letter`; `tests/test_decision_wal_dead_letter.py` (14) | no broker — Redis pub/sub with DB polling, so no partition/consumer group/offset for exactly-once to be about | 8 | 6+ months for the Kafka half | no |
| P10 | ClickHouse / OLAP store | ✅ present | `ClickHouseArchiver` for archived decisions (covered by `test_archive_sinks_live.py`), plus predictor values fanned out to their own narrow table via a DEFAULTED `write_predictor_values` on the interface — so existing sinks are untouched; `ColdStoreFanOutTest` covers the fan-out, the no-sink no-op, and a dead sink costing neither the decision nor the hot copy | `S3Archiver` inherits the no-op, so S3 deployments archive decisions but not predictor values | 4 | done | no |
| P11 | Canary deployment with delays | ✅ present | `app/champion_challenger.py` ramp + guardrails + promote/hold/rollback; the ramp drives live traffic via `app/experiments.py`; `scheduler.auto_rollback_experiments` halts a running experiment on a GUARDRAIL BREACH (not on merely losing), leader-gated, audited, off by default behind `EXPERIMENT_AUTO_ROLLBACK`; `test_canary_ramp_assignment.py` (14) + `test_experiment_auto_rollback.py` (12) | halts the experiment rather than reverting an already-promoted policy — a separate lifecycle | 5 | done | no |
| P12 | Feature factory | ❌ absent | `0 hits in any .py/.ts/.tsx; app/backtest.py and app/analytics.py are the seeds` | — | 8 | multi-month | **yes** |
| P13 | Monaco / embedded code editor | ✅ present | CodeMirror 6 with Python highlighting themed from the app's tokens, RuleMind-aware completion from live state, and inline diagnostics via `POST /api/v1/variables/lint` — which reuses `sandbox.validate_source`, so a marker IS the rule rather than a second implementation of it; 600ms debounce, never executes, a failed request shows nothing; `test_variable_lint.py` (10) + `rulemind-completion.test.ts` (19) | only the variables page; diagnostics are syntax + sandbox policy, no name resolution | 6 | done | no |
| P14 | SQL editor | ⛔ cut | `no SQL datasource exists to edit against; 0 hits for postgres/mysql/jdbc/snowflake in connector code` | — | — | n/a | no |
| P15 | Journey as a first-class entity | ✅ present | `journeys` table + CRUD and lock/unlock/duplicate at `/api/v1/journeys`; LOCK IS NOT THE LIFECYCLE — `status` is dev/uat/prod, `is_locked` means "people are filling this in, the screens must not move under them", so a locked journey still promotes and duplicating (unlocked, back in dev) is how you change a live flow; the SDK manifest now serves what the workspace authored, in the camelCase shape the Flutter example already read, falling back to the built-in demos when none are authored; `tests/test_journeys.py` (31) + `e2e/journeys.spec.ts` | no per-field screen editor (a rich form is authored as JSON); `version` increments but old versions are not retained, so "render what this respondent saw" is not answerable after an edit; a journey is not part of the bundle `promote_policy` moves | 7 | done | no |
| P16 | Partitioning / cold storage / retention in months | ✅ present | retention in calendar months (`archiver.normalize_retention_months`), archive-then-purge to the S3 / ClickHouse sink (`storage.archive_and_purge_decisions`), leader-gated scheduled job; `tests/test_retention_archive_purge.py` (13) covers the round trip, the ordering guarantee (a failing sink purges NOTHING), tenant scoping, batching, and the boundary row | table partitioning absent — an optimisation (DROP PARTITION vs DELETE), not the capability: archive-then-purge already bounds the hot table | 3 | done | no |
| P17 | Excel functions (CUMIPMT, DAYS360, EDATE, EOMONTH) | ✅ present | `packages/rule-engine/src/excel-functions.ts:397 CUMIPMT, :686 DAYS360` (with IPMT/PPMT/CUMPRINC), checked against `packages/shared/excel-financial-date.spec.json` — 50 cases **generated from the Python** — by `test/excel-financial-date-parity.test.ts` | — | 3 | done | **yes** |
| P18 | Rate limiting on resource APIs | ✅ present | `app/rate_limit.py` — `classify()` maps method+path to decision/read/write/ai/export/admin with whole-segment matching; `check_request()` enforces the class bucket then the tenant aggregate | class shares are per tenant/env, not per individual route | 3 | done | no |
| P19 | Superflows | ✅ present | ships as **sub-workflow**: `executor.py` `workflow` step, resolved by raw id or per-tenant alias with cycle detection on the resolved id; the composing thing is a policy, so it has the dev→uat→prod lifecycle (`promote_policy` has no step-type restriction); `workflow-builder/page.tsx` lists "Sub-workflow" as a palette item; 4 test modules | — | 7 | done | no |
| P20 | Predictor entity + values + reports | ✅ present | `predictors` + `predictor_values` tables, `/api/v1/predictors` CRUD, drift report at `/{id}/drift`; a predictor POINTS at a scorecard score / variable / outcome rather than recomputing it; values captured at `storage.add_decision` so the series covers every decision path; PSI (`app/drift.py`) with conventional 0.10/0.25 bands, baseline quantile bins, per-bin contributions; `tests/test_drift.py` (18) + `tests/test_predictors_api.py` (30) | values live in the primary DB, not ClickHouse — routing them to the configured archive sink is the remaining scale piece | 4 | done | no |
| P21 | Evaluation timeouts + 504 handling | ✅ present | `EvaluationBudgetExceeded at app/executor.py:159, enforced at :449,486,502,866,958-987; 504 handler at app/main.py:105-121; tested at tests/test_evaluation_budget.py:71` | `app/routers/evaluations.py:70,87,106` runs model evaluation synchronously outside the executor and so outside the budget; the `app/fast_decide.py` gap is closed (same key, same default, checked between variables and once before the compiled core) | 3 | days for the remaining model-evaluation path | no |
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
- **P10 stays 🟡 partial, but not for the reason the 2026-09-04 re-audit gave.** That audit
  demoted it because nothing verified the round-trip. `b017dab` added one against a **real
  ClickHouse container** (`tests/test_archive_sinks_live.py`), wired into CI as a service, so
  that objection is answered. What remains is the row's other clause: it stores archived
  *decisions*, not predictor values — and that half is P20's work, not this row's.
- **P17 promoted to ✅ present.** The 2026-09-04 re-audit ran about three hours before `81252e8`
  landed, so it recorded a gap that closed the same day. `CUMIPMT` and `DAYS360` (plus
  `IPMT`/`PPMT`/`CUMPRINC`) are now in the TypeScript engine, checked against a 50-case corpus
  **generated from the Python** rather than hand-written — which is this row's own e2eFlow, word
  for word. Both mutations tried against it are caught: clamping both `DAYS360` ends, and making
  `CUMIPMT`'s period range exclusive.
- **P16 half-closed.** Retention in **months** shipped in `574da83` — 21 minutes *before* the
  re-audit commit, so "days not months" was already false when it was written; this was missed
  rather than overtaken. Table partitioning genuinely has not shipped: still 0 hits under
  `app/`.
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
