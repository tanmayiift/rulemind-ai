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
- [ ] **2.3** SSRF + secret exfiltration on the real connector path. `_fire_action_request`
      resolves `{{secrets.*}}` into URLs/headers/bodies with no host check; the guard exists only
      on `/actions/test`, which deliberately does *not* inject secrets — it protects the harmless
      path and not the dangerous one. Needs an allowlist with post-DNS-resolution IP checks (the
      substring blocklist misses `127.1`, `2130706433`, IPv6-mapped, rebinding, redirects), plus
      clamps on `timeoutMs`/`retries` (today one action can be `600000ms x 10`).
- [x] **2.4** Deleted the unguarded `eval` in `logic.py` (scorecard/metric formulas). Replaced
      with `safe_formula`, a whitelisting AST evaluator — no `eval`, no `__builtins__`, no
      attribute access — so the `().__class__.__mro__[1].__subclasses__()` walk has nothing to
      stand on. Plus CPU/memory-bomb bounds on `**` and string `*`.
- [x] **2.5** Sandbox validator + the three sibling caches keyed on MD5 → SHA-256 (a
      chosen-prefix collision could seed a benign entry and skip validation on a colliding
      malicious body). All four caches were global and unbounded across tenants; now bounded.

## T3 · Multi-tenant isolation gaps

- [ ] **3.1** Unauthenticated WebSocket with no tenant scoping — `accept()` with zero auth and a
      hub keyed by `doc_id` alone. Tenant A reads tenant B's policy drafts. The middleware can't
      save it for two independent reasons (`BaseHTTPMiddleware` skips non-HTTP scopes; `/ws/v1`
      isn't in the prefix guard).
- [ ] **3.2** The isolation matrix's discovery regex is its blind spot — `/ws/v1`, `/scim/v2`,
      `/api/admin/v1` and nested action routes are invisible to it, so the anti-rot tripwire never
      fires for them.
- [ ] **3.3** RBAC's unknown-write default is fail-open (`AUTHOR`), so every new mutating prefix is
      auto-granted to `policy_maker`. `/scim/v2` is unmapped **today**.
- [ ] **3.4** `active_tenant_id` fails open to the default tenant. Plus 15 storage writers let a
      body-supplied `tenant_id` outrank the auth context — not exploitable today, but one
      `extra="allow"` from a cross-tenant write primitive.
- [ ] **3.5** SAML signature-wrapping bypass: falls back to an Assertion from the **unverified**
      document. No `NotOnOrAfter`/`AudienceRestriction`/`Recipient`/`Destination` validation, no
      replay tracking, and entity expansion enabled pre-auth.
- [ ] **3.6** Unauthenticated, unthrottled tenant + API-key minting (`/onboarding/signup` returns
      before the rate-limit check). Also `/api/admin/v1` sits outside all middleware.

## T4 · The chart does not deploy

- [ ] **4.1** Empty-string config is not "unset". `DATABASE_URL=""` raises at import →
      CrashLoopBackOff with no useful log; `RULEMIND_CONFIG_KEY=""` becomes `sha256("")`, a
      publicly derivable key — and setting a real one later makes `decrypt_config_payload`
      silently return `{}`, emptying every connector config. Unrecoverable.
- [ ] **4.2** Every decrypt returns something plausible on the wrong key — ciphertext goes out as
      a live auth header, or straight into `pickle.loads`. Raise instead; add `MultiFernet` +
      rotation; use a real KDF.
- [ ] **4.3** Liveness on `/health` kills the fleet on a DB blip. No probe budgets anywhere, and
      `/health` is a sync `def` sharing the threadpool with `/decide`.
- [ ] **4.4** No resource requests/limits, so the HPA reports `<unknown>` and never scales.
- [ ] **4.5** Pool math is 8–30x over `max_connections` (up to 3200 vs a default 100).
- [ ] **4.6** Migrations race (six concurrent runners, no lock) and failure is swallowed — the API
      starts anyway against a stale schema. Job has a fixed name, so the 2nd `helm upgrade` fails.
      Related: migration 0001 is `create_all` against live models, so a model change shipped
      without a migration works everywhere fresh and is **missing in production**.
- [ ] **4.7** The deployed UI cannot reach the API — build-time inlining, cluster-internal DNS the
      browser can't resolve, no ingress API route, localhost `CORS_ORIGINS`.
- [ ] **4.8** Containers run as root with no `securityContext`; `restricted` PSS rejects the pods.
      Also `.dockerignore` misses `.venv-rulemind` (482MB) and `.env` — which is baked into the
      web image.
- [ ] **4.9** `RULEMIND_ENV=production` in every manifest; point the `Secure` cookie flags at the
      same helper, or session and admin-JWT cookies ship without `Secure` behind TLS.

## T5 · Operability

- [ ] **5.1** There is no logging configuration at all — every `logger.info` is dropped. An
      operator currently cannot tell why a decision was slow or failed.
- [ ] **5.2** `/metrics` is per-process and wrong (no multiproc dir, no ServiceMonitor).
- [ ] **5.3** Redis has no socket timeouts — a *hung* Redis blocks every threadpool thread.
- [ ] **5.4** 8+N queries per decision; needs a TTL cache **with single-flight**.
- [ ] **5.5** Decision WAL on ephemeral disk, and `recover()` unlinks files another worker is
      appending to — enabling it can lose the decisions it exists to protect.
- [ ] **5.6** Cross-worker read-after-write: the `decision_id` `/decide` returns 404s elsewhere.

## T6 · Data protection defaults

- [ ] **6.1** No default redaction for financial or health PII — a raw card number is stored
      verbatim and returned in full to a **viewer**-role user.
- [ ] **6.2** `rule_results`, `scorecard_result` and `trace` are plaintext, and the trace carries
      evaluated inputs — which substantially defeats encrypting `payload_preview`.
- [ ] **6.3** Make `pip-audit` and `pnpm audit` gating (both `continue-on-error` today).

## Gates

| Gate | Start | Now | Target |
|---|---|---|---|
| Python suite | 892 OK | **978 OK** | monotonically up, SQLite + Postgres |
| qa core | 26 pass / 5 fail | **30 pass / 0 fail / 1 warn** | 31/31 — the warn is T6.1, which must become a *fail* |
| qa sandbox matrix | 21/21 | — | 21/21 + pool wedging, post-abuse re-exec, and an assertion on **which mode ran** |
| Playwright | 53 | — | 53 + WebSocket authz |
| Isolation matrix | CRUD only | — | every mutating route |
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

- [ ] **8.1** Phase 3 quick wins (2–3 wk). **Audited 2026-09-03 against the code, not the
  survey this list came from — two of the eight are already shipped:**
  - ~~connector connection-test~~ — exists: `POST /api/v1/connectors/{id}/test`
    (`routers/authoring.py:96`)
  - ~~expose `decision_table` in the builder palette~~ — shipped in #135 (4 refs in
    `apps/web/app/workflow-builder/page.tsx`)
  - still open, all confirmed absent by grep: sub-workflow **by alias** (the `workflow` step
    type exists at `executor.py:462`; alias resolution does not), nested-key model input
    validation, per-resource rate limits, retention in **months** (`models.py:536` is
    `audit_retention_days`, days only), the 504/evaluation-timeout contract, and polling
    conditions on the `action` step
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
