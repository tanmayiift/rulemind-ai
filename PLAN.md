# RuleMind — execution plan

**Status:** sections 0–7 done. Section 8 (parity) is **dropped by decision** — replaced by the
production-readiness tiers below, which are what stands between this repo and a rollout.

Rules: work in order · after each step run the relevant suite, tick the box, commit ·
no pausing for confirmation between steps · a real blocker goes under **Blockers** below
with what was tried, then move to the next step.

---

# Part II · Production readiness

The answer to "can I roll this out" was **no**, and not because the system is fragile — because
it can return **silently wrong decisions as HTTP 200**, on the default code path, under exactly
the load you would deploy into. Three independent audits (deployment, failure modes, security)
converged on that.

Target: **Kubernetes via the helm chart**, **multi-tenant with untrusted authors** — the worst
case on both axes, so nothing below is optional on trust-model grounds.

Honest scale: **4–6 weeks**. Tier 1 is the smallest tier and is where the product is *wrong*.

Already verified solid, so we don't redo it: the audit hash chain, quota metering,
Idempotency-Key middleware ordering, SCIM's least-privilege default, backup-with-verified-restore,
the tenant-isolation matrix's anti-rot design, the scheduler's DB-lease leader election, and the
sandbox AST guard's threat model. The controls that exist are good. What was missing is an
adversarial sweep of the full route and deserialization surface.

## T1 · The product returns wrong answers

- [x] **1.1** `UNIQUE(tenant_id, client_id)` + `client_id = user_id` meant only one decision per
      customer ever persisted — no compliance artifact, no billing, WAL replay loop, 404 reason
      codes. Subject moved to a new `user_id` column, on both the executor and fast paths.
- [x] **1.2** A timed-out variable became `None`, and `None` flips catch-shaped risk gates toward
      approve. Errors now propagate, `/decide` returns a `variable_errors` map, and any failed
      variable fails closed to `review` on **both** paths (parity-tested).
- [x] **1.3** Enforceable evaluation budget + 504. Nothing bounded a decision: the only limit
      was per-variable, applied in a serial loop, so the real bound was `timeout_ms x
      variable_count` — 110s for the demo tenant's 55 variables, holding a threadpool thread.
      Enforced cooperatively (deadline check between variables *and* between steps, plus a clamp
      of each variable's `timeout_ms` to the remaining budget) rather than with
      `asyncio.wait_for`, which would return 504 while the work kept its thread. Default 10s in
      `engine_config.evaluation_budget_ms`; 0 disables.
- [x] **1.4** `/decide` UPDATEd the policy row every request (`last_test_result`, a console
      badge). Scoped to the console path.
- [x] **1.5** Rule/variable/scorecard/decision-table CRUD emitted no `AuditEvent` — and, found
      while closing qa 6.2, there was **no route that read the trail back at all**. Every reader
      filtered to one `event_type` or `entity_id`, so "show me every change to this rule" was
      answerable only by opening the database. Added `GET /api/v1/audit` (filters + pagination,
      ordered by chain position).
- [x] **1.6** Two qa tests read fields the endpoints never return (3.10, 4.9).

## T2 · Untrusted authors have RCE

- [x] **2.1** `pickle.loads` on an uploaded blob ran **in the API process**. pickle executes
      `__reduce__` on deserialization (before any `predict`), so it was RCE, reachable at AUTHOR
      (i.e. `policy_maker`). Now: deserialization runs in a dedicated, memory-capped, kill-on-
      timeout worker pool with the master key + DB creds scrubbed from its environment; the size
      guard is enforced at upload (`validate_model`), before anything is unpickled; and
      `/api/v1/models` management is gated behind `MANAGE_ACCESS` (predict/test stay `DECIDE`).
- [x] **2.2** Sandbox pool wedging + silent degradation to unsandboxed exec. Recycle-on-timeout
      (terminate the wedged worker instead of leaking the slot), rebuild-on-`BrokenProcessPool`,
      `SANDBOX_FALLBACK_MODE` default flipped to **fail** (inline is now opt-in), and the memory
      cap moved from a per-call `setrlimit` in a reused worker (which leaked tenant A's cap onto
      tenant B) into a pool-wide worker initializer. All four code caches now bounded (from 2.5).
      Proven by a real wedge-and-recover test; removing the recycle makes it hang, which is the
      original bug.
- [x] **2.3** SSRF + secret exfiltration on the real connector path. New `app/egress.py`:
      scheme check, host **resolution** with private/loopback/link-local/reserved range checks
      (catches `127.1`, `2130706433`, IPv6-mapped, and a public name with a private A record),
      the vetted IP **pinned** for the connection so a DNS rebind can't swing it, redirects no
      longer followed, and an optional deny-by-default allowlist
      (`RULEMIND_ACTION_ALLOWED_HOSTS`). Applied to both the secret-injecting executor path and
      the console; the old substring blocklist is deleted. `timeoutMs`/`retries` clamped.
      A block is recorded as an alertable `action_error` event.
- [x] **2.4** Deleted the unguarded `eval` in `logic.py` (scorecard/metric formulas). Replaced
      with `safe_formula`, a whitelisting AST evaluator — no `eval`, no `__builtins__`, no
      attribute access — so the `().__class__.__mro__[1].__subclasses__()` walk has nothing to
      stand on. Plus CPU/memory-bomb bounds on `**` and string `*`.
- [x] **2.5** Sandbox validator + the three sibling caches keyed on MD5 → SHA-256 (a
      chosen-prefix collision could seed a benign entry and skip validation on a colliding
      malicious body). All four caches were global and unbounded across tenants; now bounded.

## T3 · Multi-tenant isolation gaps

- [x] **3.1** Unauthenticated WebSocket with no tenant scoping. New `app/ws_auth.py`
      authenticates **before** `accept()` (api-key header/param, bearer, and the dashboard's
      `rm_session` httpOnly cookie — which browsers do send on a same-origin WS handshake, so the
      web client needed no change). The hub is now keyed by `(tenant_id, doc_id)`; doc ids are
      caller-supplied policy ids, so cross-tenant collisions were routine. `edit`/`restore` now
      require the AUTHOR capability, so a viewer can watch but not write. Every REST sibling
      (`state`/`history`/`as-of`/`seed`) is authenticated and tenant-scoped too.
- [x] **3.2** Widened the isolation matrix's discovery from `/api/v1/<name>` CRUD pairs to the
      **whole route table**, WebSockets included. Every mutating route must now be probed,
      covered by a named suite, or listed in `MUTATING_ROUTE_COVERAGE` with a reason — and a
      guard-on-the-guard test asserts `/ws/v1`, `/scim/v2` and `/api/admin/v1` are actually
      visible, so discovery cannot silently narrow again. Added the cross-tenant SCIM probes the
      map promises (read/list/replace/patch-deactivate/delete all 404 across tenants). Writing
      the map immediately caught five prefixes that name no live routes.
- [x] **3.3** RBAC's unknown-write default flipped from `AUTHOR` to `MANAGE_ACCESS` (fail
      closed). Every previously-unmapped prefix is now classified explicitly, and the identity
      prefixes (`/scim/v2`, `/api/admin/v1`, `/api/v1/auth`, `/api/mobile/v1/auth`) require
      `MANAGE_ACCESS` — SCIM provisioning, *including deactivating the owner*, had been reachable
      at AUTHOR. An anti-rot test fails if any new mutating route lands on the default.
- [x] **3.4** `active_tenant_id` now raises instead of silently serving the FIRST tenant; the
      fallback survives only under `AUTH_MODE=none`, which is single-tenant by definition. The 15
      storage writers now resolve *explicit → auth context → payload*, so a body field can no
      longer outrank auth.
      **This change immediately found a live multi-tenant bug**: the unauthenticated webhook
      receiver used the tenant-scoped `get_webhook()`, which with no auth context resolves to the
      first tenant — so **webhooks were broken for every tenant but the first**, surfacing as a
      bare 404. Added `get_webhook_for_trigger()` (by UUID, signature still verified) and pointed
      the receiver and its error path at the webhook's own tenant.
- [x] **3.5** SAML hardening. Removed the fallback to the **unverified** document (XSW); the
      identity now comes from the signed element only. Added `Conditions` validation
      (`NotBefore`/`NotOnOrAfter` with 2-min skew, `AudienceRestriction`,
      `SubjectConfirmationData` `Recipient`, Response `Destination`), single-use replay tracking
      via a new `saml_assertions_seen` table with `UNIQUE(tenant_id, assertion_id)`, and an XML
      parser with entity resolution disabled (it parses attacker XML pre-auth).
- [x] **3.6** Unauthenticated, unthrottled tenant + API-key minting (`/onboarding/signup` returns
      before the rate-limit check). Also `/api/admin/v1` sits outside all middleware.

## T4 · The chart does not deploy

- [x] **4.1** Empty-string config is not "unset". `DATABASE_URL=""` raises at import →
      CrashLoopBackOff with no useful log; `RULEMIND_CONFIG_KEY=""` becomes `sha256("")`, a
      publicly derivable key — and setting a real one later makes `decrypt_config_payload`
      silently return `{}`, emptying every connector config. Unrecoverable.
- [x] **4.2** Every decrypt returns something plausible on the wrong key — ciphertext goes out as
      a live auth header, or straight into `pickle.loads`. Raise instead; add `MultiFernet` +
      rotation; use a real KDF.
- [x] **4.3** Liveness on `/health` kills the fleet on a DB blip. No probe budgets anywhere, and
      `/health` is a sync `def` sharing the threadpool with `/decide`.
- [x] **4.4** No resource requests/limits, so the HPA reports `<unknown>` and never scales.
- [x] **4.5** Pool math is 8–30x over `max_connections` (up to 3200 vs a default 100).
- [x] **4.6** Migrations race (six concurrent runners, no lock) and failure is swallowed — the API
      starts anyway against a stale schema. Job has a fixed name, so the 2nd `helm upgrade` fails.
      Related: migration 0001 is `create_all` against live models, so a model change shipped
      without a migration works everywhere fresh and is **missing in production**.
- [x] **4.7** The deployed UI cannot reach the API — build-time inlining, cluster-internal DNS the
      browser can't resolve, no ingress API route, localhost `CORS_ORIGINS`.
- [x] **4.8** Containers run as root with no `securityContext`; `restricted` PSS rejects the pods.
      Also `.dockerignore` misses `.venv-rulemind` (482MB) and `.env` — which is baked into the
      web image.
- [x] **4.9** `RULEMIND_ENV=production` in every manifest; point the `Secure` cookie flags at the
      same helper, or session and admin-JWT cookies ship without `Secure` behind TLS.

## T5 · Operability

- [x] **5.1** There is no logging configuration at all — every `logger.info` is dropped. An
      operator currently cannot tell why a decision was slow or failed.
- [x] **5.2** `/metrics` is per-process and wrong (no multiproc dir, no ServiceMonitor).
- [x] **5.3** Redis has no socket timeouts — a *hung* Redis blocks every threadpool thread.
- [x] **5.4** 8+N queries per decision; needs a TTL cache **with single-flight**.
- [x] **5.5** Decision WAL on ephemeral disk, and `recover()` unlinks files another worker is
      appending to — enabling it can lose the decisions it exists to protect.
- [x] **5.6** Cross-worker read-after-write: the `decision_id` `/decide` returns 404s elsewhere.

## T6 · Data protection defaults

- [x] **6.1** No default redaction for financial or health PII — a raw card number is stored
      verbatim and returned in full to a **viewer**-role user.
- [x] **6.2** `rule_results`, `scorecard_result` and `trace` are plaintext, and the trace carries
      evaluated inputs — which substantially defeats encrypting `payload_preview`.
- [x] **6.3** Make `pip-audit` and `pnpm audit` gating (both `continue-on-error` today).

## Gates

| Gate | Start | Now | Target |
|---|---|---|---|
| Python suite | 892 OK | **1200 OK** | monotonically up, SQLite + Postgres |
| qa core | 26 pass / 5 fail | **30 pass / 0 fail / 1 warn** | 31/31 — the warn is T6.1, which must become a *fail* |
| qa sandbox matrix | 21/21 | — | 21/21 + pool wedging, post-abuse re-exec, and an assertion on **which mode ran** |
| Playwright | 53 | — | 53 (WebSocket authz covered by 14 Python tests) |
| Isolation matrix | CRUD only | **every mutating route, incl. `/ws/v1`, `/scim/v2`, `/api/admin/v1`** | done |
| `helm install` clean cluster | crashloops | — | boots, probes pass, serves UI, survives upgrade |
| Load test vs a budget | never run | — | worst-case decision time bounded + documented |

New gate: a k6/locust run holding the sandbox pool **saturated** while asserting no decision
returns a `None`-derived outcome. That is the specific regression T1.2 + T2.2 create, and no
existing lens catches it.

CI cost: gating from `cbea732` holds — push triggers `sdk-verify` (path-filtered, no emulators)
and `security` (docs-ignored). Batch commits, push at tier boundaries, emulators stay
`workflow_dispatch`-only.

---

# Part I · Sections 0–8 (complete, kept as the record)

## 0 · Housekeeping

- [x] **0.1** Reclaim disk. Repo is 6.4 GB; ~5.6 GB is regenerable build cache —
      `demo-app/build` (2.6 GB), `demo-app/.dart_tool` (2.5 GB),
      `packages/rulemind-core-rs/target` (239 MB), `rulemind-decide-service/target` (76 MB),
      and the Flutter `build`/`.dart_tool` dirs under `packages/`. **Keep `node_modules`
      (354 MB) and `.venv-rulemind` (384 MB)** — both live; reinstalling costs more than the
      space. All targets verified gitignored with 0 tracked files.

## 1 · Merge the open PRs, in dependency order

My CI hardening is **not on main** — verified: `sdk-verify.yml` carries 14 hardening markers
on `fix/ci-artifact-quota` and zero on `main`. Wiring CI before this merges builds on a file
about to change underneath it.

- [x] **1.1** `#131` CI artifact quota — first, it unblocks the rest
- [x] **1.2** `#130` frontend a11y + mobile clipping
- [x] **1.3** `#135` builder step types (independent of the mobile stack)
- [x] **1.4** `#132` responsive mobile
- [x] **1.5** `#134` SDK integration (retarget to main once #132 lands)
- [x] **1.6** `#136` demo redesign (retarget once #134 lands)
- [x] **1.7** Verify main green; delete merged local + remote branches

## 2 · Fix the QA harness *before* wiring anything to it

- [x] **2.1** `qa/e2e-full/run.py:8` — `mods = {"core": suite_core}` never imports
      `suite_sandbox`, so `python run.py sandbox` prints `(no suite 'sandbox')` and **exits 0
      having run nothing**. Register it, and exit non-zero on an unknown suite name.
- [x] **2.2** `qa/e2e-full/harness.py:41-51` — `dump()` writes JSON and returns; `run.py`
      falls off the end. **A suite with 6 failures exits 0.** Exit 1 when `fail` or `block`
      is non-zero.
- [x] **2.3** Prove both: `run.py sandbox` runs 21 cases; a deliberately broken case turns
      the exit code non-zero.
- [x] **2.4** Run `suite_core` once against merged main and record the real result.
      `results_core.json` (from #119) says 6 failures; `E2E_TEST_PLAN.md` claims several were
      fixed since. Nothing in the repo resolves it — one run decides gate vs non-gating.
- [x] **2.5** `suite_core.py:219` filters `/api/v1/audit/decisions?userId=…`. `userId` is not
      an accepted param (`insights.py:68`) and the row has no `user_id` (`storage.py:780-801`)
      — FastAPI drops it, so the `len(mine) >= 3` assertion at `:226` matches a key that never
      exists. Filter on `client_id`.

## 3 · Testing gap 1 — the offline→online round trip

Python, not Dart: `RuleMind._httpClient` (`rulemind.dart:33`) is private with no injection
seam, and `resetForTest` (`:355-366`) never clears `_decisionOutbox`/`_decisionSyncer`, so
outbox state leaks between tests.

- [x] **3.1** New `apps/python-executor/tests/test_sdk_offline_round_trip.py`, house setUp
      from `test_sdk_decision_sync.py:9-47` incl. `os.environ.pop("AUTH_MODE", None)` so the
      test traverses real auth
- [x] **3.2** Round trip: record built from the **exact** `_toPending` key set
      (`rulemind.dart:164-187`); offline = never POST; assert audit empty → POST → assert
      `{received:1,inserted:1,duplicates:0,acked:[id]}` → assert exactly one row.
      **Key detail:** the SDK's `executionId` lands in the audit row's **`client_id`**, not
      `id` (`storage.py:1385`, `:782`) — so query `?client_id=<executionId>`
- [x] **3.3** Idempotency: replay identical batch → `{inserted:0,duplicates:1}`, still one row
- [x] **3.4** Multi-decision drain, oldest-first, `acked` covers every id
- [x] **3.5** `/sdk/v1/signing-key` — **zero** test references today. Assert 200/401, RS256,
      PEM parses, and that it **verifies a real `/sdk/v1/blocks/{id}` signature** (pattern at
      `test_decision_block.py:85-92`)
- [x] **3.6** Dart companion: `SqfliteDecisionOutbox` via `sqflite_common_ffi` through a stub
      uploader that fails then succeeds past `backoffMs`
- [x] **3.7** File an issue for the SDK testability defect (leaky `resetForTest`, no
      injectable client)

## 4 · Testing gap 2 — wire the unrun suites into CI

- [x] **4.1** *Free tier (≈0 wall clock).* New parallel `rust` job —
      **`cargo test --no-default-features`**, because `default = ["python"]` → pyo3
      `extension-module` deliberately does not link libpython and a plain `cargo test` fails
      at link time. Append 3× `flutter test` (`demo-app`, `packages/flutter-ui`,
      `packages/sdk-flutter/example` — **~200 widget tests currently unrun**) to the existing
      `flutter` job. Append `suite_sandbox` to `repo-validation`, reusing
      `qa/mobile_e2e/start_fastapi.sh`.
- [x] **4.2** *Cheap tier.* Postgres `services:` container + matrix dim on the Python step.
      `DATABASE_URL` is the only config — `storage.py:480,488` calls `create_all` and
      `psycopg2-binary` is already in requirements. Alembic **not** required.
- [x] **4.3** Playwright as its own parallel job (**48 tests, not 52** — the 52 in
      `docs/parity/` is stale), paths-filtered
- [x] **4.4** k6 — **do not gate.** `RULE_ID` defaults to the literal `"replace-me"`
      (`evaluate.k6.js:13`) and `p(95)<500` at 50 VUs will not hold on a 2-core runner.
      `workflow_dispatch` + nightly.
- [x] **4.5** Delete the dead `qa/*.ts` suite + its 3 root scripts.
      `run-automated-suite.ts:6` imports `../apps/api/src/server`, which does not exist; four
      files hardcode `/Users/tanmaykumar/Downloads/RuleMind.AI` — a different machine. Keep
      `qa/workbook-runner.ts` (healthy, imported by `test/workbook-matrix.test.ts`).
- [x] **4.6** Rotate the hardcoded key out of `demo-app/lib/engine.dart:77` — a **shipped**
      app, so it lands in the APK/IPA and `strings` recovers it
- [x] **4.7** Add `concurrency` groups to both workflows (neither has one, so every push runs
      a duplicate set)
- [x] **4.8** File an issue for `playwright.config.ts:45,60` setting `AUTH_MODE=none` on both
      servers — `middleware.py:93` early-returns on it, bypassing API key, RBAC **and** CSRF,
      so all 48 browser tests exercise a permanently unauthenticated app

## 5 · Track B0 — real linting

- [x] **5.1** Replace `py_compile app/*.py` with `ruff check` + `ruff format --check`. That
      glob matches 57 top-level files and misses all 22 under `app/routers/` and `app/core/`.
- [x] **5.2** `mypy` with a per-module ratchet — no clean sweep of the 3515-line `storage.py`
- [x] **5.3** Keep the `apps/web` vitest runner honest (one real test landed in #135)

## 6 · Track B2 — security scanning *(currently none of any kind)*

- [x] **6.1** CodeQL (python + javascript)
- [x] **6.2** `dependabot.yml`
- [x] **6.3** `pip-audit` + `pnpm audit --audit-level=high`
- [x] **6.4** `CODEOWNERS`

## 7 · Track B3–B6 — the revenue blockers *(weeks each)*

- [x] **7.1** B3 audit immutability (2–3 wk) — **biggest deal-blocker**. `AuditEvent` has no
      hash chain and the archiver purges the hot copy, so an operator with DB access can
      rewrite history undetectably. The Alembic revision must mark pre-existing rows
      *pre-chain* rather than fraudulently verified.
- [x] **7.2** B4 usage metering + quota (2–3 wk) — **no way to bill today**
- [x] **7.3** B5 tenant-isolation matrix (1 wk), parameterised over the router list
- [x] **7.4** B6 SCIM, backup with a *verified* restore, `Idempotency-Key`, OpenAPI diff gate
- [x] **7.5** Issues: **#125, #166, #139, #138, #127 closed.** #123 stays open by decision —
  the flag-gated harness is shipped; it needs one run with a real provider key, and closing
  on a skipped test would recreate the state it was filed to end. #133 stays open too: a
  GitHub runner-image regression with no local repro, but its CI cost and noise are gone.

## 8 · Phases 3–8 — parity *(months; each needs a checkpoint before starting)*

- [x] **8.1** Phase 3 quick wins (2–3 wk). **Audited 2026-09-03 against the code, not the
  survey this list came from — two of the eight are already shipped:**
  - ~~connector connection-test~~ — exists: `POST /api/v1/connectors/{id}/test`
    (`routers/authoring.py:96`)
  - ~~expose `decision_table` in the builder palette~~ — shipped in #135 (4 refs in
    `apps/web/app/workflow-builder/page.tsx`)
  - **five of the six then-open items shipped on 2026-09-04**, so this is no longer a backlog.
    Re-verified against the code 2026-09-05:
    - ~~sub-workflow **by alias**~~ — `executor.py:880` `_resolve_workflow_alias`, alias wins
      over `ref_id` at `:912-927`, registry in `engine_config.workflow_aliases`
    - ~~nested-key input validation~~ — `app/input_schema.py`, hooked at `executor.py:399-417`
    - ~~per-resource rate limits~~ — `rate_limit.py:164` `RESOURCE_CLASSES`, per-class
      resolution at `:372-378`
    - ~~retention in **months**~~ — `models.py:621` `audit_retention_months`, `archiver.py:72`
      `normalize_retention_months`
    - ~~504 / evaluation-timeout contract~~ — `main.py:153-162`, `EvaluationBudgetExceeded`
    - ~~polling conditions on the `action` step~~ — shipped 2026-09-05. `mode: "poll"` in
      `executor.py` `_poll_action`, condition through `logic.compare` (so `status == "complete"`
      means here what it means in a rule, across all five engines), bounded by
      `clamp_polls`/`clamp_poll_interval_ms` in `egress.py` and by the evaluation budget, and
      failing **closed** to review on timeout rather than handing the policy a `pending` payload.
      33 tests in `tests/test_action_polling.py`, plus the SDK handoff in Kotlin and Dart.
  - ~~**Two residual holes the strikethroughs must not hide**~~ — both closed 2026-09-05 (B1/B2).
    `fast_decide.py` now validates and budgets identically to the executor. `core/engine.py` is
    deliberately untouched: its `validate_input` covers an unrelated `policy["input_schema"]`
    that is only ever persisted for model rows, and as the no-I/O module shared with the Kotlin,
    Dart and TS runtimes it has no access to connectors or tenant `engine_config` — which are
    exactly what the nested schema is derived from.
- [ ] **8.2** Phase 4 outbound webhooks, p12, predictor, DLQ (4–6 wk)
- [ ] **8.3** Phase 5 canary, modelset, step-through debugger (6–8 wk)
- [ ] **8.4** Phase 6 Monaco + matrix entity (6–10 wk)
- [ ] **8.5** Phase 7 graph canvas, journey entity (**3–6 mo** — a re-architecture; do not
      start until 3–6 are green)
- [ ] **8.6** Phase 8 Kafka, feature factory (**6+ mo** — build only against a paying
      requirement)

---

## Note on this file

Earlier ticks were applied on feature branches, each cut from `main`, so the
chained status-line edits silently no-oped and the counter never advanced past
`1/46`. This file is now updated **on `main` only**, in one place, after the
work it describes is committed. Do not tick it from a feature branch.

---

# Production-readiness pass (opened 2026-09-04)

Numbering below is independent of the waves above; those are closed. Three audits merged into
15 correctness bugs, 21 coverage gaps, 10 unbuilt features, 17 doc drifts. Order is fix →
validate → deploy.

Two findings reframe what "validated" meant before this pass:

**No CI signal.** `main` is ahead of `origin/main` and every commit carries `[skip ci]` (added
to protect an exhausted Actions quota). Everything here has been validated locally only.

**Green that asserted nothing.** `qa/e2e-full/suite_core.py` swallowed non-200 as success, so
8 of 25 "passes" — every sandbox-escape case plus the timeout case — proved nothing. And the
Postgres lane was 11% real: `Storage(path=…)` short-circuited `DATABASE_URL`, so 797 of 1360
tests re-ran SQLite under a Postgres label.

## W4 · Restore signal

- [x] **W4.1** Fix the 8 fake passes in `suite_core.py`; re-record `results_core.json`.
- [x] **W4.2** Core QA suite gates again (the 5 failures it waited on were fixed in `4090911`).
- [x] **W4.3** Make the Postgres lane real — `RULEMIND_TEST_PG_SCHEMA_PER_PATH` gives each
  test path its own Postgres SCHEMA, the Postgres equivalent of its own SQLite file.

  Two things had to be right before it produced signal rather than noise. `SET search_path`
  from a `connect` listener is silently undone: psycopg2 opens an implicit transaction for it
  and the pool's rollback on check-in discards it, so every query ran against `public` and all
  1361 tests shared one database. It is a libpq `-csearch_path` connect option now, which the
  server applies at session start and no rollback touches. And the engine cache holds one engine
  per test path — thousands over a run — so a pooled engine exhausted `max_connections` partway
  through and 204 tests died on "sorry, too many clients already". `NullPool` for that lane.

  **What it found, once real:** `scripts/backup_verify.py::_storage_for` claimed to bind Storage
  to the URL it was given, but for a SQLite URL it passed only a path — which `$DATABASE_URL`
  overrode. So verifying a SQLite backup on a host with `DATABASE_URL` set walked the audit
  chain of the *live Postgres database* instead of the restored file, and reported the restore
  clean. That is the one answer a backup verifier must never get wrong. Also two tests that
  could only ever check one backend: `test_enterprise` asserted "no plaintext secret in the
  database" by opening the SQLite file directly, and passed against a row it never read.
- [x] **W4.4** Redis service in CI; `test_redis_integration.py`'s 10 tests stop skipping.
- [x] **W4.5** Build the PyO3 extension in CI, so `test_rust_core.py` runs and the `_HAVE_RUST`
  production branch at `fast_decide.py:328` gets covered for the first time.
- [x] **W4.6** `cargo test` the decide-service (was `cargo check`; its 10 tests never ran).
- [x] **W4.7** Replace the vacuous assertions — 7 Playwright "page loads" tests that the nav
  satisfied on any route, 6 silent `if (!createdRuleIdShared) return;` exits, two scheduler
  assertions accepting `"unconfigured"` (which means *not delivered*), and 3 seed-dependent
  `skipTest`s including a backtest test that had never once run its assertions.
- [x] **W4.8** Wire up the three scripts nothing invoked: `verify_chart.py` (every PR),
  `smoke_compose.sh` (dispatch-only — it builds two images), `saturation.k6.js` (load-test).

## W5 · Correctness bugs

- [x] **W5.1** `executor.py:437` seeds every source from the connector sample, so
  `POST /decide {"payload":{}}` returns **approve** on a demo `bureau_score: 756`. A broken
  integration approves with a legitimate-looking audit trail. Scope seeding to test/simulation.
- [x] **W5.2** `core/engine.py:24-25` — `loop`/`branch`/`workflow`/`monitor` are in neither
  step-type set, so they hit "unknown step type" and the loop CONTINUES. Same fail-open fixed
  in `fast_decide`, still open on the what-if / backtest / governance-replay path.
- [x] **W5.3** `store.ts:51` API base URL is build-time inlined → the shipped bundle points at
  `localhost:8080`. Hard blocker for the Vercel stage.
- [x] **W5.4** Platform-admin audit events omit `tenant_id`; a superadmin brute-force lands in
  an unrelated workspace's tamper-evident chain.
- [x] **W5.5** `next` 14.2.25 → 14.2.34 (10 advisories) + retire 5 dead `--ignore-vuln` lines.
- [x] **W5.6** `AuditEvent.metadata_json`/`ip_address` plaintext; `verify()` should report
  `metadata_undecryptable`, not raise, on a wrong key.
- [x] **W5.7** Durable login lockout. Without Redis the counter was a per-PROCESS dict, which
  is not a lockout: a restart resets it, and each replica keeps its own count so N replicas
  multiply the attempts an attacker gets — handed over free by round-robin routing. The
  endpoint returned 429 exactly as designed right up until it silently didn't. `login_attempts`
  sits between Redis and the dict.
- [x] **W5.8** 25 numeric `os.getenv` reads converted to `env.env_int`/`env_float`. A
  set-but-blank value makes `int("")` raise, and most of these were at module scope — a boot
  failure with a ValueError that never named the variable. The Helm chart renders optional
  values through `| quote`, so blank is what an unconfigured setting actually looks like. An
  AST scan in `test_blank_env_config` now fails on any new one (it caught one I had missed).
- [x] **W5.9** deleted `auth.generate_rsa_keypair_material` — returned the SAME value as both
  the public and the private key, and had zero callers.
- [x] **W5.10** SAML replay pruner scheduled. `prune_saml_assertions` existed and had a test;
  nothing called it. One row per SSO login, never read except to reject a replay, and that
  lookup is on the login path — so the table grew forever and the cost landed on auth.

## W6 · Coverage that hid the above

- [x] **W6.1** `evaluateScorecard` deprecated and given a shape guard. TypeScript callers were
  already protected (`factors` is required on the type), so only JS consumers hit it — and they
  got `Cannot read properties of undefined (reading 'reduce')`, naming neither cause nor fix.
- [x] **W6.2** There are none left. All ten were closed by `server-scorecard.ts` and its
  generated corpus, and each is covered — but two thinly, so seven cases were added where one
  could pass by luck: negative fractional weights (Python's `int()` truncates toward zero,
  -3.5 → -3, where `Math.floor` gives -4), the lower clamp at 0, and the four distinct ways a
  formula can raise and must fall back to `base_score`. All five engines agree, at 173 cases.
- [x] **W6.3** ClickHouse + MinIO round-trip against real containers, wired into CI. The finding
  was bigger than the coverage gap: the sink clients are lazy optional imports absent from the
  shipped image, so `DECISION_ARCHIVE_SINK=clickhouse` failed at the first write, the purge
  never ran (it is the second half of archive-then-purge), and `retention_enforced` still
  reported **true** — the one endpoint an operator would check to discover retention was not
  happening said it was. `archiving_available()` now answers about behaviour, not configuration.
  Also fixed a test that used the SHARED store and so passed only inside the full run.
- [x] **W6.4** CUMIPMT, CUMPRINC, IPMT, PPMT and DAYS360 ported to TypeScript against a
  50-case corpus generated from the Python, with the generator committed beside it.

## W7 · Deploy

Vercel (dashboard) · Supabase (Postgres) · Upstash (Redis) · Koyeb (API + scheduler).

- [x] **W7.0** (added) `/ready` now probes the SANDBOX, not just the database and Redis. This
  is the prerequisite that makes W7.3's "prove the sandbox spawns there" answerable with a curl
  instead of a guess: on a runtime that forbids `spawn`, every user-authored Python variable
  fails, the decisions still come back (fail-closed, T1.2) but without the inputs the policy is
  written around — and the pod answered 200, so the load balancer routed to it and the deploy
  looked successful. `SANDBOX_FALLBACK_MODE=inline` reports `degraded`, not `ok`: it works, but
  untrusted customer Python is running in the API process with no RLIMIT and no process
  boundary. `/health` deliberately does NOT gain this — a liveness probe that spawns a process
  per scrape restarts the fleet whenever the pool is briefly recycling.

  Running `smoke_compose.sh` for this then found three more, none of which any unit test could
  have caught:

  1. **The readiness probe had never once detected a database outage.** `_dependency_status`
     wrote `with storage.connect(): db_status = "ok"`, which looks like a connectivity check and
     is not — `SessionLocal()` builds a Session lazily, acquiring no connection until a
     statement runs, and the commit on an unused session is a no-op. With Postgres stopped it
     still answered 200, so a pod with no database stayed in rotation. It runs `SELECT 1` now.
     Invisible on SQLite, where the "connection" is a local file that is always there.
  2. **The smoke test was grading the wrong server.** A stale uvicorn the QA harness had left on
     127.0.0.1:8080 answered every check; two failed against a process that had never seen the
     image, and the ones that passed were just as meaningless. A deployability gate that can
     silently test something other than the deployment is worse than none, so it now refuses to
     start when anything holds a port the stack publishes.
  3. **The end-to-end decision had never run through compose.** Compose seeds nothing (correctly
     — it may be someone's real environment), so there was no policy to decide against; that
     check only ever passed against the stale server's seeded data. `RULEMIND_SEED_DEMO` is
     declared in compose, off by default, and the smoke run opts in.

  Also fixed the script's own reporting: `compose up`'s stderr was discarded and container logs
  printed instead, so a port conflict showed forty lines of stale Redis chatter and never the
  words "port is already allocated". **14 passed, 0 failed.**
### W7 outcome — deployed, on a different stack than planned

**All of it on Render** (dashboard + API + Postgres + Valkey), not Vercel + Supabase + Upstash
+ Koyeb. Koyeb's
console is decommissioned and Supabase's free tier is full of Orbit; putting all three backend
tiers on Render gives one region and a private network between them instead of three public TLS
hops per decision. Defined as code in `render.yaml` rather than dashboard clicks.

Live: API `https://rulemind-api.onrender.com` · dashboard
`https://rulemind-dashboard.onrender.com`

```
/ready -> {"ready":true,"db":"ok","redis":"ok","sandbox":"ok","sandbox_mode":"pool"}
```

`sandbox: ok` is the line that mattered — it is the platform assumption the whole plan rested
on, and it is now answered by a curl rather than a guess.

**Three things only the real deployment could have found:**

1. `dockerCommand: sh -c "... && ..."` exited **127** with no output. Render splits that value
   into argv on whitespace instead of handing it to a shell, so the quotes survive as literal
   characters. Moved into `render-start.sh`, which is also testable against the image locally.
2. **No security headers at all.** The only header coming back was `x-request-id` — no nosniff,
   no frame-ancestors, no HSTS, no referrer policy. `SecurityHeadersMiddleware` is outermost so
   they land on the 401/403/429 responses too, and HSTS keys on `X-Forwarded-Proto` because
   behind a TLS-terminating proxy the app itself only ever sees plain http.
3. **`/docs`, `/redoc` and `/openapi.json` were public and unauthenticated** — the complete
   endpoint map and every schema. Off in production now, `RULEMIND_ENABLE_DOCS=1` to opt back in.

- [x] **W7.1** Postgres — `rulemind-db` (PostgreSQL 18, Singapore), Alembic chain applied
  through `20260904_0024` on first boot.
- [x] **W7.2** Redis — `rulemind-kv` (Valkey 8, Singapore), `noeviction` so a login-failure
  counter is never silently evicted.
- [x] **W7.3** API — `rulemind-api`, free tier, one instance, secrets generated by Render
  (`generateValue`) so none was ever typed, pasted or logged. Auth verified enforced on the
  live deployment: every data endpoint 401s unauthenticated and a wrong key is rejected.
- [x] **W7.4** Dashboard — **on Render as a static site, not Vercel.**
  `https://rulemind-dashboard.onrender.com`

  Vercel's GitHub App could not see the private repo, and granting it needed a GitHub sudo-mode
  re-auth. Render's GitHub App was already connected to this exact repository, so moving the
  dashboard there cost zero new accounts and zero new permission grants — and put the whole
  deployment in one file.

  A **static site**, not a web service. Verified rather than assumed that it could be: the only
  thing matching `*api*` under `apps/web` is a page called api-console and a client-side fetch
  helper — no Next.js API routes, no dynamic segments, no middleware, no `next/image`, no
  `getServerSideProps`. Static sites are free, have no instance, and unlike the free *web* tier
  do **not** spin down, so the dashboard has no cold start even though the API does.

  `output: "export"` is opt-in via `NEXT_OUTPUT`, because `next start` refuses to run an
  exported build and `apps/web/Dockerfile` ends in exactly that — making it unconditional would
  have broken compose and the smoke test's "web app serves" check for a setting only the static
  host needs.

  Verified live: all routes serve as real pages (not a SPA shell), the bundle carries the
  deployed API origin rather than localhost, a browser fetch from the dashboard origin to the
  API returns 200, and CORS echoes that origin exactly while an unlisted origin gets no header
  at all — which matters because `allow_credentials` is on and the session is an httpOnly
  cookie.

  Two more Render schema quirks, after `dockerCommand`: a static site may not declare a
  `region` (there is no instance to place), and the failure is a Blueprint sync error rather
  than a build error.
- [ ] **W7.5** deployed-environment e2e + k6 — partially done unauthenticated; the authenticated
  half needs the API key, which is deliberately only readable from the Render dashboard.
- [ ] **W7.6** CI — **blocked, and not by `[skip ci]`**. Every recent run failed in 3-12s with
  **0 steps executed**, which is an exhausted Actions quota, not a test failure. Local runs
  remain the only signal until the quota resets.
- [ ] **W7.1 (superseded)** Supabase project + Alembic chain · **W7.2** Upstash · **W7.3** Koyeb, with fresh
  `RULEMIND_CONFIG_KEY`/`RULEMIND_ADMIN_JWT_SECRET` (the production gate refuses the committed
  dev defaults). **Prove the sandbox spawns there** — if the runtime blocks `spawn`, every
  Python variable fails closed and the product does not work.
- [x] **W7.4** Dashboard deployed + `CORS_ORIGINS` — on **Render as a static site**, not Vercel
  (this line said "Vercel import" and contradicted the checked W7.4 above it; the Vercel project
  is deleted). · [x] **W7.5** deployed-environment smoke, 21/21 · [ ] **W7.6** push without
  `[skip ci]` — still blocked on the Actions quota.


---

# Parity backlog build-out (opened 2026-09-05)

Full scope approved: the entire parity backlog, multi-agent, with a standing test gate. The
roadmap sizes phases 4-8 at 12+ months for 1-2 engineers, so this is delivered phase by phase,
each leaving `main` green and deployable.

**Standing gate on every item** (`docs/parity/LENS-QA.md:6-14`): tenant isolation, RBAC across
5 roles, an audit event with the actor, OpenAPI regenerates clean, and at least one negative
test. Anything touching decision semantics also gets five-engine parity against a corpus
GENERATED from the Python. Every fix mutation-checked.

## A. Phase 0 - prove the deployment

- [x] **A1** `.env.deployed` gitignored + `.env.deployed.example` committed
- [x] **A2** `scripts/smoke_deployed.sh` - 24 checks; 21/21 green unauthenticated
- [x] **A3** Defect: `load-test.yml` set `RM_BASE`; k6 reads `BASE_URL`/`API_KEY` (my bug)
- [x] **A4** Defect: `README.md` claimed auto-seeding, false since it became opt-in
- [x] **A5** Defect: spec claimed `e2e/global-setup.ts` resets the DB; no such file exists
- [x] **A6** Defect: `PLAN.md` 8.1 listed 6 open items; 5 had shipped
- [ ] **A7** Seed the deployed workspace via `POST /api/v1/onboarding/load-samples`
- [ ] **A8** Run `suite_sandbox` (21 cases) against the deployment
- [ ] **A9** Run `suite_core` (31 cases) against the deployment
- [ ] **A10** `smoke_deployed.sh` fully green including the 3 authenticated checks

## B. Phase 1 - correctness bugs and harness debt

- [x] **B1** `FAST_DECIDE=1` skips input validation - wire `input_schema` into `fast_decide.py`
      and `core/engine.py`, same semantics as `executor.py:399-417`, fail closed (closes P06)
- [x] **B2** Evaluation budget is executor-only - same exception class so the 504 handler at
      `main.py:153-162` catches it (closes the P21 residual)
- [x] **B3** Playwright remote-capable: hosts behind env vars, `webServer` off for remote,
      wss derived from https, destructive specs gated behind `RM_E2E_ALLOW_DESTRUCTIVE`
- [x] **B4** Adversarial review of B1-B3; anything refuted goes back
- [x] **B5** Polling conditions on the `action` step - five engines + generated corpus
- [x] **B6** Update P06 and P21 parity rows with the fixes
- [ ] **B7** Full regression gate green (both DB lanes, 5 engines, all gates)

## C. Phase 2 - P01, connectors that actually fetch

- [x] **C1** Decision-time fetch through the SSRF-guarded egress path, fail closed
- [x] **C2** `request_mapping` / `response_mapping` actually read and applied
- [x] **C3** Caching + per-source timeouts inside the evaluation budget
- [x] **C4** DoD five + P01 parity row + `README.md:6` claim becomes true

## D. Phase 3 - roadmap phase 4 (parallel)

- [ ] **D1** P02 p12 / PKCS#12 outbound client certificates
- [ ] **D2** P03 outbound webhooks: subscription model, event types, filters
- [ ] **D3** P20 predictor entity + values + reports
- [ ] **D4** P09-partial: DLQ + `Idempotency-Key` on the existing bus
- [ ] **D5** Migration chain serialized; DoD five each; regression gate

## E. Phase 4 - roadmap phase 5 (parallel)

- [ ] **E1** P04 workflow step-through debugger
- [ ] **E2** P05 modelset
- [x] **E3** P11 canary — ramp now drives live traffic; automated rollback remains
- [ ] **E4** DoD five each; regression gate

## F. Phase 5 - roadmap phase 6 (parallel)

- [ ] **F1** P07 matrix entity with dimensions + import
- [x] **F2** P13 editor — CodeMirror 6 (highlighting + RuleMind-aware completion); diagnostics remain
- [ ] **F3** DoD five each; regression gate

## G. Phase 6 - roadmap phase 7 (sequential, a re-architecture)

- [ ] **G1** P15 journey as a first-class entity - table, CRUD, web route, SDK across 5 engines
- [ ] **G2** P19 superflows - needs product definition before engineering
- [ ] **G3** DoD five each; regression gate

## H. Phase 7 - roadmap phase 8

- [ ] **H1** P09-full Kafka / exactly-once
- [ ] **H2** P12 feature factory

## I. Docs and CI

- [x] **I1** parity.json + PARITY-MATRIX.md: P17 -> PRESENT, P10 re-word, P16 half-close,
      six missing `gap` fields filled
- [x] **I2** README architecture tree completed + Deployment section added
- [x] **I3** `E2E_TEST_PLAN.md`: 16 -> 24 migrations, 733 -> 1457 tests. **PG 16 -> 18 was
      artefact list
- [ ] **I4** 13 per-package `CHANGELOG.md` files, regenerated from path-scoped `git log`
- [ ] **I5** Vercel GitHub App access revoked (needs the user's 2FA)
- [ ] **I6** CI: one commit without `[skip ci]` once the Actions quota resets

## Blockers

### RESOLVED 2026-09-03 — the #127 half of this was wrong

I recorded that #127's last three rows needed infrastructure I could not supply. That was
incorrect: Keycloak, aiosmtpd and Redis are REAL implementations, so the "no mocks" objection
never argued against running them locally. All three are closed — an in-process OIDC provider
over real sockets, a real aiosmtpd server, and docker Redis behind a REDIS_URL gate. The Redis
one found a production bug (rate_limit's NOSCRIPT recovery never fired).

**#123 remains open, by decision, not by blocker.** The harness exists; the validation does
not. One run with a real key closes it:

```bash
cd apps/python-executor
RULEMIND_AI_LIVE=1 RULEMIND_AI_LIVE_PROVIDER=anthropic RULEMIND_AI_LIVE_KEY='<key>' \
  ../../.venv-rulemind/bin/python -m unittest tests.test_ai_live -v
```

### OPEN 2026-09-05 — three things that need you, not me

These are the only items in this plan that cannot be finished from this session. Everything else
either is done or is work I can do without you.

**1. The deployed workspace is empty, so no authenticated call has ever succeeded against it.**
`scripts/smoke_deployed.sh` is 21 passed / 0 failed / **3 skipped**, and the three skips are the
ones that would prove the product *works* there rather than merely that it refuses strangers.
A7–A10 all sit behind this.

To unblock, paste the key into a gitignored file — nothing echoes it, and it never enters a
transcript or a commit:

```bash
cp .env.deployed.example .env.deployed
# then edit .env.deployed and set RULEMIND_DEPLOYED_API_KEY to the value of
# RULEMIND_DEV_API_KEY from Render -> rulemind-api -> Environment
```

Then `scripts/smoke_deployed.sh` goes to 24/24, and the two remote-capable suites
(`suite_sandbox`, `suite_core`) can run against Render.

**2. Vercel's GitHub App still has access to this repository (I5).** The Vercel *project* that
was failing on every push is **deleted** — that was the actual source of the "still getting
vercel deployment errors" reports, and it is gone. What remains is only permission hygiene: the
GitHub App can still read the repo. Revoking it needs a GitHub sudo-mode re-auth, which needs
your 2FA, so it is yours to do:
GitHub → Settings → Applications → Installed GitHub Apps → Vercel → Revoke.

**3. GitHub Actions minutes are exhausted (I6).** Every recent run failed in 3–12 seconds with
**zero steps executed** — a quota refusal, not a test failure and not `[skip ci]`. Until the
quota resets, local runs are the only signal, and this plan says so rather than implying CI
coverage it does not have. When it resets, one commit without `[skip ci]` closes it.
