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
- [ ] **W7.6** CI — **blocked, and not by `[skip ci]`**. Every recent run failed in 3-12s with
  **0 steps executed**, which is an exhausted Actions quota, not a test failure. Local runs
  remain the only signal until the quota resets.
- [x] **W7.1 (superseded)** Supabase project + Alembic chain · **W7.2** Upstash · **W7.3** Koyeb, with fresh
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
- [ ] **I5** Vercel GitHub App access revoked (needs the user's 2FA)
- [ ] **I6** CI: one commit without `[skip ci]` — **tried 2026-09-14 (`8043957`); still blocked.** That push created ZERO runs; Dependabot's commits created runs that executed `steps: 0`. **Needs the BILLING BLOCK cleared (or the repo made public), not minutes** — GitHub's annotation on every failed job says payments failed / spending limit; jobs get no runner, so consumption is already ~0.

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
