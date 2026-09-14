# Changelog — RuleMind API

Per-package: this repo versions each shipped artifact separately, so a change to
the Flutter SDK does not move the API's version. The root CHANGELOG.md is an index.

Entries are derived from **path-scoped** `git log` against this directory rather than
from the commit's conventional-commit scope — scopes drift (`fix(ops)`, `fix(scale)`,
`feat(api)` all land here), whereas the touched path cannot lie.

## Unreleased

### Added

- **Connectors that actually fetch (P01).** `request_mapping` and `response_mapping` were written
  by the dashboard and read by zero backend lines; a connector step never opened a socket.
  `app/connector_fetch.py` now retrieves a source at decision time through the SSRF-guarded egress
  path, opt-in per connector, with a tenant- and connector-scoped response cache and per-attempt
  timeouts inside the evaluation budget.
- **Outbound webhook subscriptions (P03).** A first-class subscription entity with 13 closed event
  types and ANDed filters evaluated through `logic.compare`, HMAC-signed with the same helper
  `webhooks.py` verifies inbound. Events are emitted from `add_audit_event` and `add_decision` —
  the two documented funnels — so a subscriber cannot miss whichever code path was added last.
- **Outbound PKCS#12 client certificates (P02).** `app/mtls.py` was inbound-only; nothing ever
  presented a client certificate. `app/client_certs.py` unwraps a bundle from the connector config
  and presents it, with contexts cached per (tenant, connector, bundle fingerprint) so a rotation
  takes effect without a restart.
- **Predictors and PSI drift (P20).** `predictors` + `predictor_values` tables, CRUD, and a drift
  report using the industry-conventional 0.10/0.25 PSI bands with per-bin contributions, so the
  report says *where* the population moved. Values are also fanned out to the configured OLAP sink.
- **Polling conditions on the `action` step.** Retrying is not polling: a retry loop exits on the
  first 2xx, and a job-status endpoint answers `200 {"status": "pending"}`. Bounded, budgeted, and
  failing closed to review.
- **A dead letter for the decision WAL.** A record that can never re-insert was retried on every
  boot forever with nothing surfacing it. Now bounded, moved out of the boot path, never deleted,
  logged at ERROR on every boot, and requeueable.
- **Automatic canary rollback (P11)**, on a guardrail breach only — never on a challenger merely
  losing. Opt-in, leader-gated, audited.
- **Editor diagnostics** at `POST /api/v1/variables/lint`, reusing `sandbox.validate_source`.

### Fixed

- **`FAST_DECIDE=1` changed correctness in three separate ways**, each a performance flag acting
  as a correctness switch. It skipped nested input validation entirely; it had no evaluation
  budget, so the 504 contract did not apply; and it seeded every source with its `sample_payload`
  on production traffic, where the executor deliberately leaves an unsupplied source empty so the
  decision fails closed. All three now match the executor, with cross-path tests.
- **A canary that never ramped.** `current_ramp_weight` was called only from analytics, so
  `assign_variant` used the static weight: a canary set to ramp 10% → 25% → 50% served 10% forever
  while the dashboard rendered the schedule as live. Ramping the challenger alone is not enough —
  the champion must take the remainder, or its static 90 still swallows the hash space.
- **Name resolution ran outside the evaluation budget.** `build_pinned_request` → `getaddrinfo` is
  blocking with no timeout of its own and ran before the deadline was consulted, so a decision's
  wall clock tracked resolver latency with no ceiling (4013ms measured against a 300ms budget)
  while blocking the event loop. Fixed here and on the pre-existing action path.
- **The PKCS#12 bundle and its passphrase were stored in the clear.** `SECRET_FIELD_MARKERS` is a
  substring test and contained `password`, which does not match `passphrase`, and nothing matching
  `client_cert_p12` — so a private key sat in a connector row readable with only the read
  capability.
- **A slow engine-cache leak in the test suite.** `db._engine_cache` is keyed by URL and never
  evicts, so every test using a fresh temp path leaked a pool holding SQLite handles to a deleted
  directory. Added `db.dispose_engine`.

### Changed

- Retention's archive-then-purge round trip is now covered end to end, including the ordering
  guarantee: a failing sink purges **nothing**, because the alternative leaves decisions in
  neither the hot database nor the cold store.


## 4.0.0 — 2026-09-03

First tagged release. `git tag` was empty before this, so there is no prior boundary
to slice against and this section covers all history to date.

### Added

- SCIM 2.0 user provisioning (B6, part 4 of 4)
- backup with a verified restore (B6, part 3 of 4)
- Idempotency-Key on public writes (B6, part 1 of 4)
- usage metering and per-plan quota (B4)
- tamper-evident hash chain over the audit log (B3)
- add Gemini, and stop blank env values defeating their defaults (#126)
- configurable per-tenant rate limiting (#115)
- predictive-model evaluation + label-based backtesting (Gini/AUC/KS/calibration/uplift) (#112)
- live collaborative policy editor — WebSocket sync, presence, time-travel (#111)
- circuit breaker, durable decision WAL, change-mgmt (shadow/what-if/rollback), full-population backtest (#110)
- first-class date type + unified loose equality across all engines (#104)
- predictor / experiment-analysis / rejection-analysis BYOK actions (#102)
- deterministic adverse-action reason codes for declined decisions (#95)
- per-tenant client-id dedupe + server receipt time + device/bundle lineage (#93)
- close the dunder-attribute escape surface in the variable sandbox (#92)
- two-person control (maker != checker) for promotion to production (#91)
- policy backtesting — preview a change's impact on real traffic before promotion (#72)
- per-workspace SLOs + outcome-drift alerting (#71)
- per-workspace data-protection controls in UI — retention window + PII redaction + at-rest encryption posture (#70)
- decision replay against current or historical bundle version (#69)
- policy diff before promotion — see + record what decision logic changed (#68)
- move human session to httpOnly cookie + double-submit CSRF (#65)
- encrypt decision payloads at rest + configurable PII redaction (#64)
- decision-log retention — pluggable OLAP archiver (ClickHouse/S3) + Postgres-prod guidance (#62)
- live decision feed via Redis pub/sub fan-out (DB-poll fallback) (#61)
- fail-closed production guard against default/unset secrets (#58)
- JSONL import/export for batch backtests (backend + UI) (#54)
- live decision feed via Server-Sent Events (backend + UI) (#53)
- embeddable decision-block API — GET /sdk/v1/blocks/{policy_id} (#52)
- in-app mutual-TLS client-certificate verification (config-gated) (#51)
- idempotent batch decision-ingest endpoint (on-device sync foundation) (#46)
- on-device decision tables — compiler + Kotlin/Dart evaluators + conformance (#44)
- one-command tracing, unreachable-row note, throughput docs (#43)
- per-workspace AI cost/usage tracking + monthly budget cap (#42)
- decision tables on the fast path + concurrent-map loops (#40)
- enterprise SSO — OIDC + SAML 2.0 with JIT provisioning (#39)
- human RBAC — workspace member accounts, password + email-OTP login, session auth (#38)
- clean fresh install + guided activation journey (#33)
- live model dropdown (auto-updates on new launches) + resizable input preview (#32)
- reports builder — dynamic columns, filters, timezone, scheduled email delivery (#28)
- RBAC — role-scoped API keys + Access & Roles UI (#26)
- config-driven AI gating — AI features stay hidden until a key is configured (#25)
- workflow loops + loop-debug (iterate collections/maps, per-iteration trace) (#23)
- decision tables + optimiser (grid authoring, hit policies, conflict/gap/unreachable/invalid detection) (#22)
- self-serve onboarding backend — signup→dev key→verify→prod key (#21)
- AI slice 2 — rejection-driver analytics + NL→policy + explain-decision (#20)
- AI Copilot foundation — BYO OpenAI/Anthropic key + NL→rule + scope guardrail (#19)
- schedules & ML-model hosting UI + working ML runtime (#17)
- UI overhaul (Stripe-refined) + real Excel & Postman workflows (#15)
- Postman-style API console for workflow action steps (#12)
- visual drag-and-drop workflow builder (#11)
- config-driven, admin-only white-label branding (#10)
- Phase 3 backend — provider library + human routing/SLAs (#6)
- add MECE rule checking, SDK input validation, and dynamic offline sandbox
- 5 major features — Excel functions, CSV export, import validation, model hosting, WoE scoring

### Fixed

- rules builder mode reset, promote with no body, and real auth in e2e (#166, #139)
- unbreak container startup — env-loader depth and a create_all race (#129)
- survive a truncated model response, and honour model ids (#128)
- reject bad inference input instead of guessing or 500ing (#121)
- correct Opus pricing, the temperature allowlist, and the model list (#120)
- make case-wise history reachable, and fix health probes and ids (#116)
- make on-device decisioning work — bundle contract and engine parity (#117)
- unbreak local install — scipy pin, .env loading, and env docs (#114)
- booleans and null are distinct types in loose equality across all 5 engines (#109)
- reject non-finite numbers, parse TZ offsets, guard regex ReDoS (#108)
- omit `temperature` for Claude 5 / OpenAI o-series (they reject it) (#105)
- stop silently swallowing failed decision-log writes (metric + error_event) (#97)
- bounded backpressure + graceful-shutdown flush (async write durability) (#90)
- cross-replica serving-cache coherence via per-tenant epoch (#89)
- decision-path errors fail closed + become observable (no silent wrong decisions) (#66)
- enforce one running experiment per policy + deterministic assignment (#60)
- rate limiter fails closed when Redis is unavailable (#59)
- aggregate A/B + dashboard analytics over the full window (not last 200) (#57)
- thread user_id through /api/v1/decide so experiments actually assign variants (#56)
- large-policy cross-engine accuracy — missing-var parity + depth guard + conformance (#45)
- log exactly one Decision row per /decide (was double-logging) (#41)
- DB pool+WAL, always-authenticated webhooks, access-change audit (#35)
- scale + durability — no 1000-row cap, async email, durable outbox, retry (#31)
- leader election so scheduled jobs fire once across replicas (#30)
- concurrent, no-persist backtest with honest server-side throughput (#29)
- flat input fields must drive source variables (was: all cases approved) (#27)
- add alembic.ini so container startup migrations actually run (#24)
- run alembic migrations on container startup (#14)
- resolve Python MECE type detection, Flutter analyzer errors, and web TS error

### Changed

- enable ruff bugbear -- select = ["E9", "F", "B"]
- finish the mypy sweep -- mypy.ini is down to one narrow block
- mypy wave 5 -- 94 to 64, and a FastAPI constraint mypy cannot see
- mypy wave 4 -- 166 -> 94, and two tests that depended on ambient state
- mypy waves 1-3 -- storage.py is now clean (225 -> 166 errors)
- extract runtime + config endpoints into app/routers/runtime.py (monolith split 14/N, final feature slice) (#86)
- extract SDK edge + execution endpoints into app/routers/sdk.py (monolith split 13/N) (#85)
- extract platform-admin + mobile endpoints into app/routers/platform.py (monolith split 12/N) (#84)
- extract experiment + hosted-model endpoints into routers (monolith split 11/N) (#83)
- extract AI + provider-template endpoints into app/routers/ai.py (monolith split 10/N) (#82)
- extract access + auth (identity) endpoints into app/routers/identity.py (monolith split 9/N) (#81)
- extract onboarding endpoints into app/routers/onboarding.py (monolith split 8/N) (#80)
- extract reports-builder endpoints into app/routers/reports.py (monolith split 7/N) (#79)
- extract webhook/schedule/review endpoints into app/routers/operations.py (monolith split 6/N) (#78)
- extract policy endpoints into app/routers/policies.py (monolith split 5/N) (#77)
- extract rule/scorecard/decision-table endpoints into app/routers/rules.py (monolith split 4/N) (#76)
- extract connector + variable CRUD into app/routers/authoring.py (monolith split 3/N) (#75)
- extract insights + audit endpoints into app/routers/insights.py (monolith split 2/N) (#74)
- extract governance endpoints from main.py into app/routers/governance.py (#73)
- async decision logging by default with read-after-write flush (#37)
- async httpx so a slow LLM never exhausts the request threadpool (#36)
