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
- [x] **8.2** Phase 4 outbound webhooks, p12, predictor, DLQ — **shipped.** P03/P02/P20 and the DLQ half of P09 are all PRESENT in `parity.json`; the box was simply never ticked.
- [x] **8.3** Phase 5 canary, modelset, step-through debugger — **shipped.** P11/P05/P04 all PRESENT (see sections E1–E3).
- [x] **8.4** Phase 6 editor + matrix entity — **shipped.** P13 (CodeMirror 6, counted equivalent to Monaco) and P07 both PRESENT (F1–F2).
- [x] **8.5** Phase 7 graph canvas, journey entity — **shipped** (G1–G2). Was sized 3–6 months as a re-architecture; the re-audit found P19 already shipped as sub-workflows and P15's shape already existed in `experience_studio.py`. (**3–6 mo** — a re-architecture; do not
      start until 3–6 are green)
- [x] **8.6** Phase 8 Kafka, feature factory — **shipped 2026-09-14** (H2 and J18–J24). P09 was greenfield rather than a migration, which is why it took a session; see section J. (**6+ mo** — build only against a paying
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

> **SUPERSEDED 2026-09-14 — this deployment no longer exists.** The free tiers moved to paid and
> the API service is already down (`/health` answers nothing). `render.yaml`, `render-start.sh`,
> `scripts/smoke_deployed.sh` and `.env.deployed.example` were deleted in J1-J2; the project now
> runs only on local Docker (`make up`) and a local kind cluster (`make k8s-up`). The record
> below is kept because the Render-specific failures it documents are real and were expensive to
> find — a platform that splits `dockerCommand` on whitespace, a static site that may not declare
> a region — and they generalise to the next platform. Nothing below is live.


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
- [x] **W7.5** ~~deployed-environment e2e + k6~~ — **closed by removal 2026-09-14.** The deployed environment is gone (see section J); the same coverage now runs locally — `suite_sandbox` 21/21 and `suite_core` 31/31 against compose, plus `smoke_compose` 14/14 and `smoke_k8s` 16/16. Original text: partially done unauthenticated; the authenticated
  half needs the API key, which is deliberately only readable from the Render dashboard.
- [x] **W7.6** CI — **UNBLOCKED 2026-09-16 with a self-hosted runner.** The diagnosis in the
  original text was wrong twice over: it is a **billing block, not an exhausted quota** (jobs
  get no runner and execute `steps: 0`, so consumption is already ~0), and it did not need the
  quota to reset at all. A self-hosted runner is free on a private repo.
  `.github/workflows/self-hosted-tests.yml` now runs the full regression on
  `workflow_dispatch` — never `pull_request`, since it executes on a real machine and there are
  15 open bot-authored dependency PRs.
- [x] **W7.1 (superseded)** Supabase project + Alembic chain · **W7.2** Upstash · **W7.3** Koyeb, with fresh
  `RULEMIND_CONFIG_KEY`/`RULEMIND_ADMIN_JWT_SECRET` (the production gate refuses the committed
  dev defaults). **Prove the sandbox spawns there** — if the runtime blocks `spawn`, every
  Python variable fails closed and the product does not work.
- [x] **W7.4** Dashboard deployed + `CORS_ORIGINS` — on **Render as a static site**, not Vercel
  (this line said "Vercel import" and contradicted the checked W7.4 above it; the Vercel project
  is deleted). · [x] **W7.5** deployed-environment smoke, 21/21 · [x] **W7.6** CI runs — on a
  **self-hosted runner** (2026-09-16). GitHub-hosted runners remain blocked by a billing
  failure, not a quota, and that turned out not to matter: a self-hosted runner is free on a
  private repo and runs the same suite.


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
- [x] **A7** Seed the workspace via `POST /api/v1/onboarding/load-samples` — **done locally 2026-09-14.** Retargeted to local
  compose.** Was blocked on an API key for the Render instance; that instance is gone, and
  `qa/e2e-full/harness.py:7` already defaults to `http://127.0.0.1:8080`, which is the compose
  API port. No code change needed, only a running stack.
- [x] **A8** Run `suite_sandbox` (21 cases) against local compose — **21/21 pass** 2026-09-14
- [x] **A9** Run `suite_core` (31 cases) against local compose — **31/31 pass** 2026-09-14
- [x] **A10** ~~`smoke_deployed.sh` fully green~~ — **closed by removal.** The script tested a
  hosted instance that no longer exists. `scripts/smoke_compose.sh` (14 checks) and the new
  `scripts/smoke_k8s.sh` cover the same ground locally, and unlike the deployed script they can
  actually be run by anyone who clones the repo.

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
- [x] **B7** Full regression gate green — Python 1761 on SQLite and 1670 on Postgres
  (schema-per-path), vitest 462, cargo 19 + 10, flutter 163, Kotlin, Playwright 58/58,
  26 migrations applied + downgraded on real PG 16, deployed smoke 21/21,
  check_parity / verify_chart / check_audit_ignores

## C. Phase 2 - P01, connectors that actually fetch

- [x] **C1** Decision-time fetch through the SSRF-guarded egress path, fail closed
- [x] **C2** `request_mapping` / `response_mapping` actually read and applied
- [x] **C3** Caching + per-source timeouts inside the evaluation budget
- [x] **C4** DoD five + P01 parity row + `README.md:6` claim becomes true
- [x] **C5** (found during C1-C4) **Third `FAST_DECIDE=1` correctness divergence**, same shape as
  B1 and B2. `executor._payloads_by_source` gates sample seeding on `_SAMPLE_SEEDING_SOURCES`
  ({test_console, simulation, loop_debug}) so that on production traffic an unsupplied source
  stays empty and the decision fails closed. `fast_decide._compute_variables` seeded every source
  from `sample_payload` unconditionally, so `/decide` with the flag on evaluated against demo
  data — approving, by construction — where the executor failed closed. Seeding is now opt-in and
  only `_run_decision_batch` asks for it, matching the `source="simulation"` it passes on its own
  executor branch.
  - A **second, older divergence** surfaced underneath it: the fast path's flat-field override
    asked `if field in sample`, reading a source's field set off its sample, so it worked only
    because seeding was on. The executor iterates `connector["schema_paths"]`. The bundle now
    carries `schema_paths` and the fast path uses the same declaration.

## D. Phase 3 - roadmap phase 4 (parallel)

- [x] **D1** P02 p12 / PKCS#12 outbound client certificates — `app/client_certs.py`,
  wired into `connector_fetch`. Python's `ssl` cannot build a client identity from
  memory, so the decrypted PEM touches a 0600 temp file unlinked in a `finally`; two of
  the seven mutations were exactly that leak.
- [x] **D2** P03 outbound webhooks: subscription model, event types, filters — and
  `manage_access` rather than `author`, because a subscription is standing egress
  configuration, not a policy artifact
- [x] **D3** P20 predictor entity + values + reports — PSI drift with the conventional
  0.10/0.25 bands; a predictor POINTS at a number rather than recomputing one, and
  capture hooks `add_decision` so the series cannot miss a decision path
- [x] **D4** P09-partial: DLQ on the decision WAL. `Idempotency-Key` already shipped in
  W7.x (`app/idempotency.py` + the `idempotency_records` table), so this was the DLQ half
  only: a WAL decision that can never re-insert is now bounded and dead-lettered instead
  of retried on every boot forever.
- [x] **D5** Migration chain serialized (one head, 26 revisions, applied + downgraded
  against real Postgres 16); DoD five per entity; regression gate green on BOTH lanes

## E. Phase 4 - roadmap phase 5 (parallel)

- [x] **E1** P04 workflow step-through debugger — `/api/v1/debug/sessions` + a web surface,
  built as ONE executor field (`debug_stop_before`) on the real execution path rather than a
  debug interpreter. Two things it turned up: the parity test compared two `simulate=True` runs
  and so could not see that `simulate` also switches SAMPLE SEEDING on — the debugger was showing
  demo values for fields the caller never sent (fixed with `seed_samples_override`; the test now
  compares against a real production run) — and `_payloads_by_source` overwrites
  `normalized["custom"]` unconditionally, so a seeding test written against `custom` passes
  whether or not seeding is on.
- [x] **E2** P05 modelset — ordered routing by branch-style condition, pinned on a blob
  hash (nothing increments the version column), compare and drift endpoints
- [x] **E3** P11 canary — ramp drives live traffic, and a guardrail breach now halts the
  experiment automatically (leader-gated, audited, opt-in)
- [x] **E4** DoD five each; regression gate green on the same tree 2026-09-06: Python **1892** (SQLite) and **1891** (Postgres 16, schema-per-path), vitest 462, cargo 19 + 10, flutter 163, Kotlin 46, Playwright **68/68** with `RM_E2E_ALLOW_DESTRUCTIVE=1`, smoke_compose 14/14, smoke_deployed 21 passed / 3 skipped, plus check_parity, verify_chart, check_audit_ignores and `helm lint`.

## F. Phase 5 - roadmap phase 6 (parallel)

- [x] **F1** P07 matrix entity with dimensions + import — the ENTITY already existed:
  `decision_tables`' `inputs` are named dimensions, and its optimiser already reported gaps,
  so the row's "~4 weeks, no matrix entity" was wrong and only import was missing. Added
  `app/decision_table_import.py` + `POST /decision-tables/import`, parsing the workbook in the
  browser (SheetJS is already a web dependency; OOXML on a 512MB API process is a
  decompression-bomb surface). Found a real bug on the way: `field_type` defaulted to "string"
  makes `analyze_decision_table` unable to order the column, so every row came back
  "not_analyzable" and the GAP ANALYSIS SILENTLY REPORTED NOTHING. Types are now read off the
  data. Two inference tests passed for the wrong reason until a mutation refused to die.
- [x] **F2** P13 editor — CodeMirror 6: highlighting, RuleMind-aware completion, and
  inline diagnostics served by the sandbox's own validate_source
- [x] **F3** DoD five each; same gate as E4 above — one run covers all three phases, since they share a tree.

## G. Phase 6 - roadmap phase 7 (sequential, a re-architecture)

- [x] **G1** P15 journey as a first-class entity — table, CRUD, lock/unlock/duplicate, web
  route, and the SDK manifest serving what a workspace authored. The row's "3-6 months, not even
  one shape to build a table from" was wrong: `experience_studio.py` defines the shape,
  `/sdk/v1/experience-manifest` serves it, and the Flutter example already RAN it, so one of the
  four e2e clauses was done before this started. Lock is deliberately NOT the lifecycle. Found a
  live trap: journey ids are slugified titles, so a workspace naming one "Instant Personal Loan"
  collides with a built-in demo id and inherited five demo scenarios whose payloads answer
  screens it does not have.
- [x] **G2** P19 superflows — re-audited, not built: the capability ships as
  **sub-workflows** (a `workflow` step resolved by id or per-tenant alias, cycle-guarded;
  the composing thing is a policy, so it already has the dev→uat→prod lifecycle). The
  row had been scored on a NAME having zero hits. P19 ABSENT (3–6 months) → PRESENT.
- [x] **G3** DoD five each; same gate as E4 above.

## H. Phase 7 - roadmap phase 8

- [x] **H1** P09-full Kafka / exactly-once — **done 2026-09-14** (J18–J24). This line previously read: The DLQ and idempotency halves shipped (P09 is PARTIAL, not ABSENT) and carry most of the buyer-visible value without a broker. What is left needs a broker to be about: there is no partition, consumer group or offset for exactly-once to mean anything against. 6+ months, and the plan's own rule is to build it against a paying customer.
- [x] **H2** P12 feature factory — two of the three e2e clauses already worked (a variable
  receives the already-computed variables, so a DERIVED feature is an ordinary variable; and
  scorecards bin on variable_id). Backfill was the real gap, and it was hurting shipped P20 too:
  `predictors.observe` is forward-only, so a new predictor had an empty 30-day baseline. The
  difficulty is the timestamp — `PredictorValue.created_at` defaults to now, and accepting that
  drops replayed history into the CURRENT window, so the drift report compares today against
  today and reports no drift. A wrong baseline is worse than an empty one.

## I. Docs and CI

- [x] **I1** parity.json + PARITY-MATRIX.md: P17 -> PRESENT, P10 re-word, P16 half-close,
      six missing `gap` fields filled
- [x] **I2** README architecture tree completed + Deployment section added
- [x] **I3** `E2E_TEST_PLAN.md`: 16 -> 24 migrations, 733 -> 1457 tests. **PG 16 -> 18 was
      artefact list
- [x] **I4** Per-package `CHANGELOG.md` files. Six carry an `Unreleased` section covering
  this cycle (API, dashboard, both mobile SDKs, and the two Rust crates, which already
  had one). The other seven packages have **no commits since the 1.0.0 boundary**, so
  they correctly have nothing to add — writing an empty Unreleased heading into each
  would be noise, not coverage. Entries are prose explaining WHY, matching the existing
  convention, rather than a `git log` dump.
- [x] **I5** Vercel GitHub App access revoked — **done by the owner on 2026-09-16** at
  `github.com/settings/installations/157614895`. Evidence it held: the Deployments and
  Environments cleanup ran *after* the uninstall and both remain at **0** — a still-installed App
  would have recreated a `Production` deployment on the pushes since. Original note kept below,
  because the reasoning for why this needed the owner is still the reasoning:

  ~~needs the owner's 2FA; re-confirmed 2026-09-16.~~ Evidence it still matters: `vercel[bot]` created **5 deployments** on this repo,
  the most recent **2026-09-04**, so the App held write access and nothing since has removed it.
  (The Vercel *project* is gone; the *installation* is a separate thing and outlives it.)

  Verified unreachable from here, twice: `gh api /user/installations` returns 403 with
  `X-Accepted-Oauth-Scopes:` **empty** — no OAuth scope grants it — and the body says
  *"You must authenticate with an access token authorized to a GitHub App"*, which `gh` cannot
  mint. `gh auth refresh` does not help. `/repos/{owner}/{repo}/installation` needs an App JWT
  and returns 401.

  One click, in a browser with 2FA:
  **Settings → Applications → Installed GitHub Apps → Vercel → Configure → Uninstall**
  (`github.com/settings/installations/157614895`).
- [x] **I6** CI — **CLOSED 2026-09-16 by a different route than this item assumed.** GitHub-hosted
  runners are still blocked (a billing failure, not minutes: the annotation on every failed job
  reads *"recent account payments have failed or your spending limit needs to be increased"*, and
  jobs get no runner at all). The fix was not to unblock them: a **self-hosted runner** is free
  on a private repo and executes the same suite. Registered, online, and running the full
  regression. Three attempts were needed, and each failure was a real defect in the workflow that
  only running it could surface — a missing venv (`actions/checkout` makes a fresh clone), then
  `psycopg2-binary` building from source because bare `python3` here is Homebrew 3.14 while the
  project targets 3.11.

## J. Local-only, optimise, close P09 — 2026-09-14

The Render deployment is moving to paid tiers, so the hosted footprint goes.

**CORRECTION (2026-09-14, later the same day). This section originally said "the Render API is
already dead". That was WRONG, and the error matters.** The claim came from a single 30-second
`curl` that timed out — which is exactly what a sleeping free-tier service looks like on its
first request. Re-probed three times in a row: **HTTP 200 in 16.2 s (cold), then 0.16 s, then
0.16 s.** Both the API and the dashboard are **live**. Deleting `render.yaml` deleted the
CONFIG, not the SERVICES: they kept running the whole time, and may still be billing.

A false "it is already gone" is the most expensive kind of wrong here — it is precisely what
leaves something running and unnoticed. `scripts/render_teardown.sh` now deletes every resource
via the Render API given a token, and it re-probes afterwards rather than trusting a timeout.

Four findings that shaped the steps:

1. **`decision_bus.py` is not a durable bus.** 113 lines of best-effort Redis pub/sub for the
   dashboard SSE feed, one producer (`storage.add_decision`), one consumer
   (`routers/runtime.py:717`), and a docstring saying publishing "is best-effort and never
   allowed to affect the decision write path." P09's e2eFlow describes a durable path that does
   not exist yet, so **P09 is greenfield, not a migration**. `decision_log.py:19` already names
   the gap: "the durable-outbox follow-up".
2. **No APK/AAB exists anywhere** — not on disk, and `git log --diff-filter=A` says none was
   ever committed. The space is 1.75 GB of ignored build caches plus **23.3 GB of reclaimable
   Docker layers**.
3. **`render-start.sh` is `COPY`'d and `chmod`'d** at `apps/python-executor/Dockerfile:24,28`.
   Deleting the file without editing both lines fails the image build.
4. **The QA harness already defaults to `127.0.0.1:8080`**, so A7-A10 retarget to local compose
   with no code change.

### A. Local-only: strip paid hosting, make k8s real

- [x] **J1** Delete `render.yaml`, `scripts/smoke_deployed.sh`, `.env.deployed.example`; drop the — `render.yaml`, `scripts/smoke_deployed.sh`, `.env.deployed.example` deleted; dangling `.gitignore` comment removed
  dangling `.gitignore:4-6` comment
- [x] **J2** Delete `render-start.sh` AND `Dockerfile:24,28`; rebuild to prove the build passes — `render-start.sh` deleted with `Dockerfile:24,28`; image rebuilt clean (`Successfully built`) before committing
- [x] **J3** Drop the dead `isStaticExport` branch from `apps/web/next.config.js` — `isStaticExport` branch removed; `apps/web/out/` dropped from `.gitignore` and disk
- [x] **J4** Purge the hosted narrative from README/PLAN/CHANGELOG/E2E_TEST_PLAN + stale comments — README Deployment rewritten as compose + kind; W7 marked SUPERSEDED rather than erased (its platform failures generalise); A7-A9 retargeted local, A10 closed by removal; comments in PythonCodeEditor/store.ts/decision_table_import/test_security_headers/parity.json/load-test.yml de-hosted
- [x] **J5** `make k8s-up` / `k8s-down`: kind cluster, load local images, `helm install` — `make k8s-up` — kind cluster, images built from the same Dockerfiles compose uses and `kind load`ed (no registry), `helm install` with all four secrets generated per install
- [x] **J6** `scripts/smoke_k8s.sh` — first time the chart is proven, not just linted — `scripts/smoke_k8s.sh` — **16/16 on a fresh cluster.** Installing the chart for the first time found **four defects that linting cannot see**, listed below
- [x] **J7** Rewrite README Deployment as compose + kind, both free — README Deployment rewritten as a two-path table: `make up` (compose) and `make k8s-up` (kind + Helm), both free, both with a committed smoke script

**What installing the chart found — none of it visible to `helm lint` or `verify_chart.py`,
because all four are runtime or ordering failures and the rendered YAML was valid throughout:**

1. **The chart was un-installable as shipped.** `configmap.yaml` sets `RULEMIND_ENV=production`,
   which arms `security_config.PRODUCTION_SECRET_CHECKS` — a fail-closed gate over **four**
   secrets. The chart supplied values for two. `RULEMIND_ADMIN_PASSWORD` and
   `RULEMIND_DEV_API_KEY` had no key in `values.yaml` at all, so the API pod raised
   `InsecureProductionConfigError` and CrashLoopBackOff'd. Fixed by adding both.
2. **Helm hook ordering made a fresh install impossible.** `job-migrate.yaml` is a
   `pre-install,pre-upgrade` hook doing `envFrom: configMapRef: rulemind-config`, but the
   ConfigMap and Secret were plain manifests — and Helm applies hooks *before* regular
   manifests. Result: `Error: configmap "rulemind-config" not found`, 26 times, until the
   install timed out. Fixed by annotating both as hooks at weight `-10` (ahead of the Job's
   `-5`), with `before-hook-creation` rather than `hook-succeeded` so the Deployments still
   have their config after the hooks finish.
3. **A concurrent-boot race in seeding — a product bug, not a chart bug.** Every replica runs
   `Storage.seed_if_empty()` at startup against one shared database, and it is check-then-insert
   with nothing in between. The api and worker pods boot together, both saw an empty table, both
   inserted, and the loser died on
   `duplicate key value violates unique constraint "ix_api_keys_lookup_hash"`. The chart's
   production defaults are api=3 + worker=2, so a real first install races **five** ways.
   ~1900 tests never saw it because the suite is one process against SQLite. Fixed with a
   bounded retry, covered by `tests/test_seed_concurrency.py` (4 tests), and both mutations
   checked — removing the retry fails the loser test, making it unbounded hangs the bounded one.
4. **`make help` could not show these targets.** Its grep was `^[a-zA-Z_-]+:`, which excludes
   digits, so every `k8s-*` target was invisible in the one place that lists them.

The schema-creation race is NOT in that list, and deliberately: `_create_schema` already takes a
Postgres advisory lock and swallows a lost race, and its docstring describes this exact
worker-startup crash. Four threads hitting it on SQLite does fail, but SQLite is single-process
by definition — that is a test artifact, so the thread test constructs its Storage objects
serially and races only the seeding.

### B. Reclaim disk

- [x] **J8** `docker image prune` / `system prune` — 23.3 GB of 24.6 GB reclaimable — Docker **24.6 GB -> 8.07 GB**. Dangling layers (2.6 GB), the unused `mcr.microsoft.com/playwright` image (3.8 GB, zero references in this repo), dangling volumes (136 MB). Left your other projects' images (buildit, pulsetrade) alone
- [x] **J9** `rm -rf` ignored build caches (~1.75 GB). NOT `node_modules`, `.venv-rulemind`, `.env` — repo **2.6 GB -> 976 MB**. Rust `target/` 834 MB, `.next` 214 MB, Flutter build+`.dart_tool` 329 MB, `.mypy_cache` 118 MB, Gradle 43 MB. Each checked with `git ls-files --error-unmatch` before removal; nothing tracked was touched
- [x] **J10** Delete the 128 MB `.runtime/` dev SQLite DBs (confirm first — discards local state) — dev SQLite DBs removed (~108 MB). Inspected first: 1,108 tenants named `p01-<timestamp>` / "Isolation Probe" against 29 rules, 15,388 error_events — test residue, not curated work, and reseeded by `RULEMIND_SEED_DEMO=1`
- [x] **J11** Add `.mypy_cache/` + `.ruff_cache/` to the root `.gitignore` — `.mypy_cache/` + `.ruff_cache/` added to the root `.gitignore` (they self-ignored via tool-generated nested files)

### C. Performance: latency and footprint

- [x] **J12** Baseline FIRST — `bench.py` p50/p99, both k6 scripts, `docker stats`, image sizes — baseline captured on a quiet stack: p50 29.03ms / p99 70.01ms at conc=1, and throughput barely moving (32.9 -> 72.5 rps for 16x load) — a serialization signature, not work
- [x] **J13** Instrument real query count per decision (documented as "8+N") — **6** queries per warm decision, not the '8+N' the comments guess. The catalog cache already serves connectors/variables/settings. The real waste is 55 sandbox calls for a policy that reads 6 variables
- [x] **J14** Fix the hotspots the measurements name, each with a before/after number — measured `COMPUTE_ONLY_USED_VARS=1` at **48.7% faster** (32.70 -> 16.78ms p50, 55 -> 6 variables) — then found it **breaks 16 tests**, so it is documented as unsafe rather than recommended. See `docs/PERFORMANCE.md`
- [x] **J15** Web image is **2.54 GB** (`COPY . .`) — multi-stage build, target under 500 MB — **2.54 GB -> 232 MB (-91%)** via a 3-stage build on Next `output: "standalone"`. Verified serving, not just small: `/` and a deep link 200, referenced CSS+JS both resolve 200, uid 10001, smoke_compose 14/14
- [x] **J16** Trim API image; check whether the ML stack can import lazily — **already done — no change needed.** The plan assumed sklearn/lightgbm were resident per worker (the codebase's own comments say so). `python -X importtime -c 'import app.main'` prints nothing for sklearn/lightgbm/scipy/numpy: they are already lazy. Recorded as measured-and-not-needed rather than quietly dropped
- [x] **J17** Re-run baseline, publish before/after in `docs/PERFORMANCE.md`. Parity corpus must — outbox cost measured directly: **+0.098 ms/decision (+0.3% end to end)**. An end-to-end re-run first showed 29 -> 53 ms, which was **machine noise** — load average 17.45 from orphaned sandbox workers plus a uvicorn from 2026-08-29 still burning a core. One INSERT cannot cost 24 ms, which is what made it worth checking
  not regress — accuracy is not traded for speed

### D. P09 — durable delivery and exactly-once on Kafka (KRaft)

The roadmap's 6+ month row. Phased so each step ships independently; P09 only moves to PRESENT
at J24.

- [x] **J18** Kafka in KRaft mode in compose (behind a profile) and in the Helm chart — Kafka KRaft (no ZooKeeper) under the `kafka` compose profile; healthy in ~15s
- [x] **J19** Transactional outbox in Postgres — decision write + outbox row in one transaction — `decision_outbox` table + migration `20260914_0029`; the row is inserted in the SAME session as the Decision, which is the entire guarantee
- [x] **J20** Relay outbox → Kafka, idempotent producer, per-tenant partition key, leader-gated — relay with `enable.idempotence`+`acks=all`, stamping `sent_at` only on a clean delivery report after `flush()`; leader-gated on the scheduler
- [x] **J21** Consumer group with manual offset commits tied to the DB transaction; dedupe via — consumer group with `enable.auto.commit=false`, offsets committed after the handler, dedupe by atomically claiming the event id in `idempotency_records`
  the existing `idempotency.py` `(tenant_id, key)` constraint
- [x] **J22** Wire `decision_wal.replay_dead_letter` so a poison message DLQs instead of blocking — poison messages go to a dead-letter topic **and their offset is committed**, so one bad event cannot block its partition
- [x] **J23** The e2eFlow as a real test: kill a consumer mid-flight → DLQ → redelivery does not — `tests/test_decision_outbox.py` — 18 tests, 5 against a real broker, including the row's own e2eFlow. Five mutations checked; one initially SURVIVED (see below)
  double-log. Plus the DoD five and a mutation check per guard
- [x] **J24** P09 → PRESENT with honest evidence; SSE fan-out stays on Redis — P09 PARTIAL -> PRESENT. Parity is now **PRESENT=21, CUT=1, ABSENT=0, PARTIAL=0**

**Four things the P09 work turned up that are worth keeping:**

1. **P09 was greenfield, not a migration, and that is why it took a session rather than the
   roadmap's 6+ months.** The estimate assumed `decision_bus.py` would move onto a broker. It
   would not have: that file is 113 lines of best-effort Redis pub/sub for the dashboard's SSE
   feed, one producer, one consumer, with a docstring saying publishing "is best-effort and
   never allowed to affect the decision write path". A live view is not delivery.
   `decision_log.py:19` had already named the real gap — "the durable-outbox follow-up".
2. **A mutation SURVIVED the first pass.** Stamping every outbox row sent regardless of the
   delivery report — the at-most-once bug — passed all 17 tests, because on a healthy broker
   nothing is ever rejected so "stamped after the ack" and "stamped always" are
   indistinguishable. They differ only during an outage, which is exactly when it matters.
   `test_a_rejected_row_is_NOT_stamped_sent` forces a produce failure against a dead port; the
   mutation now dies.
3. **`event_payload` read `tenant_id` off the decision dict, where it does not exist.**
   `storage._decision_to_dict` has no `tenant_id` key, so every published event carried
   `"tenant_id": None` — and the consumer dead-letters a message with no tenant, so the entire
   stream would have gone to the DLQ. Caught by the PII test asserting the field is *present*,
   which a happy-path test would never look at.
4. **`app/scheduler.py` uses `logger`, not `LOGGER`.** The new relay job referenced `LOGGER`, so
   it would have raised NameError — but only on the broker-failure path, i.e. only during an
   outage, on the code whose job is to handle one.

**Plan audit 2026-09-14 — four steps were not actually complete when first reported.** Checked
against the repo rather than memory, after being asked "everything done?":

1. **J14 (fix the hotspots)** — **reachability now complete; one non-reachability blocker left.**
   `_needed_variable_ids` is a recursive walk over rule conditions, scorecard bins,
   decision-table inputs, connector sources and nested steps in loops / branch arms /
   sub-workflows, plus variables named in STRING expressions (`over: "variables.applicants"`,
   `"app['score'] < 600"`). Failures with the flag on: **16 → 7 → 3**. Three assumptions were
   wrong and each was caught by a test, not by reading: top-level steps are not enough,
   `inputs` is a LIST not a map (reuses `decision_tables._input_index` now), and conditions are
   sometimes plain strings. The last 3 failures are a single cause — `fast_decide` still
   computes the whole bundle, so fast and slow disagree, which is the FAST_DECIDE divergence
   class fixed three times already. The flag stays OFF until that is closed.
2. **J18 (Kafka in compose AND the Helm chart)** — only compose was done. The chart now has
   `env.kafkaBootstrapServers`, rendered into the ConfigMap **only when set**, matching how
   `REDIS_URL` degrades: unset means the outbox records every event and simply does not deliver.
   Verified by rendering with and without.
3. **J23 (the DoD five)** — the audit event was missing. `decision_event_dead_lettered` is now
   written when an event is parked, with `actor_role: system:decision-consumer` stated
   explicitly rather than a null, because a consumer has no authenticated caller. Only the
   dead-letter path is audited: a processed event already has its Decision row, and auditing
   every one would double the audit table to record success. RBAC is genuinely **N/A** — P09
   adds no HTTP surface, which the OpenAPI gate confirms.
Two further things were found while closing those, and both are recorded because they were my
own mistakes:

- **The engine-cache cap (37252f1) was wrong and is reverted.** It did not fix the SQLite flake
  (no cap ~1 error, FIFO 3, LRU 4, removed 8) and introduced a bug: FIFO evicts the oldest
  engine, which is `app_main.storage`'s, so it disposed the shared engine out from under live
  tests. The real cause is **leftover temp dirs from interrupted runs** — 73 of them gave 8
  errors, clearing them gave 1 — and `scripts/run_all_tests.sh` now sweeps them.
- **I reported that cap as a fix on a single clean sample.** One green run is not evidence when
  the thing being measured is intermittent.

4. **J25 (machine-readable output)** — `flutter test --machine` and `cargo test --format json`
   were never added. Both now are; the Flutter lane counts from its JSON (163) since the prose
   line it used to parse is no longer printed.

### E. GitHub: test results, Dependabot, doc truth-up

- [x] **J25** Make runners emit machine-readable output. Only Gradle emits JUnit XML today and it — JUnit XML everywhere: `unittest-xml-reporting` for Python, vitest `junit`, Playwright `junit`+`html`. The Playwright one also fixes a real hole — `sdk-verify.yml` uploads `playwright-report/`, which could **never exist** because no `reporter:` was configured and CI defaults to `dot`
  is discarded; `playwright-report/` is in an artifact path but can never exist (no `reporter:`)
- [x] **J26** `scripts/run_all_tests.sh` → collects XML/JSON, generates formatted `TEST_RESULTS.md`. — `scripts/run_all_tests.sh` + `scripts/render_test_report.py`. Counting is per-runner, not a guessing cascade — the first version reported Rust as **0 tests** because cargo's empty doctest target prints a final `test result: ok. 0 passed` and `tail -1` took it
  Must encode `PYTHON_BIN=<venv>` and `cargo test --no-default-features`
- [x] **J27** Commit the generated `TEST_RESULTS.md` — the quota-proof half — `TEST_RESULTS.md` committed — **10 lanes green, 4,526 tests**, every number read from the runners
- [x] **J28** `.github/workflows/test-report.yml` writing `$GITHUB_STEP_SUMMARY` (zero occurrences — `.github/workflows/test-report.yml` writing `$GITHUB_STEP_SUMMARY` (**zero occurrences repo-wide** before this), artifacts uploaded `if: always()` rather than failure-only, plus `timeout-minutes` on **all 18 jobs across 6 workflows** — none had one
  repo-wide today) + README badge + `timeout-minutes` on every job in all five workflows
- [x] **J29** 22 Dependabot PRs: apply locally, run the affected lane, merge what passes, close — 2 PRs landed and closed with evidence (#144 pyo3, #168 wasm-bindgen); the other 20 annotated with status + `@dependabot rebase`. **Every one is 7-116 commits behind main** — #144's branch carried 6 Rust tests where main has 19, so merging it would have deleted the conformance suite
  what breaks with the failure recorded, mark the rest pending on Actions
- [x] **J30** Truth-up stale counts: README/CLAUDE.md say 1369/379/58, reality is **1892/462/68**; — README/CLAUDE.md corrected: 1369/379/58 -> **1913/462/68**, '0 open PRs' -> 22, plus a pointer to the generated report so the next reader does not have to trust a typed number
  CLAUDE.md says "0 open PRs" against 22. Tick 8.2-8.4, close A7-A10 as local-verified
- [x] **J31** One commit without `[skip ci]` to close I6 and test whether the quota reset — landed 2026-09-14 — this is the commit; the run result is recorded in the Blockers section

## Blockers

> **NOTHING IN THIS SECTION IS STILL OPEN (audited 2026-09-23).** Every item below is resolved:
> the Render services and account were deleted 2026-09-19 (R9.1), the Vercel GitHub App was
> revoked, the Actions billing block was routed around by moving the whole gate to a self-hosted
> runner (R7), and #123 was closed 2026-09-19 by a live run against a real key. The section is
> kept as the record of what blocked and how each cleared — but it read as a live blocker list
> for weeks after it stopped being one, which cost a re-audit every time someone opened this
> file. If you are looking for what is actually outstanding, it is the *"What this plan does
> NOT close"* list in the current round, not here.

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

**1. ~~The deployed workspace is empty~~ — RESOLVED BY REMOVAL, 2026-09-14.** This blocker asked
you to paste an API key so three skipped checks could run against the hosted instance. That
instance is gone: the free tiers moved to paid and the API already answers nothing. Rather than
pay to close a test, the hosted footprint was deleted and the same coverage moved local —
`smoke_compose.sh`, the new `smoke_k8s.sh`, and `suite_sandbox` / `suite_core` against
`127.0.0.1:8080`, which is what the harness defaulted to all along.

**What is still yours to do:** delete the Render services and database in the Render dashboard so
they stop billing. I removed every reference in the repo, but I cannot touch your account.

**2. Vercel's GitHub App still has access to this repository (I5).** The Vercel *project* that
was failing on every push is **deleted** — that was the actual source of the "still getting
vercel deployment errors" reports, and it is gone. What remains is only permission hygiene: the
GitHub App can still read the repo. Revoking it needs a GitHub sudo-mode re-auth, which needs
your 2FA, so it is yours to do:
GitHub → Settings → Applications → Installed GitHub Apps → Vercel → Revoke.

**3. GitHub Actions will not start jobs (I6). RE-TESTED 2026-09-14 — still blocked, and the
diagnosis was wrong.**

**CORRECTION: this is a BILLING BLOCK, not an exhausted quota.** GitHub's own annotation on
every failed job says so verbatim: *"The job was not started because recent account payments
have failed or your spending limit needs to be increased."* That changes what fixes it — jobs
are assigned no runner and execute `steps: 0`, so **consumption is already ~0** and trimming
workflows or waiting for a monthly reset saves nothing. The heading below is kept for the
evidence it records; the word "exhausted" in it was mine and was wrong.

This was a week-old claim, so it was checked rather than repeated. Commit `8043957` was landed
**without** `[skip ci]` specifically to find out. The result, from the API rather than from the
UI:

- **Zero workflow runs were created for that commit at all** (`actions/runs?head_sha=8043957`
  → `total_count: 0`), even though `sdk-verify.yml` triggers on push to `main`.
- Runs that *were* created — for Dependabot's rebase commits — finished in **3–4 seconds** with
  `steps: 0`. A job that executes zero steps has not failed a test; it has not run one.

So the blocker stands, and it is worse than "runs fail": pushes no longer produce runs. Local
runs remain the only signal, and `TEST_RESULTS.md` (10 lanes, 4,526 tests, generated from the
runners' own output) is what stands in for CI until minutes are available again.

**What unblocks it:** settling the billing block at `github.com/settings/billing` — *or*, at
no cost, **making the repo public**: public repositories get unlimited free GitHub-hosted
Actions minutes, and the repo already ships an Apache 2.0 LICENSE and a README that calls itself
open-source. That switch is the user's to flip, not mine. A **self-hosted runner** is the third
option and is free even on a private repo; it should be wired to `workflow_dispatch` only, never
`pull_request`, because 22 open Dependabot PRs means running bot-authored dependency code. Once a run genuinely executes, `.github/workflows/test-report.yml`
publishes a job summary, and the 20 open Dependabot PRs can be judged on a real result instead of
a red X that only means "out of minutes".


## K. Round 2 — close J14, correct two wrong claims, ship a deck — 2026-09-14

### K. J14 — `fast_decide` now agrees with the executor

- [x] **K0** Closed the fail-open in `core/engine.py` FIRST. `dict(context.get("variables") or
  payload or {})` tested truthiness, so an **empty** variables map fell back to the raw payload as
  the feature map — unvalidated caller input standing in for authored, typed variables. It was
  unreachable only because the fast path always computed the whole catalog, and the fix for the
  divergence would have made it reachable. Presence now decides. Mutation-checked: restoring the
  old expression fails `test_empty_variables_does_not_fall_back_to_payload` with
  `'approve' != 'reject'`. `backtest`, `whatif` and the governance replay all pass
  `computed_variables or {}` and were exposed to it too.
- [x] **K1/K2** Walk extracted to `app/variable_reachability.py`; the executor's methods are thin
  adapters. **One implementation, both paths** — a second copy in `fast_decide` is exactly the
  drift the module exists to prevent.
- [x] **K3/K4** Applied on the fast path. Set computed at bundle build, applied per request (the
  flag is read per request and the parity tests flip it mid-process, so a baked-in answer would be
  sticky per cache entry). `core_bundle["variables"]` stays the full catalog, matching the
  executor's unfiltered `variable_lookup`. `needed_variable_ids` is an **optional** bundle key and
  `_compute_variables`' signature is unchanged, so hand-rolled test bundles still work.
- [x] **K5** One reader for the flag, strict `== "1"` like its sibling `FAST_DECIDE`.
- [x] **K6** Two fixtures were measuring the unfiltered catalog rather than the guard they name,
  and both were fixed rather than skipped: the budget tests now run a policy that reaches 20
  variables (with a guard asserting it does), and the seeding tests got a rule that actually reads
  `seed_score`.
- [x] **K7/K8** `test_compute_only_used_vars_parity.py` runs both paths over every fast-servable
  step type, flag on and off, asserting identical outcomes **and** identical computed-variable
  keys — plus that the flag actually filters (a **non-empty** strict subset; an empty set is a
  subset of anything and would prove the reverse). That non-empty check caught a real fixture bug
  while being written. Mutation-checked in both directions separately: fast path ignores the
  filter → 3 named tests fail; executor ignores the flag → 2.
- [x] **K9** Full suite green with `COMPUTE_ONLY_USED_VARS=1`, which had never been true.

### L. Docker disk — the premise was inverted

- [x] **L1/L2** `scripts/lib/docker_disk.sh`: reads free space **from inside the VM**, fails fast
  in `k8s_up.sh`, warns in `run_all_tests.sh` (most lanes need no container). `k8s_up.sh` prunes
  dangling layers **only after a successful run** — after a failure those layers are the evidence.
- [x] **L3** Documented in `docs/PERFORMANCE.md`. **Host free space is not the number that
  matters**: this machine runs Colima with a hard 30 GiB VM allocation, so the host's 650 GB never
  participated. The last report got this wrong, and a confident irrelevant observation reads as an
  all-clear.

### M. Two of my earlier claims were wrong

- [x] **M1/M3c** **Render is LIVE, not dead.** "The API is already dead" came from ONE 30 s curl
  that timed out — exactly what a sleeping free-tier service looks like. Re-probed: 200 in 16.2 s
  cold, then 0.16 s, then 0.16 s. Corrected here and in the prose.
- [x] **M2a** `docs/PRE-PUBLICATION-AUDIT.md` — full history scanned, not just the tree. `.env`,
  `.env.local` and `.env.deployed` have **zero** add-commits in any branch; no provider-key pattern
  matches anywhere in history. The committed dev defaults are real values but fail closed in
  production.
- [x] **M2b** Repo description said **"Orchestrator"**; now describes the product.
- [x] **M2c** **Actions is a BILLING BLOCK, not an exhausted quota.** Corrected in `CLAUDE.md`,
  `README.md`, `test-report.yml` and here. It changes the fix: consumption is already ~0, so
  trimming workflows saves nothing.
- [x] **M3b** `scripts/render_teardown.sh` — deletes every service, database and key-value store
  the moment a token is exported. Token read from the environment, never written to a file, never
  in argv, never echoed. Re-probes afterwards rather than trusting a timeout.
- [x] **M4** Vercel confirmed to need the user: `gh api /user/installations` returns 403 with an
  **empty** `X-Accepted-Oauth-Scopes`, so no scope grants it and `gh auth refresh` cannot help.

### N. Pitch deck

- [x] **N1/N2/N3** `docs/deck/RuleMind-AI.pptx` — 11 slides for enterprise buyers, with
  `docs/deck/README.md` mapping every figure to where it was verified and carrying the explicit
  do-not-claim list. Slide 10 states those limits on the slide.
- [x] **N4** `CLAUDE.md` drift fixed: 18 routers → 23, `storage.py` 3515 → 4979 lines, `main.py`
  1629 → 1972, AI "dual-provider" → three (Gemini is fully implemented), and the test counts now
  point at the generated `TEST_RESULTS.md` instead of carrying their own copy.

### Also fixed: the last of the shared-global leak

25 test modules still assigned `app_main.storage` without restoring it — the cause of the
`unable to open database file` "flake", which always lands on whoever reads the global next
rather than on the module that leaked it. `tests/_storage_global.py` now holds one helper, called
via `addCleanup` so it runs AFTER the module's own tearDown. **Zero modules leak now.**

## R10. Three audits, and the altitude was wrong all along — 2026-09-24

The owner's criticism: this project has been fixing configuration symptoms without ever
evaluating the architecture, the APIs, the scalability, the product or the UX. Correct. Three
parallel audits — execution engine, API/scalability/orchestration, product/UX/integrations —
replaced guesswork with evidence. Full plan in the session plan file; the findings that reframe
everything:

**The config surface is the systemic defect.** The backend reads **111 environment variables**.
`docker-compose.yml`, the Helm chart and `k8s/` together can deliver **28**. **83 are settable by
nothing**, and **64 of those are documented in `.env.example`** — documented, read, undeliverable.
For a self-hosted product whose deployment model IS the chart, the customer can configure **16%**
of the software. `PROMETHEUS_MULTIPROC_DIR`, `MODEL_MEMORY_MB`, `RULEMIND_SELF_SERVE_SIGNUP`,
`DECISION_ARCHIVE_SINK`, `FAST_DECIDE` were never five unrelated bugs; they were five samples
from a population of 83. Among the unreachable: `DECISION_ENCRYPT_AT_REST`,
`AUDIT_ENCRYPT_AT_REST` and `LOG_LEVEL`.

**The performance story was pointed at the wrong layer.** Measured, 3 rounds, medians: as
shipped 60.3 rps / p50 124.9 ms; with both perf flags 109.9 rps; with 4 workers 140.8 rps — the
default runs at **43%** of the same code on the same hardware. But `FAST_DECIDE` contributed
**0%**, because `policy_instant_personal_loan` (the load test's default) has `action` and
`review_gate` steps and fails `fast_path_eligible` outright. The entire gain was
`COMPUTE_ONLY_USED_VARS`. And the Rust core is not merely unpackaged — it accelerates rule
evaluation, under 100 µs of a ~50 ms decision, so shipping it is worth **1–2%**. Queried live:
**1 of 4 seeded policies can use `FAST_DECIDE`, and 0 of 4 can ever reach the Rust core.**

**A compliance fabrication ships in the demo.** `apps/web/app/decision-explorer/page.tsx:36-42`
returns hardcoded `["AA05 · Debt-to-income too high", "AA12 · Insufficient credit history"]` for
every rejected decision, under a heading reading "Adverse-action reason codes". A real
deterministic implementation exists at `governance.py:213` → `logic.py:841-897` and the UI never
calls it.

- [x] **R10.1 THE COMPLIANCE FABRICATION IS GONE.** `decision-explorer/page.tsx:36-42` returned
  `["AA05 · Debt-to-income too high", "AA12 · Insufficient credit history"]` for **every**
  rejected decision and `["MR02 · Manual verification required"]` for every review, rendered
  under a heading reading "Adverse-action reason codes" — the codes a lender puts in an
  FCRA-style decline notice. The real deterministic implementation had existed the whole time
  and was called by nothing: `GET /api/v1/decisions/{id}/reason-codes` →
  `logic.adverse_reason_codes()`, which ranks failing conditions by relative miss magnitude.
  Now wired, with the fetch error surfaced rather than swallowed (an empty list and a failed
  fetch mean different things, and only one of them is "declined for no stated reason").
  **Verified in the browser**, two rejected decisions:
  `RC-LOAN-BUREAU-SCORE-LT / RC-LOAN-PAN-VERIFIED-FLAG-LT / RC-LOAN-LIVENESS-SCORE-LT` versus
  `RC-TRAVEL-PASSPORT-MONTHS-VALID-LT / RC-TRAVEL-SUPPORT-MATRIX-READY-LT / RC-TRAVEL-VISA-READY-LT`.
  8 tests pin the property the fabrication violated — two decisions failing different conditions
  must produce different codes — plus a source guard, because no Python test could have caught a
  frontend literal. Both mutations fail the named test.

- [x] **R10.2 THE AUDIT TRAIL NOW NAMES WHO.** `AuditEvent.user_id` is one of
  `audit_chain.CHAINED_FIELDS` — the tamper-evident hash has always covered it — and **no
  caller ever set it**, so every `user_id` in the product was NULL and the chain was protecting
  the absence of an answer. `routers/insights.py:73` claims the trail answers "who changed which
  rule/variable/scorecard/policy, when"; it answered what and when. Defaulted inside
  `add_audit_event` from the contextvar the middleware already populates (an API key id, or
  `member:<id>` for a session — exactly what `active_actor()` returns), rather than at the ~14
  call sites, because the next emitter added would have missed it the same way. An explicit
  actor still wins; a scheduler job with no request context still gets NULL, which is correct
  rather than a fabrication. 7 tests, including that the chain still verifies across a mix of
  pre-fix (NULL) and post-fix rows — adding a field the chain already covers is exactly the
  change that could silently fork every existing row. Mutation fails 2 named tests.

  Also found and fixed in passing: `app/__init__.py` calls `load_dotenv()`, so setting
  `OTEL_EXPORTER_OTLP_ENDPOINT` in `.env` for the soak leaked into every host-side test run and
  made the suite retry OTLP exports against a container hostname. The soak scripts export it
  themselves; `.env` no longer does.

- [x] **R10.3 A SERVER DECISION NOW CARRIES ITS POLICY VERSION.** `Decision` had no
  `policy_version` column at all, and policies are mutated in place — so "which version of this
  policy produced this decision" was answerable only by inferring it from timestamps against
  `EntityHistory`. For the regulator question eight months out, that inference *is* the answer,
  and the product did not make it. Added to the model, to an Alembic migration
  (`20260924_0031`), and populated on **both** decision paths — `PolicyExecutor` via a new
  `ExecutionContext.policy_version`, and `fast_decide` in both its result branches and in
  `_log_decision` — because a lineage field present on one path and absent on the other is the
  FAST_DECIDE divergence class this repo has now fixed five times.

  `bundle_hash` stays NULL on **both** server paths, deliberately. It means "the compiled SDK
  bundle this device decided from"; the executor never loads a bundle, and the fast path's
  in-process serving bundle is not that artifact and has no published hash. Hashing it anyway
  would put two different meanings in one column — worse than a NULL, because a NULL is
  legible. (I wrote the opposite twice while doing this — a comment in `executor.py` and a
  test docstring both said the fast path records one. It does not; the word does not appear in
  `_log_decision`. Both corrected, because a false claim in a comment reads as a guarantee.)

  Found while writing the tests: `add_decision` parsed `created_at` with
  `_parse_client_datetime`, which **returns None for anything that is not a `str`**, so any
  internal caller passing a real `datetime` silently got `utcnow()` — a replayed, imported or
  backdated decision quietly claimed to have happened just now. Now `_as_datetime`. 8 tests.

- [x] **R10.4 AN EIGHT-MONTH-OLD DECISION IS NOW FINDABLE.** `list_decisions` filtered on
  `client_id / user_id / policy_id / outcome` plus `limit/offset` and had **no date range**. The
  only ordering is `created_at DESC`, so reaching an old decision meant paging every row written
  since — and the Decision Explorer did not even do that: it fetched the default 200 and filtered
  in the browser. The screen a regulator question lands on could see the most recent 200
  decisions and nothing older. Added `created_from` / `created_to`, half-open `[from, to)` so
  adjacent ranges tile without double-counting a boundary row, at storage and at
  `/api/v1/audit/decisions`.

  Two things fixed alongside it: `limit`/`offset` were untyped, so `?limit=-1` reached Postgres
  as `LIMIT -1` (500) and SQLite as *no limit* — the unbounded fetch pagination exists to
  prevent; and the route **did not pass a tenant**, so it fell through `Storage._tenant_id`'s
  chain to `default_tenant_id`, the first tenant in the table.

  That one turned out to be **five routes, not two**. `/audit/decisions` and `/audit/errors`
  were the pair sitting directly beneath the comment in `insights.py` explaining the hazard;
  `/audit/decisions/count`, `/audit/promotions` and `/analytics/rejection-drivers` had it too —
  a promotion ledger showing one customer's release history to another, a rejection-driver
  analytic computed over someone else's decisions. Found only because the test was rewritten
  as a **sweep of the file** for any `main.storage.*` call without `tenant_id=`, rather than a
  list of the two routes I had noticed. Fixing the two and leaving three is the pattern this
  round keeps finding, and a test that enumerates what you already fixed cannot catch it.
  12 tests; mutation (drop the tenant from any one route) fails the sweep by name.

  Query params moved to the `Annotated` form so the Python default is a real int: written as
  `limit: int = Query(default=200, ...)` the default value is a `Query` object, which FastAPI
  unwraps but a direct call does not — `test_enterprise` calls the handler as a function and
  failed inside SQLAlchemy with `'<' not supported between instances of 'int' and 'Query'`.

  Worth recording because the first attempt was wrong: my initial tenancy test passed against
  the mutation, because on an authenticated request the contextvar is always set and the
  fallback never runs. The test proved nothing. Replaced with one that clears the contextvar and
  asserts the fallback serves the wrong tenant's rows, plus a source-level guard; the mutation
  then fails by name.

- [x] **R10.5 THE AUTHORING AUDIT TRAIL HAS A SCREEN.** `GET /api/v1/audit` — the change trail
  the tamper-evident hash chain protects — and `GET /api/v1/audit/verify` were called by
  **nothing** in the web app. The nav item labelled "Audit Logs" showed decisions, promotions and
  errors; the record of who changed which rule was reachable only by opening the database. Added
  a "Change History" tab to `AuditPage.tsx` rendering `event_type · entity_id`, the actor, and
  the chain-verification badge, so the control is inspectable from the product rather than
  asserted in a docstring.

  **Verified end to end against the running stack**, which is the only thing that closes R10.2:
  created a variable through the API and read the audit row back — `variable_created
  v_actor_probe · user_id=a024138c-…`, where every pre-fix row still reads `user_id=None` — with
  `/api/v1/audit/verify` returning `ok: true, verified: 16, breaks: []` across the mix of NULL
  and populated rows.

- [x] **R10.6 THE PRODUCT NOW HAS A CONFIGURATION INTERFACE.** Not a flag, a population.
  `scripts/config_surface.py` derives the surface from source — an AST walk of every module
  under `app/`, plus the container's own shell (`RULEMIND_WORKERS` is read by the Dockerfile
  CMD, so a Python-only pass reported the largest measured throughput lever as "not read").
  Measured 2026-09-24: the backend reads **111** settings; `docker-compose.yml`, the Helm chart
  and `k8s/` together could deliver **27**; **84 were settable by nothing**, 68 of them
  documented in `.env.example` — read by the code, described to the operator, deliverable by no
  artifact. Among them: `DECISION_ENCRYPT_AT_REST` and `AUDIT_ENCRYPT_AT_REST` (encryption at
  rest could not be turned on), `LOG_LEVEL` (verbosity could not be raised during an incident),
  `RATE_LIMIT_*`, `CIRCUIT_BREAKER`, `SANDBOX_FALLBACK_MODE`, `RULEMIND_SIGNING_PRIVATE_KEY`.
  For a product whose deployment model IS the chart, the customer could reach **16%** of the
  software. `PROMETHEUS_MULTIPROC_DIR`, `MODEL_MEMORY_MB`, `RULEMIND_SELF_SERVE_SIGNUP`,
  `DECISION_ARCHIVE_SINK` and `FAST_DECIDE` were never five unrelated bugs — five samples.

  Closed structurally rather than instance by instance: a `config:` / `secretConfig:`
  passthrough in the chart (real env-var names, rendered into the ConfigMap and the Secret) and
  an optional `env_file` in compose. Deliberately `.env.runtime`, **not** `.env` — compose
  already reads `.env` for `${VAR}` interpolation and that copy holds host-side values
  including an `AUTH_MODE`; injecting it into the containers would have silently disabled
  authentication on `make up`. Two roles, two files.

  `scripts/check_config_surface.py` is the gate, in the family of `check_parity.py` and
  `verify_chart.py`, and now in `run_all_tests.sh`. It asserts three things: every setting is
  **reachable** (named key or documented passthrough), **visible** (in `.env.example` and in
  `values.yaml`), and that the internal allow-list — 3 entries, each with a written reason —
  still names variables the code reads. It also asserts **the passthrough itself still
  exists**, because without that every other verdict is vacuous and the gate would pass anyway.
  Mutation-checked three ways: drop `LOG_LEVEL` from `values.yaml` → fails; drop
  `CIRCUIT_BREAKER` from `.env.example` → fails; stub out the ConfigMap `range` → fails naming
  the passthrough. Exit 1 on violation, 0 clean, both verified. 15 tests, each reader form
  separately, because a form the AST walk cannot see is a setting the gate cannot protect.

  `.env.example` shipped **`SANDBOX_FALLBACK_MODE=inline`** against a code default of `fail`
  (`sandbox.py:459`). That is the behaviour when no sandbox subprocess is available: `fail`
  refuses the variable, `inline` runs untrusted customer Python **inside the API process**. The
  file an operator copies re-enabled the exact posture the code default was changed to stop —
  silently, and only under the load that exhausts the pool. Fixed, pinned in the chart, and
  pinned by a test in both places.

  **The pod's resource envelope and its process budget now agree**, with measured numbers
  rather than assumed ones. A pod does not run `uvicornWorkers` processes: each uvicorn worker
  forks its own sandbox pool *and* its own model pool — `1 + 2 × (1 + 4 + 2) = 15` at the
  defaults. Measured inside the running container (`/proc/<pid>/statm`): uvicorn worker 118 MB,
  sandbox subprocess 27 MB, a process with the ML stack resident 120 MB — a **~975 MB
  full-pool floor**, against a `requests.memory` of **1Gi**. The pools fill lazily, which is
  why an idle pod reads 285 MB and nobody saw it; the scheduler could pack a node on the idle
  number and OOMKill it as they filled. And `requests.cpu` was **500m** for those 15 processes
  against a 2-core limit — with `hpa.cpuUtilization` being a percentage of the *request*, pods
  scaled out at 350m of a permitted 2000m, ~17% of their own ceiling. Now 1 CPU / 1536Mi
  request, 2 CPU / 3Gi limit, with the arithmetic written into `values.yaml` beside the
  connection budget it mirrors.

  Two claims corrected while doing it. `MODEL_MEMORY_MB` × workers "= 8 GB against a 2 Gi pod"
  is **wrong**: it is an `RLIMIT_AS`, an address-space cap, not an RSS budget, so it does not
  sum against the pod limit — it is merely inert, because the cgroup limit is reached first.
  And `values.yaml` claimed "xgboost/lightgbm/sklearn/**pandas** resident per worker"; pandas
  is not in the image.

- [x] **R10.7 A PERFORMANCE FLAG WAS CHANGING THE API CONTRACT.** Under `FAST_DECIDE=1` the
  `/api/v1/decide` handler returns `fast_decide()`'s dict verbatim, and that dict had **no
  `decision_id`** — the word appears nowhere in the file. The slow path has returned one since
  it existed, and it exists so an integrator can deep-link
  `GET /api/v1/decisions/{id}/reason-codes` or open the case without a second lookup. So an
  operator flipping a flag documented as a performance toggle broke every such integration
  silently, with a 200 whose body was simply missing a field. `fast_decide.py` warns about
  exactly this divergence class three times — fail-closed semantics, PII redaction, the
  `client_id` collision — each found by someone comparing the two paths. The response shape was
  the one nobody compared.

  Fixed both directions, because the first fix created the second: the id is minted in
  `fast_decide` and **handed to `_log_decision`** rather than generated there, so the id the
  caller receives names the row that was actually written (the obvious wrong fix — mint one for
  the response and let the logger mint its own — returns an id that 404s, which is worse than
  returning none). `log=False` returns `decision_id: None` rather than an id naming nothing;
  the key is always present, because a key that comes and goes is its own divergence. And the
  slow path now reports `policy_version`, which for a few minutes only the fast path did.

  7 tests comparing the two responses as SETS of keys, so a field added to one branch and not
  the other fails here. The load-bearing one is `test_both_paths_were_actually_exercised`:
  `FAST_DECIDE=1` on an ineligible policy falls through to the executor silently, so without
  that guard the file would compare the slow path with itself and pass — which is how the
  repo's own load test has produced fast-path numbers that were slow-path numbers. Three
  mutations, each failing named tests: drop the key (4), let the logger mint its own id (1),
  drop `policy_version` from the slow path (3).

- [x] **R10.8 BUNDLE SIGNING WAS BROKEN BY DEFAULT ABOVE ONE REPLICA.**
  `RULEMIND_SIGNING_PRIVATE_KEY` appeared in **no chart, manifest or compose file** — the
  Secret rendered five keys and not this one. Unset, `compiler._load_signing_keypair` generates
  a fresh RSA-2048 key **per process**, so at `replicaCount.api: 3` an SDK that fetches
  `/sdk/v1/signing-key` from one pod and `/sdk/v1/bundle` from another fails signature
  verification — and the SDK is built to *refuse* a bundle it cannot verify, so on-device
  decisioning stops rather than degrades. Every restart also invalidates a pinned key.

  The code already knew. `signing_key_is_ephemeral()` exists and has always been correct; it
  surfaced its answer **only as a JSON field on the signing-key response**, so the deployment
  that breaks is precisely the one that never reads that field. No startup warning, no
  readiness failure. That is the same shape as this round's other findings: the check existed
  and nothing consumed it.

  Now deliverable — `secretConfig:` renders it into the Secret, and the rendered multi-line PEM
  was verified to round-trip byte-for-byte and re-parse as a private key, because a key that
  renders but does not parse would fail at first use rather than at install. And now
  **fail-closed**: added to `PRODUCTION_SECRET_CHECKS`, so `RULEMIND_ENV=production` with no
  signing key refuses to boot, waivable per-secret like the other four for a single-replica
  install that means it. A blank value counts as unset — `RULEMIND_SIGNING_PRIVATE_KEY=` is a
  set-but-empty env var and `compiler.py`'s `if pem:` falls straight through to generating one,
  so a check testing only `is None` would pass the exact configuration that breaks.

  `scripts/k8s_up.sh` generates one per install alongside the four secrets it already
  generates, for the reason written there: `values-kind.yaml` sets `rulemindEnv=production`, so
  the new gate is exercised by the local kind install rather than tiptoed around. 6 tests,
  including one pinning the gate and `compiler.signing_key_is_ephemeral()` to the same
  question — if those two ever disagree about whether bundles are stably signed, the startup
  gate is the half nobody would notice drifting. Two mutations, each failing named tests.

- [x] **R10.9 THE RUST PATH WAS EMPTYING THE AUDIT RECORD, AND THE RUST CORE WAS SKIPPING NINE
  STEP TYPES.** Two bugs, one root: an accelerator that answers a narrower question than the
  thing it accelerates, and says nothing about the difference.

  **The audit record.** `fast_decide` had two branches. When the Rust extension was importable
  and the policy carried no scorecard or decision table, it served from
  `rulemind_core_rs.Bundle` and returned `score: None, rule_results: [], trace: []`
  **hardcoded**, while the Python branch one line below returned all three populated.
  `trace` + `rule_results` are ~66% of the stored Decision row and they are the whole answer to
  "why was this rejected" — so a decision taken through that branch was recorded as
  **unexplainable**: same outcome, no reasons, nothing noting the reasons were dropped. It was
  **live**: the Rust extension is built into this repo's venv, so `_HAVE_RUST` was true on
  every host test run and any rules-only policy took it.

  Removed the branch rather than populating it, and the reasoning is worth keeping because it
  contradicts what the README implies. Populating it means reconstructing the step-level trace
  — an entry per `outcome` step carrying the running merged outcome — which needs
  `_merge_outcome` re-implemented on the Python side: a second copy of merge semantics, the
  exact drift the five-engine parity contract exists to stop. Taking the branch only when no
  trace is needed is the same thing stated honestly, because on this path a trace is *always*
  needed (logged, it is the record; unlogged, it is the output). And the cost of dropping it is
  small in a way the docs do not say: the Rust core replaces **rule-tree evaluation**, under
  100 µs of a ~50 ms decision, so its ceiling is **1–2%** — and queried against the live API,
  **zero of four seeded policies could ever reach it**. A path that served no shipped policy,
  could not explain the decisions it did serve, and was worth one or two percent.

  **The step skipping.** `CompiledBundle::steps` matched `rule` and `outcome` and sent
  everything else to `_ => {}` — `connector`, `scorecard`, `decision_table`, `action`,
  `review_gate`, `loop`, `branch`, `workflow`, `model`: nine of eleven, skipped with no error
  and no signal. **A policy gated by a `review_gate` returned APPROVE with the gate never
  executed**, HTTP 200. `fast_decide.py` documents this precise failure as the reason the
  Python fast path *declines* such policies; the Rust core ignored their steps instead. Now
  `EvalError::UnsupportedStep(type)` — refuse, naming the step. `connector` stays a no-op and
  is now an explicit arm rather than a fall-through, because a compiled bundle already carries
  the values and there is nothing to fetch. In `rulemind-decide-service` an evaluation error is
  already a 422, so the wrong answer became a named refusal with no service change.

  **This is a parity restoration, not just a fix.** Checked against the other four engines
  afterwards: the Python core fails closed on an unknown step (`core/engine.py:296-307` — "the
  steps that actually ran are not the policy"), Kotlin returns `"Unknown step type: ..."`
  (`PolicyExecutor.kt:327`), and Dart does the same (`policy_executor.dart:354`, with a comment
  recording that it *used* to mark the step skipped). **Rust was the only one of five that
  silently skipped**, and it is the one no lane executes end to end — the five-engine contract
  held everywhere it was tested.

  The extension was **rebuilt into the venv** (`maturin develop --release`), because otherwise
  the Python suite would have gone on testing the old compiled `.so` — a green run against a
  binary that no longer matches its source is this project's recurring failure in a new place.
  8 Python tests asserting the RECORD (a rejection carries the rule, the failing condition, its
  threshold and value; the stored row matches the response; `adverse_reason_codes` can still be
  derived from it) — deliberately about the record rather than the branch, so they would have
  failed on the old code without naming Rust. Mutation: re-empty the three fields → 5 named
  failures. 5 Rust tests + 1 decide-service test; mutation: restore `_ => {}` → 3 named
  failures. Rust suite 16 + 8, decide-service 11, all green.

- [x] **R10.10 THE LOAD TEST HAD NEVER EXERCISED THE PATH IT BENCHMARKED.** `qa/loadtest.py`
  defaults `RM_POLICY` to `policy_instant_personal_loan`, whose steps are
  `action, connector, outcome, review_gate, rule, scorecard`. `action` and `review_gate` are
  both in `_IO_STEP_TYPES`, so that policy is **permanently** fast-path ineligible — and
  `FAST_DECIDE=1` does not error on an ineligible policy, it falls through to the full executor
  silently with a 200. **Every fast-path number this repository has ever produced through its
  own load test was a slow-path number**, produced with the flag on and the flag doing nothing.
  Same failure as a test that passes without asserting: the harness reported success at the
  thing it was not doing.

  The harness now probes the policy's shape over HTTP and **refuses to run** on an ineligible
  one, naming the blocking steps, with `RM_REQUIRE_FAST=0` to opt into slow-path numbers
  explicitly. Run against the live API it now prints
  *"REFUSING TO RUN: policy 'policy_instant_personal_loan' can NEVER take the fast path — it
  contains action, review_gate."* Confirmed against all four seeded policies, and the answer
  matches the table in the round's plan exactly: **`policy_pl_underwriting` eligible, the other
  three blocked by `action, review_gate`.**

  The step-type list is duplicated in the harness on purpose — it talks HTTP and must not
  import the server it measures — so a test pins the copy to `fast_decide._IO_STEP_TYPES`. A
  duplicate that drifts is a guard that has stopped guarding, which is this round's theme.
  8 tests, including that an unreadable policy answers UNKNOWN rather than eligible: a probe
  that fails open is how the original defect returns through the error path.

- [x] **R10.11 README RETRACTION.** `README.md` advertised a `FAST_DECIDE=1` + Rust core tier at
  "high hundreds to low thousands/s". The row is **deleted**, not softened, because three
  separate things make it unreachable: one of four seeded policies is fast-path eligible and
  **zero of four** could ever reach the Rust core (it additionally requires no scorecard and no
  decision table); the Rust serving branch was removed outright in R10.9; and the ceiling was
  1–2% regardless, because the core accelerates rule-tree evaluation — under 100 µs of a ~50 ms
  decision, which `simulation/LOAD_TEST_RESULTS.md:37-41` had already measured as not the
  bottleneck. Replaced with what actually moves the number: variable computation (~37%, the
  subprocess sandbox) and the worker count — 60.3 rps as shipped, 109.9 with
  `COMPUTE_ONLY_USED_VARS=1`, 140.8 with 4 workers, measured three rounds on a 4-core box.

- [x] **R10.12 THE THREE FREE WINS, MEASURED — TWO TAKEN, ONE DEFERRED WITH A NUMBER.**

  **1. The sandbox re-prepared every variable on every call.** `_execute_in_process` re-ran
  `validate_source`, `exec(code, ...)` and `inspect.signature` on **each** invocation — 55 times
  per decision on the seeded tenant — while `_execute_inline`, one function below, cached
  exactly that. `SANDBOX_MODE=pool` is the default, so **production traffic took the uncached
  path and the cached path was the one nobody ran.** Measured in the API image, 2000
  iterations: preparation **33.8 µs** against **0.3 µs** for the variable's own body — the
  preparation was **113× the computation**. Broken down: `compile` 6.7 µs, `inspect.signature`
  2.4 µs, `exec` + resolve 1.6 µs, validation 0.3 µs.

  The fix has a trap and the trap is the interesting part. Caching the resolved **function** —
  which the inline path does — also caches its `__globals__`, and the cache key is a source
  hash, so two tenants submitting byte-identical variable source share any module-level state
  that code mutates. That is a cross-tenant data channel introduced by a performance fix, in
  the default execution mode. So only the **code object** is cached: immutable, stateless,
  safe to share; the namespace is rebuilt per call. 1.5 µs of the saving is deliberately left
  on the table as the price of isolation.

  Confirmed **end to end through the real pool**, not only in the microbenchmark, because the
  IPC round trip could have swallowed the whole saving: 200 calls × 5 runs × 3 processes,
  medians **0.094–0.097 ms cached against 0.122–0.130 ms uncached** — ~0.030 ms per variable,
  ~**1.7 ms per decision** over a 55-variable catalog, agreeing with the microbenchmark.
  Worth saying that the FIRST measurement of this was a single sample per side and reported
  the cache as *slower* (0.316 vs 0.183 ms); it was noise on a machine running the soak, and
  it is the same mistake this file already records once ("reported as fixed on a single clean
  sample, which is not evidence for an intermittent failure"). Repeats, or it is not a
  measurement. 6 tests;
  two mutations — remove the cache (fails `test_the_source_is_compiled_once_and_reused`), and
  cache the function instead (fails `test_module_level_state_does_not_leak_between_calls`,
  which is the point).

  **2. `list_experiments` ran uncached on every decision** — inside `fast_path_eligible`, so
  it was paid even when the policy was ineligible and `FAST_DECIDE` did nothing at all. Every
  other catalog read on that path already went through the epoch+TTL cache. Now cached as
  `running_experiment_policy_ids`, **with the writers invalidating at the storage layer** —
  `create_or_update_experiment` and `delete_experiment`, not the router, because the
  scheduler's auto-rollback writes experiment status too and a cache correct only for HTTP
  callers is not correct. Without that invalidation the cache reintroduces the A/B assignment
  bug this check exists to prevent: for the TTL after an experiment starts, the fast path
  serves it with no variant assigned. 7 tests; mutation (drop the invalidation) fails 2 named.

  **3. The three uncached SELECTs — measured and DEFERRED, deliberately.**
  `list_rules` / `list_scorecards` / `list_decision_tables` run per decision at
  `executor.py:1330-1333`. Timed against the live Postgres, 200 iterations each:
  **0.506 + 0.398 + 0.292 = 1.196 ms per decision**, about 2.4% of a ~50 ms decision.

  Not taken, and the reason is the ratio rather than the number: caching them means **nine**
  writer methods must invalidate, and a single missed one serves decisions against a stale
  rule — silently, and only for the tenant who just edited it. This repo already has the
  worked example of that failure mode (the storage-global fix that needed all 41 call sites
  and was wrong until it had them). 2.4% is not worth a nine-site hand-edit with that blast
  radius, and plan item 4.1 puts the profile before the optimisation for exactly this reason.
  Recorded with its measurement so the next person decides from a number rather than an
  intuition.

- [x] **R10.13 PROFILED THE 63% NOBODY HAD MEASURED.** The only real profile in this repo was
  "37% in the sandbox". The rest of a decision — middleware, the per-decision `asyncio.run()`,
  the SQL, serialization, encryption, the outbox — had never been broken down, so every
  optimisation proposed for it was a guess. `cProfile` inside the running API container, warm
  caches, 20 decisions, two policy shapes side by side. Full table in `docs/PERFORMANCE.md`
  under "The other 63%".

  | | `instant_personal_loan` | `pl_underwriting` |
  |---|---:|---:|
  | warm wall clock | **15.5 ms** | **13.5 ms** |
  | sandbox IPC wait | **29.8%** · 165 lock acquires | **35.6%** · 165 |
  | **deepcopy** | **14.6%** · **7,079 calls** | **10.3%** · **3,691** |
  | Postgres execute + commit | 13.8% · **14 stmts + 7** | 11.4% · **10 + 5** |
  | `dataclasses.asdict` | 2.2% | 0% |
  | JSON | 1.2% | 1.1% |
  | Fernet | **0.3%** | 0.3% |

  Four things fall out, and three of them contradict something written down.

  **`deepcopy` is the second-largest cost and appears in no document, comment or plan.** 7,079
  calls per decision, not one call site: the ORM-to-dict conversions each deep-copy their JSON
  columns (`_variable_to_dict` × 55, `_rule_to_dict` × 39, `_connector_to_dict` × 18,
  `_scorecard_to_dict` × 8), plus `ExecutionContext.to_dict()`'s `asdict(self)`, which
  deep-copies the **entire** context — payload, all 55 computed variables, rule results and
  trace — on any policy that persists. That is the whole 2.2% / 0% asdict split:
  `pl_underwriting` never pauses, so it never persists, so it never pays it.

  **Two suspected costs are not costs.** The plan named "4 Fernet encrypts" and "13.2 KB of
  JSON" among the unmeasured 63%. Fernet is **0.3%**; JSON is **1.2%**. Neither is worth
  touching — which is the entire argument for profiling before optimising, made against my own
  earlier list.

  **The query count in the docs was stale in both directions.** `docs/runbooks/production.md`
  said **6**, from a measurement that was correct when taken and went stale when execution
  persistence and the decision outbox landed; before that it said "8+N" from a comment's guess.
  Now 10 + 5, or 14 + 7 with a `review_gate`. Corrected in both documents, with the instruction
  to treat any query count in prose as a date-stamped observation rather than a property of the
  system — this is the third time this one line has been wrong.

  **The sandbox is the headline, and it is the IPC, not the work.** 30–36% is spent *waiting on
  a lock* — 165 acquires for 55 variables, three per round trip — while the variable bodies
  themselves measure 0.3 µs. So the lever is the variable **protocol** (batch independent
  variables; send only what the callee declares it reads — `variable_reachability` already
  computes that), not making variables faster. They are already fast. That is plan item 4.4,
  and it now has a measurement behind it instead of an intuition.

  **38% is unaccounted, and that is stated rather than hidden.** Spread across 843 functions
  with no single contributor above 1%. There is no large win hiding in it, which is itself a
  finding: the remaining levers are the four above.

- [x] **R10.14 THE VARIABLE PROTOCOL — TWO HYPOTHESES MEASURED WRONG, ONE SCOPED.** Plan item
  4.4 listed three ways to attack the 30–36% spent waiting on sandbox round trips, "cheapest
  first". Two were built, measured and **reverted**. Writing that down because a negative
  result nobody records gets re-attempted. Full detail in `docs/PERFORMANCE.md`.

  **1. "Send only what the callee declares it reads" — the O(n²) pickling. Worth ~5% of one
  round trip.** A trip costs **0.130 ms with an empty values map, 0.136 ms carrying 55 values,
  0.133 ms carrying 55 × 200-character strings**. The plan called this "the 37%"; it is
  0.006 ms. The cost is the trip, not the payload.

  **2. Dispatch independent variables concurrently. Built, measured, REVERTED — slower.** The
  premise held: a variable can be reordered only if it never reads the accumulated map, which
  is statically decidable, and **55 of 55 seeded variables are independent** — the loop was
  keeping one of `SANDBOX_POOL_SIZE` workers busy for no reason. But end to end it went
  **15.5 → 18.3 ms**, and isolated: 55 sequential **6.97 ms**, waves of 4 with a thread pool
  per wave **8.28 ms**, waves of 4 with a shared thread pool **5.90 ms**. A shared pool is
  **15% better, not 4×**. `ProcessPoolExecutor` serialises every submit and every result
  through one queue and one management thread, and the per-variable work is 0.3 µs — the
  bottleneck is the queue, not the workers. Concurrency in front of a serial queue buys only
  overlap of the pickling.

  Reverted rather than kept at 15%, because it was a net regression as written and the honest
  version (one shared thread pool, held across decisions) trades a real 15% for threads fanning
  out over the untrusted-code path. Not a trade to make at the tail of a session, and not one
  worth making at all once hypothesis 3 is on the table.

  **3. One round trip for the whole group, executed inside the worker.** The only design that
  attacks the actual cost: by parts, ~0.13 ms for the trip plus 55 × ~3 µs in-worker with
  compilation already cached ≈ **0.35 ms against 6.97 ms — ~6.6 ms off a 15.5 ms decision,
  ~40%**. Estimated by parts and **explicitly not measured end to end**, because measuring it
  means building it.

  Not built, and the constraint list is why — each is a guarantee the per-call design gets for
  free: per-variable timeout with pool recycle (one trip means one deadline; restoring
  granularity needs `signal.setitimer` inside the worker, which is new security-boundary code);
  error isolation across the group; budget abandonment, which requires pool-sized waves rather
  than one dispatch (the first attempt sent the whole catalog at once and the budget suite went
  from 504 to 200 — the tests were right); and fail-closed on a broken pool per item.

  **Two process notes worth more than the result.** The first end-to-end comparison of
  hypothesis 2 was a single sample per side and said the cache in R10.12 was *slower*; repeats
  said the opposite. And the first *design* for hypothesis 2 replaced `execute_variable` with a
  new batch primitive in `sandbox.py`, which would have re-implemented the timeout, the pool
  recycle and the fallback with group semantics — 13 test call sites patch that function
  precisely because it is the seam those guarantees live on. Fanning out the existing call
  instead kept every guarantee and made the revert a one-line `git checkout`.

- [x] **R10.15 `COMPUTE_ONLY_USED_VARS` IS NOW A PER-TENANT DECISION, AND THE "ONE READER" HAD
  A SECOND READER.** The flag restricts a decision to the variables the policy can transitively
  reach. It is worth the entire measured **+82%**, and it is off by default *not* for
  performance reasons: with it on, `computed_variables` records what the policy read rather than
  the tenant's whole catalog. That is a choice about your **audit surface**. A regulated lender
  reconstructing declines from the recorded set wants everything; a high-volume tenant scoring
  thin-file applicants wants the latency. Same install, different answers — which a
  process-wide environment variable cannot express.

  Now `engine_config.compute_only_used_vars` (PUT /api/v1/settings) wins in **both**
  directions, `COMPUTE_ONLY_USED_VARS` is the install-wide default, and off is the default of
  that. Documented in `.env.example` beside the other per-tenant engine_config blocks and in
  `values.yaml` where the env default is set. A tenant value that is not exactly `true` or
  `"1"` leaves the default in place, so a typo cannot silently shrink someone's audit record.

  **And the divergence guard had a bypass.** `variable_reachability.compute_only_used_vars()`
  carries a docstring stating it is the ONE reader, "because both decision paths ask this
  question and two different answers to 'is the flag on' is its own divergence" — while
  `PolicyExecutor.execute` called `os.getenv("COMPUTE_ONLY_USED_VARS")` directly and never
  reached it. The guard existed; one of its two callers did not use it. No behavioural test
  could have caught that, because with only the env var in play both readings agree — they
  diverge only once a per-tenant value exists, which is exactly what this change adds. Pinned
  by a source check on both modules instead.

  10 tests, including that a null tenant value falls through rather than meaning off (a UI
  sends `null` for "not set", and reading it as False would override an install that
  deliberately turned the flag on). Three mutations, each failing named tests: ignore the
  tenant setting (2), read the env directly in the executor (2), accept a truthy junk value (7).

- [x] **R10.16 THE PyO3 SPIKE (4.5) IS ANSWERED BY THE PROFILE: NO.** Item 4.5 proposed
  exposing `decide_from_payload` through PyO3 so the Rust `variables::compute_variables`
  register machine could replace subprocess round trips, gated on 4.1's profile. The profile is
  in, and it closes the question rather than opening it: the cost is **0.13 ms per round trip
  through the pool's serial queue**, and the variable bodies themselves are **0.3 µs**.
  Replacing 0.3 µs of Python with Rust cannot touch a 0.13 ms transport. The Rust register
  machine would have to replace the *transport* too — i.e. run all variables in-process, which
  removes the subprocess isolation boundary that exists to contain untrusted customer code.
  That is not an optimisation, it is a different security posture. Closed, with the measurement
  as the reason.

- [x] **R10.17 A PRODUCTION INSTALL SHIPPED NO API CONTRACT, AND THE ONE DOCUMENT IT DID SHIP
  WAS WRONG ABOUT 13 OF THE 17 ENDPOINTS IT NAMED.** `/openapi.json`, `/docs` and `/redoc` are
  all disabled when `RULEMIND_ENV=production` (`main._docs_enabled` — correctly, they were
  served **unauthenticated**, handing the complete endpoint map to anyone) and the Helm chart
  sets exactly that. So the machine-readable contract was unreachable and `API.md` was the only
  thing a self-hosted customer got.

  Measured against the route table: **278 endpoints exist, `API.md` documented 17, and 13 of
  those 17 did not exist.** Coverage of real endpoints: **4 of 278, about 1%.** And it was not
  merely thin — it was wrong about *paths*: `GET`/`POST /api/v1/api-keys` (really
  `/api/v1/access/keys`), `/api/v1/approvals` and `/approvals/:id/approve|reject` (really
  `/api/v1/reviews` and `/reviews/{task_id}/decide`), `POST /api/v1/rules/validate` and
  `/rules/:id/evaluate` (really `POST /api/v1/rules/{rule_id}/test`),
  `GET /api/v1/dashboard/summary` (never existed), four collections that do not exist
  (`segments`, `ab-tests`, `exports`, `outcome-imports`), and `/docs/json` as the spec's
  location, which it has never been. An integration team following the shipped documentation
  gets 404s on nine concrete endpoints.

  **`GET /api/v1/openapi.json`, authenticated.** Removing the spec was the right fix for the
  wrong reason: the problem was that it was *public*, not that it existed. This route is not in
  `EXEMPT_PATHS`, so it needs an API key like every other `/api/v1/` route — the customer's
  integration team can generate a client, an unauthenticated scanner still gets nothing.
  Verified in a production-mode app: `/openapi.json` → 404, `/api/v1/openapi.json` → 401
  without a key, **200 with one, 211 paths / 279 operations**. Its own limitation is stated in
  the docstring rather than left to be discovered: every operation is `-> Dict[str, Any]`, so
  there are **279 operations and zero typed 2xx schemas** (plan item 5.1).

  `API.md` rewritten to be true, and `scripts/check_api_docs.py` is the gate — the fourth in
  the `check_parity` / `verify_chart` / `check_config_surface` family, all derived from source.
  It checks verb+path claims *and* bare collection paths, because the four phantom collections
  were named without a verb and a verb-only check would have passed the document with them in
  it. Corrections about the old wrong paths live in **blockquotes**, which the gate treats as
  notes rather than claims — otherwise the gate fails on the correction itself, and deleting
  the note would throw away the part that helps someone already integrated against the wrong
  path. Mutations: a phantom endpoint, a phantom collection, and a wrong verb on a real path
  — each caught.

- [x] **R10.18 ONE ERROR ENVELOPE.** Three incompatible shapes: `middleware.py` returned
  `{"error", "detail"}` and sometimes only `{"error"}` — with a *human sentence* in the `error`
  field; every route handler raised `HTTPException`, which FastAPI renders as `{"detail"}` with
  no `error` at all; and request-body validation produced `{"detail": [ …list… ]}`. On top of
  that `detail` is a string at 187 call sites and a **dict** at 3. A client had to sniff which
  field was present, then sniff the type of `detail`.

  **The fix is additive, and that is the whole design.** The obvious "normalise `detail` to a
  string" destroys the three structured errors — and those three are the *good* ones:
  `invalid_input` carries per-field `issues`, the most useful error this product produces, and
  `idempotency_key_reused` carries the clashing key. Seven existing tests and any integrator
  depend on that shape. So `error` is guaranteed present and guaranteed a string — the field to
  switch on — and `detail` keeps whatever shape the handler chose, as context. A structured
  detail that already names its own code keeps it, so the envelope never disagrees with the
  body it wraps.

  Registered on **Starlette's** `HTTPException`, not FastAPI's subclass: 404 for an unmatched
  route and 405 for a wrong method are raised by the router and never reach the subclass, so
  the subclass registration would have left exactly the errors a client hits first without an
  `error` field. The envelope lives in a new `app/errors.py` because both `main.py` and
  `middleware.py` must produce it and main imports middleware — a helper in either is a cycle
  or a duplicate, and a duplicate is how the shapes diverged to begin with.

  **The first version of the envelope was wrong for mTLS, and the mTLS suite caught it.** Its
  refusal reasons are already codes — `client_cert_missing`, `client_cert_expired`,
  `client_cert_not_allowed`, `client_cert_unparseable`, `proxy_verify_failed` — and v1 put the
  generic `unauthorized` in `error` with the code in `detail`, which made a missing certificate
  indistinguishable from a missing API key for a client switching on `error`. Now passed as a
  structured detail so the specific code survives, with the five reasons pinned by a test.

  11 tests. One of them found a live defect while being written: the 401 returned
  `{"error": "Missing API key"}` — a sentence where the machine-readable code belongs, and no
  `detail` at all. All 11 middleware responses now go through the envelope. Three mutations,
  each failing named tests: drop the handler (3), put a sentence back in `error` (1), let a
  structured detail lose its own code (1). Plus a test asserting every 4xx/5xx status the
  routers actually raise has a name in the vocabulary, so a new status cannot silently degrade
  to the generic `"error"`.

- [x] **R10.19 THE TENANT FALLBACK NO LONGER SERVES THE FIRST TENANT'S DATA TO WHOEVER ASKS.**
  `Storage._tenant_id` resolved `tenant_id or contextvar or payload_tenant or
  default_tenant_id` — and that last link is **the first tenant in the table**. Any path that
  lost the contextvar — a background thread, a `ThreadPoolExecutor` worker, an `asyncio.run`
  outside a request, a route that forgot to pass one — resolved to somebody else's data and
  read **or wrote** it, silently. `routers/insights.py` carries a comment documenting exactly
  this disclosure mode, and five of its own routes fell through it (R10.4).
  `main.active_tenant_id` was made fail-closed for the same reason; the layer underneath it
  was not.

  **It cannot simply raise**, which is why this sat open: the fallback is genuinely correct for
  a single-tenant install, and removing it breaks seeding, the scheduler, WAL replay and every
  dev box. So the condition is the honest one — the fallback is right when there is exactly
  **one** tenant and is a cross-tenant disclosure the moment there are two. `_only_tenant()`
  answers that, cached one-way (an install cannot go back to single-tenant without a deletion,
  so only `create_tenant` invalidates), and a failed count returns the old behaviour rather
  than turning a transient DB error into a tenant-context error.

  Single-tenant deployments are untouched. Multi-tenant ones now fail loudly at the point of
  the bug instead of quietly returning the wrong customer's rows.

  The test that pinned the old behaviour was written with the door open — *"if this is now an
  error, that is an improvement and this test should assert the raise"*. It did, and it does.
  Three tests now: the refusal, the single-tenant path still working, and that creating a
  tenant closes the fallback **immediately** (a stale cache after the second tenant is created
  is the original bug with an extra step). Two mutations, each failing named tests.

  **And it immediately found a real one.** The full suite came back with 7 errors in
  `test_sweep_mapping_typo_validation`, a file with **no setUp at all** — it called
  `validate_policy_steps` as a plain function against whatever `app_main.storage` the previous
  test module happened to leave behind, and `main.current_rule_map()` calls `list_rules()` with
  no tenant. It passed only because the fallback quietly picked the first tenant of a shared,
  multi-tenant storage. Closing the fallback turned a silent dependency on global state into
  seven loud errors, which is the whole argument for the change. Fixed with the file's own
  house pattern (isolated storage + the contextvar the middleware would have set), not by
  weakening the guard.

  This is the *minimum* from plan item 5.5, not the structural fix. Postgres row-level security
  is still the right answer for 613 hand-written tenant predicates, and it is a schema change.

- [x] **R10.20 A PAUSED EXECUTION THAT NOTHING RESUMES WAS UNDISCOVERABLE.** A multi-step policy
  that pauses — on a `review_gate`, or for an SDK client resuming at the edge — writes a durable
  `workflow_executions` row. The only ways to reach one were `GET /sdk/v1/executions/{id}` and
  its resume twin, **both of which need the id you are trying to find**. There was no list, no
  count, no screen and no sweeper. So an execution that paused and was never resumed was not
  merely un-resumed, it could not be discovered at all — "nothing ever resumes it" was not even
  an observable claim.

  Added `GET /api/v1/executions` (filter by status / policy / `paused_before`, tenant-scoped,
  **context omitted** because it carries the whole payload and every computed variable — 200 of
  them is megabytes of PII in a list view), `GET /api/v1/executions/stuck`, and a leader-gated
  hourly sweep that files an error event naming the count and the oldest, so they appear in
  `/api/v1/audit/errors`.

  Two design calls, both tested. **"Stuck" excludes anything with an open review task** — those
  are waiting, not stuck, and `check_review_timeouts` already owns them; counting them would make
  the signal noise. **The sweep reports and does not expire**: a paused execution is a customer's
  application mid-underwrite, and a sweeper that guessed wrong about "orphaned" would abandon a
  live case. `EXECUTION_SWEEP_EXPIRE=1` opts in; off by default for the same reason
  `DECISION_ARCHIVE_SINK` defaults to `none`. The three new settings were caught by
  `check_config_surface.py` the moment they were added and documented in `.env.example` and
  `values.yaml` — the gate doing its job on its first real use.

  **A mutation survived, and it found a duplicate.** The sweep and the `/stuck` endpoint each
  had their own copy of the awaiting-review filter. Breaking the sweep's copy passed the whole
  suite, because the only test of that rule went through the endpoint and exercised the *other*
  copy. Collapsed into one `find_stuck_executions()`, plus a sweep-side test and a source check
  that the filter is not re-copied. Re-run: all three mutations now fail named tests (expiry on
  by default; the list leaking context; the shared filter counting awaiting executions — 2
  tests, one per caller).

  No Temporal, per the plan: this is built on the DB-lease leader election and error-event
  machinery already in the repo, and adds no cluster dependency to a self-hosted product.

- [x] **R10.21 THREE POLICY EDITORS → A CANVAS AND A PER-POLICY VIEW.** Your decision: keep
  **Workflow Builder** (the drag-and-drop canvas) and **Policies** (the per-policy view), and
  solve Build Studio entirely inside Workflow Builder *if a thorough evaluation said it could be*.
  It could. All three saved to the same `PUT /api/v1/policies/{id}` — one entity, three UIs —
  because Studio was written as *"a single canvas-first surface that replaces the split
  Policies-step-list / Workflow-Builder editors"*, shipped, and the editor it replaced was never
  removed.

  **The evaluation, line by line.** Studio could do nine things Workflow Builder could not —
  and two of its features were silently broken:

  | Studio had | Workflow Builder before | Now |
  |---|---|---|
  | create a policy (`+ New`, POST) and rename it | **could only edit an existing policy** | ported |
  | author a rule inline and link it | no | ported (and fixed — below) |
  | reorder with ↑/↓ buttons | drag only — **impossible from a keyboard** | ported |
  | review-gate settings | **no editor at all**, not even JSON | *expanded* to every key the executor reads |
  | referenced rule/scorecard shown by name | raw `ref_id` | ported |
  | environment on the picker ("(PROD)") | name only | ported |
  | the late-load guard (`+ New` not clobbered) | n/a — could not create | ported, or creating reintroduces the bug |
  | resilient loading | `Promise.all` — one failed GET blanked the page | ported |
  | grouped palette | flat list of 13 | ported |
  | **branch "condition"** | real `BranchEditor` | **not carried over — broken** |
  | **loop editor** | real `LoopEditor` with a body | **not carried over — broken** |

  The two broken ones, verified against the executor rather than read off the UI: Studio's
  branch field wrote `config.condition` on the branch step, but `executor._execute_branch` routes
  on `config.branches[].condition` and `config.default`, and Studio had no way to add a branch —
  **so a branch built in Studio routed nothing.** Its loop editor had no way to add body steps,
  **so a loop built there iterated nothing.** Both looked configured.

  Also added while there: the sub-workflow step is a policy **picker** instead of a typed id (a
  typo was only discovered at decision time, as a fail-closed "unreachable child"), excluding the
  current policy, which is the cycle `_visited_workflows` exists to catch.

  `/studio` is now a real `redirect()` — bookmarks and links land on a working canvas, not a 404.
  Workflow Builder took Studio's nav slot. **Policies stays** as the per-policy view with Run and
  Promote; for the six step types it cannot edit (`branch`, `loop`, `workflow`,
  `decision_table`, `model`, `monitor`) it shows **"Edit on the canvas ↗"**, and both pages honour
  `?policy=<id>` so each opens the other *on the same policy*. It still saves those steps
  unchanged — verified that `update_policy` skips `None`, so its partial body cannot wipe
  `trigger` or `defaultOutcome` (a suspected data-loss path that turned out not to be one).

  **A real bug, inherited from Studio, found by the retargeted tests.** The inline rule editor
  said **"Rule created and linked." before it linked**: it set the message, reloaded seven lists,
  and only then patched `ref_id` onto the step. A Save in that window stored a rule step pointing
  at nothing. Now it links synchronously and confirms after. **And the first mutation check of
  that test passed with the bug restored** — it reproduced on a cold run and hid on a warm one,
  because the reload beat the click. Made deterministic by slowing the rules GET; the mutation
  now fails.

  Workflow Builder had **no behavioural browser coverage at all** before this — only
  route-health rendered it. Studio's four e2e tests (status preserved on save, confirmation
  survives reload, `+ New` survives the late load, MECE disabled until saved) are retargeted
  rather than deleted, plus seven for what the merge added and what it refused to carry over:
  creating a policy, keyboard reorder, inline rule linking, review-gate settings reaching the
  saved policy, **a branch saved in the shape the executor routes on**, `/studio` redirecting,
  and Policies opening the canvas on the right policy. **11 of 11.** Three mutations, each
  caught: the late-load guard removed, a partial save body, and Studio's link ordering.

- [x] **R10.22 A CORRECTION: THE `/rules` TIMEOUT WAS MINE, NOT PRE-EXISTING.** R10.19's commit
  (`7a199af`) says `/rules` had been failing on `waitUntil: "networkidle"` — *"pre-existing,
  verified by stashing every change: a `next dev` first-visit compile of a 1400-line page blew
  the 60s budget."* **That is wrong.** The timeout was caused by the heading capture I added in
  the same change: `/rules` has **no `h1` or `h2` at all**, and a Playwright locator's
  `innerText()` auto-waits for its element for the *whole* test timeout — the `.catch` only ran
  after it. The stash check was invalid because it covered the app files and not the spec, which
  is where the line was. The `test.slow()` added on that false diagnosis is removed (it made
  every genuine hang take three minutes to report); the `domcontentloaded` + best-effort-idle
  change is kept on its own merits, with its comment rewritten to say what actually happened.

  **And the mistake surfaced a real defect.** `/rules` was the only route in the app with no
  heading — no outline and nothing to jump to for a screen reader. It now has a visually hidden
  `h1`, the same fix the two full-height canvases use, and route-health gained **"every route has
  a heading"** (mutation: remove it → fails). route-health went from 42/43 in ~4 minutes to
  **44/44 in one minute.**

- [x] **R10.23 EVERY COLLECTION PAGES, THE SAME WAY.** Measured from the routing table rather than
  grepped (a regex over the routers reported **0** paginated endpoints because it could not read
  multi-line signatures — the app's own route objects said 4): of the **30 GET endpoints that
  return a list, 4 paged** (`/audit`, `/audit/decisions`, `/audit/errors`, `/executions`, all in
  SQL) and **26 returned a bare, unbounded array** — `/policies`, `/rules`, `/variables`,
  `/scorecards`, `/reviews`, `/decision-tables`, `/schedules/{id}/history`, `/bundles/versions`
  among them. No way to ask for less, learn how many, or walk a large collection.

  `app/pagination.py` applies one contract at the route level, installed after every router so
  it walks the routing table as it stands: `?limit=` (1–1000), `?offset=`, `X-Total-Count`, and
  RFC 8288 `Link` `rel="next"`/`"prev"`. Done once rather than in 26 handlers because 26 copies of
  the same bounds and headers would drift, and because it **covers the endpoint someone adds next
  month** — the test enumerates the same table, so there is nothing to remember to extend.

  Three properties, each mutation-checked:
  * **Opt-in, so nothing changes for existing clients.** No parameters → the exact array as before,
    no new headers. The dashboard's pickers load full lists and five SDKs expect an array.
  * **The four self-paging endpoints are left alone.** Wrapping them applies the offset twice —
    the handler returns rows 200–399 for `?offset=200` and a second slice of `[200:400]` is empty.
    (Mutation: wrap them anyway → 4 named failures, one per endpoint.)
  * **Bad values are a 422 in the error envelope, not a clamp.** `?limit=-1` had reached Postgres
    as `LIMIT -1` and 500'd on the audit routes; on SQLite it meant *no* limit.

  The parameters are also injected into the OpenAPI spec — applied at the ASGI layer they would
  have been invisible to anyone generating a client, the audience that most needs to know a
  collection can be paged. `API.md` documents the contract and states its limit honestly: it
  bounds what is **sent**; the authoring collections are still read whole and sliced, which is the
  right trade at hundreds per tenant, while the genuinely unbounded tables page in SQL.

  One test was wrong on the first run and is recorded: it assumed an unknown variable's history
  404s, so it was measuring the handler rather than the wrapper. It now drives the wrapper with a
  synthetic 404 app. 12 tests; with this, **plan item 5.3 is closed** (the envelope half was R10.18).

- [x] **R10.24 WEBHOOK DELIVERY IS NOW DURABLE — AS ITS OWN DOCSTRING HAD CLAIMED ALL ALONG.**
  `webhook_dispatch.py` said a failed delivery is *"retried by the scheduler, and dead-lettered
  after a bound"*. What shipped was a bare daemon thread **per event per subscription**, retrying
  in a tight loop inside that thread, persisting only last-known state onto the subscription row
  (whose own comment says *"Not a delivery LOG"*). So a rolling deploy or a SIGTERM dropped every
  in-flight event with no record it existed; a receiver down for ten minutes lost everything
  emitted in those ten minutes; there was no dead-letter queue to replay from; and one event ×
  50 subscriptions was 50 OS threads started inside a request handler. `decision_outbox.py`, one
  file over, did all of this correctly.

  Now, on that pattern: `emit` **inserts** one `webhook_deliveries` row per matching subscription
  (migration `20260924_0032`, idempotent) — a cheap write, still never delaying or failing the
  caller. **One** long-lived worker delivers promptly. A **leader-gated scheduler drain** is the
  durability guarantee: it delivers retries whose backoff has elapsed *and* rows a dead process
  claimed and never finished. Backoff 10s → 30s → 2m → 10m → 30m → 2h → 6h (~9 hours); after
  `WEBHOOK_MAX_ATTEMPTS` the row is **dead**, kept, listable (`?status=dead`) and replayable
  (`POST /api/v1/webhook-deliveries/{id}/redeliver`, which keeps the attempt count so the history
  still shows it died). A target blocked by egress policy goes straight to dead — that is
  configuration, and retrying it repeats the refusal on a timer for nine hours. At-least-once, with
  the delivery id sent so receivers can dedupe; `API.md` says so.

  **Three things the tests caught that reading would not have.**
  * **The existing e2e tests would have lost their guarantee silently.** They joined every thread
    named `webhook-delivery` to make ordering deterministic. That thread no longer exists, so the
    join matched nothing and returned at once — **all 75 webhook tests kept passing** with the
    guarantee gone. Replaced with an explicit `flush()` that waits for the dispatcher.
  * **A mutation survived, and the reason was the interesting part.** Making the claim
    non-exclusive (accept every write) passed. The exclusivity check only matters in a *race* —
    two claimers both SELECT the same pending row before either UPDATEs, which Postgres READ
    COMMITTED permits — and a sequential test cannot produce it because the second SELECT already
    filters the row out. The guarded write is now its own method and tested directly: the loser
    must get `False`. Mutation now caught.
  * The config gate flagged all four new settings the moment they were added, and the
    mutating-route tripwire refused the new `redeliver` route until it named the test that probes
    it across tenants. Both gates doing their job.

  13 durability tests: recorded before sent, marked delivered, rescheduled with backoff, dead after
  the bound and listable, egress-blocked straight to dead, dead letters replayable, **a claim
  abandoned by a dead process is recovered** (the SIGTERM case), a fresh claim is not stolen, the
  claim is exclusive under a race, a deleted subscription's queue is not sent, one event to 11
  subscriptions starts at most one thread, deliveries and replay are tenant-scoped, and the drain
  is leader-gated. Four mutations, all caught.

- [x] **R10.25 OAUTH2, BOTH DIRECTIONS — AND IT WAS WORSE THAN "NOT IMPLEMENTED".** (Plan 6.2.)
  **Outbound, connectors.** `auth_type: oauth2` was a bearer token in disguise: the probe took the
  first of `access_token`, `token`, `api_key`, **`client_secret`** and sent it as
  `Authorization: Bearer`. So a connector configured the normal way — `client_id` +
  `client_secret`, no pre-issued token — **sent its client secret to the data provider's API on
  every call**, where only the token endpoint should ever see it. And one given a pre-issued token
  failed every decision from the moment that token expired, because nothing renewed it. The seeded
  GST connector is `oauth2`.

  Now `app/oauth2_client.py` does RFC 6749 §4.4 client credentials when `config.token_url` is set:
  `client_secret_basic` (form-encoded before base64, which matters the moment either value has a
  `:`) or `client_secret_post`, optional `scope`/`audience`; the token endpoint goes through the
  **same SSRF-pinned egress** as a fetch, redirects off, because it receives the secret. Tokens are
  cached per (tenant, connector, endpoint, client, scope, audience) until `expires_in` minus 60 s,
  single-flighted so a cold cache under load makes one token request, and **a 401 from the
  resource invalidates the token and retries once** with a fresh one. Without `token_url`,
  `oauth2` still means a static token — and `client_secret` is no longer on the list it will send.
  Token failures come back with stable codes (`oauth2_client_rejected`, `oauth2_token_unreachable`,
  …), and the probe reports a rejected client as an auth failure, not as unreachable. The token is
  added to the redaction set so a transport error quoting the request cannot carry it into a
  trace or a 502 body — tested by injecting exactly that, and mutation-checked. 35 tests.

  One thing only running it found: the 401-recovery was first written after `_send` returned — but
  `_send` *raises* on a 401, so the recovery was unreachable code that every test around it passed.
  Moved into the exception path; a test now asserts the second request carries the new token.

  **Inbound, the product's own API.** The only machine credential was a long-lived `x-api-key` on
  every request, which standard OAuth2 client libraries cannot use at all.
  `POST /api/v1/oauth/token` now exchanges a key (`client_id` = its `kid`, `client_secret` = the
  key) for a bearer token with **exactly the key's tenant and role**, re-resolved to the key on
  every request — so revoking the key kills its tokens, and a role change applies to them. Signed
  with a key **derived** from the admin JWT secret under a fixed label and typed `rm_access`, so a
  platform-admin JWT is not an access token and an access token is not an admin JWT — no new secret
  to provision, and no confusion between the two. Rate-limited on the login budget, because it
  accepts a guess. `OAUTH2_ACCESS_TOKEN_TTL_SECONDS` (default 3600, clamped 60–86400) in
  `.env.example` and `values.yaml`. 20 tests.

  **Seven guards mutation-checked, one survived first.** `typ` check, domain separation, the tenant
  binding, re-resolving the key per request, the `client_id` ↔ key binding, and the kid-cache clear
  on revoke were all caught. The kid-cache clear on **role change** survived: the test never used
  the token before changing the role, so the cache was cold and there was nothing stale to serve.
  It now uses the token first; caught.

  **Two corrections found on the way.**
  * `API.md` said a key could be sent as `Authorization: Bearer rm_live_...`. **It returns 401** —
    a bearer value is read as a session or access token, never a key. Checked by request, not by
    reading; the doc is fixed rather than the behaviour widened.
  * R10.24's `redeliver` route was never mapped in `rbac.py`. The anti-rot test caught it. It had
    fallen to the fail-closed default, which is MANAGE_ACCESS — the same capability subscriptions
    need — so it was never wrong, only undeclared. Mapped, and a test pins that a `policy_maker`
    cannot replay a delivery.

- [x] **R10.26 CONNECTOR CREDENTIALS WERE IN PLAINTEXT IN `entity_histories`.** Found while building
  6.1, and checked by running it rather than reading it: `create_connector` wrote the whole payload
  — config included — into the history snapshot as plaintext JSON, one column over from the config
  it had just Fernet-encrypted. Every credential any connector was created with was readable in the
  database and in every backup. No route serves connector history (probed: 404), and neither the
  audit trail nor `/api/v1/export` carries it, so this was at-rest exposure, not an API leak — and
  6.1's template install would have added provider credentials to it. The snapshot is now masked
  with the same markers the API masks on read, and migration `20260924_0033` scrubs rows written
  before (idempotent; deliberately no downgrade). Three tests; both halves mutation-checked.

- [x] **R10.27 THREE NAMED-PROVIDER CONNECTORS, BUILT FROM THE PROVIDERS' OWN CONTRACTS.** (Plan 6.1,
  on your call to proceed with a documented request/response contract instead of live credentials.)
  **Equifax** US Consumer Credit Report (bureau, OAuth2 client credentials), **FinBox BankConnect**
  predictors (bank statements) and **Setu** PAN verification (KYC) — one per category, chosen
  because their contracts can be read without an account. Experian (password grant, gated schema)
  and Socure (gated) were checked and rejected on those grounds. `GET /api/v1/connector-templates`,
  `…/{id}`, `POST …/{id}/install`; the contract page `docs/connectors/TEMPLATES.md` is **generated**
  from the templates and a test fails when it drifts.

  **What "documented" means here, stated per template:** Setu's endpoint, headers, bodies, error
  codes and sandbox test PANs come from Setu's own OpenAPI in its public docs repo. FinBox's come
  from its public REST docs. Equifax's auth flow comes from its quick start guide; its request and
  response schema from Equifax's portal OpenAPI as republished in a public repo (the portal needs a
  corporate login), so its token URL and production base URL are install settings, not guesses.
  **None has been run against a live sandbox.** Each is tested end to end — installed through the
  API and driven through a real decision — against a local server serving the provider's own
  published example bodies. Each template carries `verified` / `not_verified` lists.

  **Reading the contracts found three things a template written from memory would have got wrong.**
  * Equifax's own example puts an **unrequested** FICO model first and the requested model second —
    with score 0. `models[0].score` reads the wrong model; the template maps the array and the
    starter variable selects by model number, and treats a non-positive score as no score.
  * Equifax documents `status: "completed === Request Schema Validation Error"` — a 200 carrying a
    failure. The check requires exactly `completed`.
  * Setu's quickstart says `"success"`, its OpenAPI says `"SUCCESS"`. The check accepts both.

  **Engine support, generic, any connector can use it** (`connector_fetch`): `required_inputs`
  (checked before a token request or socket — a missing SSN is `missing_input` naming the field,
  never the value, and spends no bureau pull; patterns stop a payload id walking the URL path),
  `response_checks` (a 200 that is not an answer — still processing, no hit, file frozen — fails
  with a named code instead of mapping into a payload of Nones, which the existing all-None guard
  does not catch when one field, like the hit code, is present), `error_codes` +
  `error_detail_path` (documented statuses to stable codes, provider text in the reason, redacted).

  **Two engine defects the templates exposed, fixed:**
  * **Installing a bureau connector made every decision in the workspace pull a credit report.**
    With every variable computed (the default), every opted-in source any variable reads was
    fetched — for a policy that never mentions the bureau. That is a billed call and, for a consumer
    bureau, an inquiry with no permissible purpose for that application. Skipping the fetch alone
    was no fix: the score variable fails closed on an absent report, and any failed variable sends
    the decision to review. Now a variable whose source must be fetched and that the policy does not
    reach (same transitive walk as `compute_only_used_vars`) is neither fetched nor computed. The
    fast path declines fetching tenants entirely, so the two paths cannot diverge here.
  * **4xx responses counted against the circuit breaker and were retried.** Five applicants with a
    typo in their PAN opened the breaker on a healthy KYC provider and sent everyone else to review
    for the cooldown; a rejected bureau pull was re-sent. Only 408, 429 and 5xx are retried or
    charged now.

  Secret keys are scoped to the connector id at install (`setu_pan_client_secret`) because
  `{{secrets.*}}` is flattened across the tenant — a second install would otherwise silently
  replace the first one's credentials. Installs are tenant-scoped (tested across tenants), 409 on
  an existing id, 422 with every problem at once, author capability. 38 tests; **12 mutations, all
  caught** — including the scoping, per-element progress checks, per-value hit codes, and reading
  the score from the first model.

- [x] **R10.28 MODELS A CREDIT SHOP ACTUALLY HAS, AND AN EVALUATION SCREEN THAT TAKES A FILE.**
  (Plan 6.3.) `model_executor` hosted pickles only, and the evaluation dataset went into a
  `<textarea>`. Now:
  * **ONNX** (onnxruntime, now a real requirement — it was in the dev venv only as `magika`'s
    dependency, which the image never installs), **XGBoost JSON**, **LightGBM** model text, and
    **PMML** — `Scorecard` with ranked reason codes, `RegressionModel`, `TreeModel` — through an
    in-house pure-Python evaluator on `defusedxml`, because every maintained Python PMML scorer
    runs on a JVM. Anything outside that subset is refused at upload **by name**, not scored
    approximately. Pickle still works; `MODEL_ALLOW_PICKLE=0` refuses it before it reaches a
    worker, for an install that wants no code-on-load format at all.
  * **The format comes from the bytes, never from the declared type.** Pickle opens `0x80`, ONNX
    `0x08`, PMML `<`, XGBoost `{`, LightGBM `tree`; nothing labelled ONNX can reach `pickle.loads`.
    Stored as `model_format` (migration `20260924_0034`) for listing only; scoring re-reads the bytes.
  * **Named boosters and PMML score by feature name.** A caller listing features in another order
    got a confident wrong score from a positional loader; tested with the order reversed.
  * **Evaluation scored one row per worker round trip** — a process hop and a full model
    deserialize per account, so a real portfolio could not finish. It is one batch now, chunked
    at 2,000 rows (`MODEL_BATCH_TIMEOUT_MS` per chunk), and a test asserts one worker call.
  * **The evaluation screen takes a `.csv`/`.xlsx`/`.json`/`.jsonl` upload**, held as rows, never
    dumped into the textarea; column inputs suggest the file's columns, and a named column the
    file lacks is reported before any request. The upload parser is one shared function, now also
    behind the simulation screen that had its own inline copy. The models page drops a "type"
    dropdown the server always overrode and labels each model by its detected format.

  **Evidence is the libraries, not us:** `tests/fixtures/models/generate_fixtures.py` fits one
  dataset with scikit-learn, XGBoost and LightGBM, exports ONNX with skl2onnx and a PMML
  regression with the fitted coefficients, and records each NATIVE library's predictions. Every
  format, scored through the isolated worker, matches its library to 1e-5 (LightGBM 1e-9). 23
  Python tests, 6 Vitest, 3 Playwright (a 20,000-row CSV evaluated end to end in the browser).
  **13 mutations, two survived first:** chunk reassembly (the test compared only probabilities)
  and reason-code ranking (the fixture's gaps were already in declaration order); both tests
  fixed, both now caught. The entity-bomb test was also rewritten before it could pass for the
  wrong reason — its document had no model element, so it would have failed as "no model" even
  if entities were expanded. README's upload example used `model_blob_base64`; the field is
  `model_base64`, so the example 422'd as written. Corrected.

- [x] **R10.29 THE DIFFERENTIATED CAPABILITIES GET SCREENS; PROMOTION GETS A REVIEW.** (Plan 6.5.)
  What-if, shadow deployment, releases/rollback, bundle versions and entity history all had working,
  tested APIs and no UI. The Policies screen now has a **Release & impact** panel: what-if KPIs
  (recorded vs would-be, against a chosen bundle version), shadow start/report/stop, the release
  timeline with rollback, and version history with a field-level diff. The Decision Explorer can
  **replay one decision under a retained bundle version** — the regulator's question about a single
  applicant.

  **Promotion went through `window.confirm()` on four pages** (Deploy, Rules, Variables, Policies),
  each recording a fixed reason — "Manual promotion from Deploy board." — so the ledger said, on every
  row, that someone clicked, never why. One shared review (`PromotionReview.tsx`) now shows the change
  summary and the measured backtest impact and requires a written reason, which is what gets
  recorded. Built on a new accessible `Dialog` in the UI kit (the app had none): labelled, focus
  in and back, Tab trapped, Escape cancels, portalled so no card can clip it. Non-promotion confirms
  (delete, import, editor mode switch) are unchanged and remain `window.confirm()`.

  **Four backend defects found on the way, all fixed and mutation-checked:**
  * **Rollback recorded whatever `actor` the caller sent.** Any caller could log a production
    rollback under someone else's name; without one it said "system", because the fallback read
    `request.state.user_id`, which nothing sets. It now records the authenticated caller, exactly
    as promotion does.
  * **Only variables had a history route.** Policy, rule, scorecard, decision-table and connector
    versions were snapshotted on every write and readable by nothing. `GET /api/v1/history/{type}/{id}`,
    tenant-scoped, and connector snapshots are masked on read as well as on write (R10.26).
  * **The bundle-compile debounce dropped changes.** A write within 5 s of the previous one was not
    deferred, it was discarded — no compile was ever scheduled, so the bundle SDKs download and that
    replay/what-if/backtest read stayed at the previous change until some unrelated later write.
    Promoting a rule and then its policy from the Deploy board inside five seconds shipped a bundle
    without the policy change. The debounce now has a trailing edge: one deferred compile per tenant.
    Found because the replay browser test's policy change never reached a bundle.
  * `check_api_docs.py` treated a documented query string as part of the path.

  **One test was wrong before it was right:** the replay browser test first used a decision whose
  policy had not changed, so a screen hard-coded to say "the same outcome" passed it. It now records
  a decision under an approving policy, changes the policy to reject, and requires the screen to
  report the flip — which is what exposed the debounce bug. 5 Playwright tests for the panel and
  replay, 1 for the reviewed promotion, the existing v3 promotion flows moved onto the dialog, and 7
  Python tests (rollback actor, history ×4, debounce ×2); every guard mutation-checked, the replay
  one only after the test was fixed.

- [x] **R10.0** Restored the system I left mid-benchmark: soak restarted (one generator,
  contention window configured), stack returned to committed defaults, and the `FAST_DECIDE` /
  `COMPUTE_ONLY_USED_VARS` / `RULEMIND_WORKERS` wiring committed.

## R9. Render gone, paging proven, and an OS upgrade that broke the build — 2026-09-19

- [x] **R9.1 RENDER IS GONE.** The owner deleted the account. Both hostnames answer 404 with `x-render-routing: no-server` — Render's own header for "no service is bound here". That is the check worth recording, because this repo already called these services dead once on a single timeout: a *sleeping* free-tier service answers 200 after a ~50s cold start and a *stopped* one answers 5xx. Only a deleted one produces `no-server`. **No hosted footprint left to bill.**
- [x] **R9.2 Paging proven END TO END** against a live stack, not by reading config. All 8 links green, and `--send` delivered a real alert carrying its runbook link. **The routing separation holds**: the on-call sink received only `RuleMindPagingTest`, the heartbeat sink only `Watchdog`. A heartbeat in the on-call channel pages a human every interval to say nothing is wrong, which is how one gets muted.
- [x] **R9.3 Both deploy paths re-run** rather than carried forward — compose **14/14**, kind + Helm **16/16**. `k8s_alerts.sh` applied all 8 rules to the cluster, correctly detecting no prometheus-operator and falling back to the sidecar ConfigMap.
- [x] **R9.4 Branch protection re-checked by BOTH routes** — classic `PUT /branches/main/protection` and modern `POST /rulesets` both 403 "Upgrade to GitHub Pro or make this repository public". Still genuinely unavailable.
- [x] **R9.5 Gate + CI green** — 16 lanes, 0 skipped, locally and on the runner.

### An OS upgrade broke the Python lane, and the pin guarded the wrong axis

The machine went to **Darwin 27** between runs. The next regression failed BOTH Python lanes — 2064 tests, one error each — in a module nothing had touched:

```
ImportError: dlopen(.../scipy/sparse/linalg/_propack/_spropack.cpython-311-darwin.so):
section '__DATA/__thread_bss' has a zero-fill section type, but offset field is not zero
```

The comment on those pins **already described this failure and named the wrong cause**: *"on modern macOS (Darwin 27 / py3.13) the 1.15.1 wheel fails to dlopen, so newer interpreters take 1.18.0 there."* It is not the interpreter, it is the OS — so the guard was written on `python_version`, Python stayed at 3.11, and an OS upgrade walked straight past it.

1.17.1 is the newest release supporting 3.11, loads on Darwin 27, and satisfies scikit-learn 1.9.1. It works on Linux too, which collapses four cells to two: the `sys_platform` split existed only to work around a version that was simply too old.

### Two flakes correctly identified as flakes, not defects

* The image-build lane failed once on `getaddrinfo EAI_AGAIN registry.npmjs.org` **inside** the Docker build — a DNS blip in the window after Colima restarted. Checked rather than assumed (host `dig` and an in-container `nslookup` both resolved) and it passed on re-run.
* A CI run sat `queued` forever because the **self-hosted runner did not survive the reboot** (`./svc.sh status` → not installed, so nothing restarted it). Labels matched; the queue entry was simply stale. Restarted the runner and re-dispatched.

### Self-inflicted, again, and caught this time

I dispatched CI while the local gate was running — the same machine, competing for ports — and my own echo line claimed I had not. Cancelled it within the minute, before it could take the ports. Third instance this cycle of "the runner IS this machine"; the ports are now per-lane, which is what stops it being a correctness problem.

## R8. The device, the key, and a frontend nobody had opened — 2026-09-19

- [x] **R8.1 #176 CLOSED on real hardware.** 14 instrumentation tests on a Nothing Phone (1) (A015, Android 16, `securityLevel=1` TRUSTED_ENVIRONMENT), a Galaxy S23 (SM-S918B) and an emulator — 42 runs, 0 failures. `getEncoded()` returning null IS the non-exportability proof. Mutation: swapping `keystoreKey()` for a plain `SecretKeySpec` leaves every functional test passing and fails 4 of these. Opt-in `device` lane in the gate.
- [x] **R8.2 #123 CLOSED against a real OpenAI key.** 10/10 live tests, including the two that carry the weight — a generated rule SAVES and comes back compiled to `IF (Bureau Score >= 600) → APPROVE ELSE → REJECT`, and usage counters move against real tokens. The suite's own assertions were written against a shape the API never returns (`body["rule"]["nodes"]`; the real envelope is `{draft, in_scope, provider, valid, validation_error}`), so it would have failed on its own invention the first time anyone supplied a key.
- [x] **R8.3 212 dead workflow runs deleted.** Of 1,065 runs scanned, 212 executed `steps: 0` — assigned no runner, the billing block. 18 real failures kept, every one verified to have executed steps. `scripts/github_runs_cleanup.sh --dry-run` lists before touching anything.
- [x] **R8.4 The frontend, opened rather than read.** 40 routes: **11 clean → 39**. The one page the user named was the tip.
- [x] **R8.5 Watchdog + delivery-failure alerts.** Every other rule in `alerts.yml` assumed the paging pipe works and nothing checked it — if paging breaks the symptom is that you stop being woken up, which is indistinguishable from everything being fine. `Watchdog` (`expr: vector(1)`) is a dead man's switch routed to its OWN destination on a 5m repeat, because a heartbeat in the on-call channel gets muted and stops being a heartbeat. `scripts/verify_paging.sh` walks the chain link by link and `--send` injects a real alert.

### Four defects the browser found that no test did

1. **Running `pnpm test:e2e` broke the dev server.** Playwright's `next dev` shares `apps/web/.next` with the developer's, and `NEXT_PUBLIC_*` is inlined at compile time — so an e2e run left client chunks hardcoding the e2e API (`:8100`). SSR HTML regenerates, the stale client chunk does not: 24 hydration failures, 26 `ERR_CONNECTION_REFUSED`, and a dashboard saying "Unable to reach the API server at 127.0.0.1:8100" about a backend that was up on 8080. It survives a restart, because `.next` persists. One line each side: `NEXT_DIST_DIR`.
2. **A 500 that read as a CORS error.** `/audit`, `/decision-explorer` and `/reports` all reported "blocked by CORS policy". It was `InvalidToken` on one decision row encrypted under a different key — and an unhandled 500 never reaches the CORS middleware that would have added the header, so the browser named the wrong subsystem entirely.
3. **The AI model picker showed one model, from the wrong provider.** `GET /ai/models` defaulted to the literal `"anthropic"` and ignored the workspace's `default_provider`; the page raced two fetches and applied whichever RESPONSE landed last; and Gemini was fully implemented server-side but unreachable, because the selector pinned a two-element literal under a comment claiming it was driven by the config.
4. **Settings had no labels, no section headings, and no `h1` anywhere.** `SectionHeader` is the page title on all 11 v3 pages and rendered `h2`, so no page had a level-one heading at all; `SectionTitle` rendered `h3` on 14 pages, skipping a level. 19 inputs were named only by a placeholder — which disappears the moment you type — and 10 icon-only buttons had no name at all.

### A fix of mine that was wrong, caught by re-running the audit

The first version of the decryption fallback substituted a marker **dict** for every unreadable field. `trace` and `rule_results` are lists, and a non-empty dict is truthy, so it also defeated the `or []` guards at the call site. The dashboard died on `(selected.trace ?? []).map is not a function` — the guard was for null and had been handed an object. **A fallback that changes a field's type is not a fallback; it moves the crash.** Each field now falls back to its own zero value and the row carries one boolean.

## R7. The green that proved nothing, and the last three Android files — 2026-09-19

- [x] **R7.1** `load-test` reported SUCCESS while **both** its k6 scripts asserted nothing. `evaluate.k6.js`'s `has outcome` check read `body.outcome`; `POST /api/v1/rules/{id}/test` nests it under `result`. It failed on 100% of requests — **0 of 1398** in run 35430953581 — and stayed invisible for two independent reasons: the step carried `continue-on-error: true`, and the script declared no `checks` threshold, so a 0% check rate was not even a non-zero k6 exit. Fixed, and now 2828 of 2828 on the runner.
- [x] **R7.2** `saturation.k6.js` was worse, because it looked like it passed on its own merits. Its assertion is conditional — *if* a variable failed to compute, the decision must have gone to review with the fail-closed guard in its trace — and against a healthy server no variable ever fails, so the assertion never executed. It printed `PASS` having proven only that the server was up. Now counts degraded responses and reports **INCONCLUSIVE** with the reason when a run never saturated; `degraded_responses: ["count>0"]` fails the job.
- [x] **R7.3** `test/load/saturation_setup.py` arms it deterministically, and verifies its own effect with a real decision before the sweep starts. On the runner: **1,894 degraded responses, 0 unsound decisions** — the guard genuinely firing under load and holding. Both k6 steps lost `continue-on-error`.
- [x] **R7.4** `CryptoManager` (10), `SyncWorker` (5), `LegacySecureStoreMigration` (5). **Every file in `rulemind-android/src/main` now has a test**; Kotlin lane **120 → 140**.
- [x] **R7.5** Docs truthed up: readiness item 7 closed, the Alertmanager entry corrected (the receiver is real now, only the destination URL is missing), Render re-probed 2026-09-19 (**still live**, 200), and the Actions tab's red history categorised.

- [x] **R7.6** The API image could not be built AT ALL, and 5,000 passing tests said nothing. `requirements.txt` pinned `scipy==1.18.1; sys_platform != "darwin"` with no `python_version` guard; 1.18.x needs Python >= 3.12 and the image is `python:3.11-slim` on Linux, so pip aborted the ENTIRE install. The comment three lines above those pins warns about exactly this failure — for the darwin side, which has a guard. Now four cells, because the constraint has two dimensions. Found by rebuilding, not by reading: `docker compose start api` re-ran the migrate service, that failed on something else, and rebuilding to fix *that* exposed this.
- [x] **R7.7** Gate gains a `build` lane in the DEFAULT set — 16 rows, **5,092 tests, 0 skipped**. `smoke_compose.sh` already ran `docker compose build` and would have caught it, but `smoke` is opt-in because it needs 8080/3000/55432/6380 to itself. The build half needs no ports. Mutation-checked: the bad pin turns the lane FAIL with the pip error in the summary.
- [x] **R7.8** The Postgres lane stopped sharing a database with the running app. The guard was correct and fired repeatedly, but it can only SKIP the lane and the fix it prints drops the app's data — so on any machine where someone used the stack, 2,059 tests were silently absent from the gate. `rulemind_lane` is created additively instead. The migrations lane's version of the same sharing had already stamped the app's `alembic_version` to a revision the running image did not carry, so `docker compose up` would not start.
- [x] **R7.9** All four workflows green on `92902fe`, and this time the greens mean something: self-hosted tests 16/16 lanes strict, security, backup-verify, and load-test at **2680/2680 checks** with **1507 degraded responses and 0 unsound decisions**.

### What the saturation sweep taught, recorded because I got it wrong twice first

The script's own header says to run it against a pool smaller than the concurrency. That is not sufficient and not reproducible:

| Configuration | Result |
|---|---|
| pool=4, timeout_ms=2000 (defaults), 60 VUs | 0.0% degraded — INCONCLUSIVE |
| pool=4, timeout_ms=60, 60 VUs | 0.0% degraded — INCONCLUSIVE |
| pool=1, timeout_ms=60, 60 VUs | 0.0% on one run, **32.9% on the next** |
| pool=4, timeout_ms=60, **one slow variable** | 81.9% degraded, 59 guard firings, 0 unsound |
| pool=1, timeout_ms=60, **one slow variable** | 99.6% degraded, 241 guard firings, 0 unsound |

`future.result(timeout=)` bounds the wait for **one variable**, not the decision, and uvicorn's threadpool caps concurrent decisions at 40 — so the per-call queue never gets deep enough to cross the timeout, and whether it does at all depends on what else the machine is doing. Gating a threshold on that would flap. Forcing it with a variable slower than the timeout is deterministic on any machine.

### A mutation that survived, and the test it condemned

`ExistingPeriodicWorkPolicy.UPDATE` → `KEEP` passed a test named *"scheduling twice replaces rather than stacks"*. The unique-work name leaves exactly one request enqueued under either policy, so a count assertion cannot tell them apart — and under `KEEP` a changed `bundleSyncIntervalMinutes` is silently discarded while every outward sign (one enqueued sync, right name, right constraints) still looks correct. The test now reads `periodicityInfo` and asserts the interval.

### Mutation checks this round

| Mutation | Named test that failed |
|---|---|
| k6 `checks` threshold removed | evaluate sweep no longer gates — reproduced before the fix |
| `fail_open_on_variable_error` on | saturation sweep: unsound decisions by name, exit 99 |
| harness not armed | saturation sweep: INCONCLUSIVE, exit 99 (was PASS, exit 0) |
| stored keypair ignored | `the device key pair survives a new CryptoManager over the same store` |
| `Result.retry()` → `Result.failure()` | 2 `SyncWorkerTest` tests |
| migration marker never set | 2 `LegacySecureStoreMigrationTest` tests |
| `UPDATE` → `KEEP` | **survived** the first version; killed by the interval assertion |

## R6. Finish #176's Android file list — 2026-09-19

- [x] **R6.1** `SqliteDecisionOutbox` (12 tests). Failure modes here are data loss and double-delivery, not a wrong answer: backoff respected, oldest-first draining, `trimToCapacity` drops the OLDEST, `markSynced([])` is a no-op.
- [x] **R6.2** `RuleMind` (7 tests). The refusal contract before `initialize`, and that `initialize` actually SCHEDULES the periodic bundle sync — an enqueue that silently stopped would fail nothing and leave the device deciding on a stale bundle forever.
- [x] **R6.3** Full gate green — **5,062 tests**, Kotlin lane **46 → 110**. Pushed; CI green.

### A mutation that survived, and what it was hiding

`CONFLICT_IGNORE` → `CONFLICT_REPLACE` in the outbox broke no test. The idempotency test asserted only the row count, which both modes satisfy — so it proved nothing about the conflict mode it was named after. REPLACE resets `attempts` and `next_attempt_at`, so a decision that had failed three times and was backed off would return as attempts=0 and immediately eligible: the SDK re-hammers a server it already knows is failing, and the give-up logic never fires. Now asserted directly.


## R5. Paging, Render, and the rest of the Android coverage — 2026-09-18

- [x] **R5.1** Alertmanager's last hop closed. A real `paging` receiver reads its destination from a **gitignored** `observability/secrets/paging_url` via `url_file`, so turning paging on never means committing a credential — which is why it had stayed off. Verified both ways: an alert arrived carrying its runbook link, and with the file absent Alertmanager still started and failed *loudly* at notify time.
- [x] **R5.2** `BundleParser` covered — 11 tests, mostly on `bound()`, which encodes a THREE-way distinction (absent / explicit null / non-numeric) mirroring the Python core. A parsing default that differs from the server's is a divergence the parity corpora cannot catch, because they compare evaluation and this runs before it. Android module 26 → **37**.
- [x] **R5.3** Full gate green — 15 lanes, 0 skipped, **5,035 tests** (Kotlin 46 → 83 across this session). Pushed; CI green.
- [x] **R5.4 — CLOSED 2026-09-19, by the owner.** Delete the Render services. Resolved in R9.1; this box stayed unticked afterwards and was the plan's last open item, which made the whole file read as if something were outstanding. `RENDER_API_KEY` exists nowhere on this machine (no env var, no `~/.render`, no CLI, not in `.env`), and Render has no unauthenticated delete. `scripts/render_teardown.sh` does it in one command once the key is exported; it lists every resource first and requires typing `DELETE`.


## R4. Truth-up the docs, and give the Android module tests — 2026-09-18

- [x] **R4.1** Rewrote `docs/PRODUCTION-READINESS.md`. Its headline open item is "what remains is registering the runner" — done days ago, with many green runs since. Its Next.js entry is closed at 14.2.35 (now 15.5.25) and its dependency section predates the whole Dependabot round.
- [x] **R4.2** Fixed the stale references to retired workflows (`sdk-verify.yml`, `ci.yml`, `test-report.yml`) — only the three parity files were actually WRONG (they said the check runs in `sdk-verify.yml`); CLAUDE.md and the readiness doc already explain the retirement, and PLAN.md's are historical entries that should stay. `check_parity.py` still passes.
- [x] **R4.3** Gave `rulemind-android` a test source set — Robolectric, so it runs on the JVM in the standing gate rather than waiting for an emulator. 18 tests covering `SecurePrefs` and `ExecutionStore`, all mutation-checked. Kotlin lane 46 → **64**. It has **1,644 lines and zero tests** — NetworkClient (268), RuleMind (254), BundleParser (253) — and the `SecurePrefs` crypto rewritten this week is verified by compilation alone.
- [x] **R4.4** Closed issue #133 and opened #176 in its place: its premise was emulator jobs in `sdk-verify.yml`, a workflow that no longer exists. #176 states the real remaining gap — Robolectric cannot back the AndroidKeyStore, so "the key is non-exportable" is still unproven, and NetworkClient/BundleParser/RuleMind remain uncovered.
- [x] **R4.5** Full gate green — 15 lanes, 0 skipped, **5,024 tests** (Kotlin 46 → 72). Pushed; CI green.

### Not in scope, and why

- **fastapi/starlette 1.x** — the owner asked for this later.
- **AI live validation (#123), Alertmanager's last hop, branch protection** — each needs a credential or a paid plan.
- **Render** — re-probed 2026-09-18: the API answers **200 in 0.3s** and the dashboard 200. STILL LIVE, still possibly billing. Worth recording that the first probe returned `000` and a hasty read would have reported it dead — which is the same mistake this file already records once.


## R3. Round 3 execution — 2026-09-16

- [x] **R3.1** Port `sdk-verify.yml` to the self-hosted runner, `workflow_dispatch` only.
- [x] **R3.2** A4 — delete the 11 stale deployments and 4 environments (`scripts/github_deployments_cleanup.sh`, `--dry-run` first).
- [x] **R3.3** A6 — fix the vitest count in `run_all_tests.sh`. Cause: vitest KEEPS ANSI colour under CI, so `Tests +[0-9]+ passed` could not match across the escape sequences; it parsed on a laptop and silently undercounted in CI. Now reads `tests=` from `test-results/vitest-junit.xml`, with an ANSI-stripped log fallback.
- [x] **R3.4** B1 — ran both QA suites against the compose stack after seeding: **core 31/31, sandbox 21/21**, exit 0. The sandbox-escape matrix (16 techniques + timeout, memory bomb, deep recursion, ReDoS) all refused.
- [x] **R3.5** B2 — wire both QA suites into `run_all_tests.sh` as lanes.
- [x] **R3.6** B3 — swept all **38** discovered routes against compose: 35 clean, 3 findings. `/admin` and `/branding` log an expected 401 for an unauthenticated visitor. `/collab` was a REAL BUG and is fixed: the page never sent a credential on its WebSocket, and a browser cannot put `x-api-key` in a header, so the socket could not open in the shipped API-key configuration. Results refreshed in `qa/e2e-full/web_sweep_results.json`.
- [x] **R3.7** C5 — resolved `/model-sets` and `/predictors`. CORRECTION to the premise: neither was in the sidebar and neither was linked anywhere — they existed only in app-shell's PAGE_COPY map, so no menu 404'd; the real defect was two fully tested backends (7 + 8 endpoints) with no way to reach them from the product. Built both pages and added them to the sidebar, with 3 e2e tests driving them against a live backend.
- [~] **R3.8** C1-C4 — multi-agent sweep. **C3 (orchestration) and C4 (evaluations) completed**; their findings are fixed, tested and mutation-checked. **C1 (UI/UX) and C2 (backend API) were killed mid-run by an account session rate limit**, not by any failure in the work — their partial output landed (studio, decision-tables and auth e2e specs; insights/runtime/governance API tests and fixes) and is validated and committed, but neither lens finished its scope. Remaining: the rest of the 39-route UI pass, and the untested endpoints beyond insights/runtime/governance.
- [x] **R3.9** C3 — covered `transform` (21 tests) and found **four real defects, all fixed and mutation-checked**: (1) the reachability walk never scanned `config.mapping`/`inputMapping`, so a transform-only policy had an EMPTY needed set — and because the executor filters on `simulate or flag`, **every backtest** silently did not predict production; (2) `$.computed` keyed on truthiness, so an empty transform output exposed other steps' fields; (3) an unterminated `[` in a JSONPath returned the whole list; (4) a `model` step that could not run failed OPEN, turning a broken model into an approval.
- [x] **R3.10** Full regression green, workflows green, pushed. **4,906 tests across 15 graded rows, 0 failed, 0 skipped** — and "0 skipped" is now enforced rather than hoped for: `RULEMIND_REQUIRE_ALL_LANES=1` (set in CI) turns a skipped lane into a failure, after a local run exited 0 having silently not executed 2,085 tests. CI run `35133080356` green with strict mode on; `security` and `backup-verify` green.
- [x] **R3.11** Closed the remaining sweep findings A–G: promotion provenance (3 columns + migration 0030), the promotion gate's 90%-false-fail rate at N=200, the 300s analytics cache, the positional p-value, both `/simulation` label defects, and author-time validation of mistyped JSONPath mappings. All mutation-checked.
- [x] **R3.12** Fixed a compounding infrastructure leak the sweep surfaced: the Postgres lane created **453 schemas / 18,573 tables** and never dropped them, which broke `backup-verify` with `out of shared memory` — an error naming a Postgres tuning knob and saying nothing about test detritus. Lane now cleans up (one transaction per DROP, since doing all 453 in one re-creates the failure), and the dump is scoped to `public` so test schemas can never break a restore rehearsal again.
- [x] **R3.13** Finished the sweep scope the rate limit cut short. Endpoints enumerated mechanically (every `@router.<verb>` cross-referenced against tests/e2e/qa): **9 of 272** had no mention — all now covered, including two that resolve their tenant from a contextvar rather than an argument and had no isolation test. Routes: `e2e/route-health.spec.ts` puts **all 40** in the gate, replacing an ad-hoc script whose results were a stale JSON file.
- [x] **R3.14** Dependabot: all 12 open PRs resolved. **2 merged** (#164, #165), **7 applied to main and closed** (#147+#152 react/react-dom — which fail individually and pass only together; #174 vitest 5; #153/#155/#172 Android; #173 lucide-react), **2 closed as rejected/obsolete** (#146 TypeScript 7, #158 setup-python), **1 open** (#171, blocked on numpy).
- [x] **R3.15** Closed **three CRITICAL advisories** that the PRs turned out to contain between them: two Next.js unauthenticated RCEs and the Vitest UI file-read. `pnpm audit`: 3 critical → 0.
- [x] **R3.16** Android toolchain upgraded (Gradle 8.7→9.7.1, Kotlin 1.9.24→2.2.20, AGP 8.5.2→9.4.0), which is what "blocked on a toolchain bump" needed someone to actually do.
- [x] **R3.17** All three follow-ups closed.
  - **Advisories: `pnpm audit` is now clean** — 4 high + 3 moderate + 1 low → *No known vulnerabilities found*. `xlsx` had **no fix on npm** (`patched=<0.0.0`; SheetJS stopped publishing there), so it is pinned to the tarball they actually ship. vite/postcss/nanoid/esbuild are transitive and fixed by selective pnpm overrides — a bare range override silently left vite on the vulnerable 7.3.1.
  - **Android crypto**: moved off the deprecated `EncryptedSharedPreferences` to a Keystore-backed `SecurePrefs`. **Four** stores used it, not the two the warnings named — including the one holding the device's RSA private key. One-time migration retained in a single file that exists to be deleted. Deprecation warnings 5 → 0.
  - **numpy**: the ceiling moved from a doc note into `requirements.txt` as an environment marker, where pip enforces it, and 2.2.1 → 2.4.6 in the process.
