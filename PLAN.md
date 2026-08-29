# RuleMind — execution plan

**Status:** `6/46 complete` · started 2026-08-28 · sections 1–2 in flight

Sections 0–6 are days of work and are genuinely finishable. Sections 7–8 are **months**
(audit immutability alone is 2–3 weeks; the graph canvas 3–6 months). Those boxes are
expected to stay unchecked for a long time — that is the honest state, not a stall.

Rules: work in order · after each step run the relevant suite, tick the box, commit ·
no pausing for confirmation between steps · a real blocker goes under **Blockers** below
with what was tried, then move to the next step.

---

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

- [ ] **1.1** `#131` CI artifact quota — first, it unblocks the rest
- [ ] **1.2** `#130` frontend a11y + mobile clipping
- [ ] **1.3** `#135` builder step types (independent of the mobile stack)
- [ ] **1.4** `#132` responsive mobile
- [ ] **1.5** `#134` SDK integration (retarget to main once #132 lands)
- [ ] **1.6** `#136` demo redesign (retarget once #134 lands)
- [ ] **1.7** Verify main green; delete merged local + remote branches

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
      **Answered:** run against a seeded backend gives **25 pass / 5 fail / 1 warn**, so
      `suite_core` goes in **non-gating** initially. `suite_sandbox` is **21/21 clean** and
      gates. The 5 remaining core failures are product-level, filed separately: 3.10 excel
      library returns 0 functions, 4.9 batch decide payload shape (the known F-13), 5.3
      reason-codes 404, 6.1 finds 1 of 3 decisions, 6.2 rule missing from the audit trail.
      Note the suite is not idempotent — it accumulates backend state between runs.
- [x] **2.5** `suite_core.py:219` filters `/api/v1/audit/decisions?userId=…`. `userId` is not
      an accepted param (`insights.py:68`) and the row has no `user_id` (`storage.py:780-801`)
      — FastAPI drops it, so the `len(mine) >= 3` assertion at `:226` matches a key that never
      exists. Filter on `client_id`.

## 3 · Testing gap 1 — the offline→online round trip

Python, not Dart: `RuleMind._httpClient` (`rulemind.dart:33`) is private with no injection
seam, and `resetForTest` (`:355-366`) never clears `_decisionOutbox`/`_decisionSyncer`, so
outbox state leaks between tests.

- [ ] **3.1** New `apps/python-executor/tests/test_sdk_offline_round_trip.py`, house setUp
      from `test_sdk_decision_sync.py:9-47` incl. `os.environ.pop("AUTH_MODE", None)` so the
      test traverses real auth
- [ ] **3.2** Round trip: record built from the **exact** `_toPending` key set
      (`rulemind.dart:164-187`); offline = never POST; assert audit empty → POST → assert
      `{received:1,inserted:1,duplicates:0,acked:[id]}` → assert exactly one row.
      **Key detail:** the SDK's `executionId` lands in the audit row's **`client_id`**, not
      `id` (`storage.py:1385`, `:782`) — so query `?client_id=<executionId>`
- [ ] **3.3** Idempotency: replay identical batch → `{inserted:0,duplicates:1}`, still one row
- [ ] **3.4** Multi-decision drain, oldest-first, `acked` covers every id
- [ ] **3.5** `/sdk/v1/signing-key` — **zero** test references today. Assert 200/401, RS256,
      PEM parses, and that it **verifies a real `/sdk/v1/blocks/{id}` signature** (pattern at
      `test_decision_block.py:85-92`)
- [ ] **3.6** Dart companion: `SqfliteDecisionOutbox` via `sqflite_common_ffi` through a stub
      uploader that fails then succeeds past `backoffMs`
- [ ] **3.7** File an issue for the SDK testability defect (leaky `resetForTest`, no
      injectable client)

## 4 · Testing gap 2 — wire the unrun suites into CI

- [ ] **4.1** *Free tier (≈0 wall clock).* New parallel `rust` job —
      **`cargo test --no-default-features`**, because `default = ["python"]` → pyo3
      `extension-module` deliberately does not link libpython and a plain `cargo test` fails
      at link time. Append 3× `flutter test` (`demo-app`, `packages/flutter-ui`,
      `packages/sdk-flutter/example` — **~200 widget tests currently unrun**) to the existing
      `flutter` job. Append `suite_sandbox` to `repo-validation`, reusing
      `qa/mobile_e2e/start_fastapi.sh`.
- [ ] **4.2** *Cheap tier.* Postgres `services:` container + matrix dim on the Python step.
      `DATABASE_URL` is the only config — `storage.py:480,488` calls `create_all` and
      `psycopg2-binary` is already in requirements. Alembic **not** required.
- [ ] **4.3** Playwright as its own parallel job (**48 tests, not 52** — the 52 in
      `docs/parity/` is stale), paths-filtered
- [ ] **4.4** k6 — **do not gate.** `RULE_ID` defaults to the literal `"replace-me"`
      (`evaluate.k6.js:13`) and `p(95)<500` at 50 VUs will not hold on a 2-core runner.
      `workflow_dispatch` + nightly.
- [ ] **4.5** Delete the dead `qa/*.ts` suite + its 3 root scripts.
      `run-automated-suite.ts:6` imports `../apps/api/src/server`, which does not exist; four
      files hardcode `/Users/tanmaykumar/Downloads/RuleMind.AI` — a different machine. Keep
      `qa/workbook-runner.ts` (healthy, imported by `test/workbook-matrix.test.ts`).
- [ ] **4.6** Rotate the hardcoded key out of `demo-app/lib/engine.dart:77` — a **shipped**
      app, so it lands in the APK/IPA and `strings` recovers it
- [ ] **4.7** Add `concurrency` groups to both workflows (neither has one, so every push runs
      a duplicate set)
- [ ] **4.8** File an issue for `playwright.config.ts:45,60` setting `AUTH_MODE=none` on both
      servers — `middleware.py:93` early-returns on it, bypassing API key, RBAC **and** CSRF,
      so all 48 browser tests exercise a permanently unauthenticated app

## 5 · Track B0 — real linting

- [ ] **5.1** Replace `py_compile app/*.py` with `ruff check` + `ruff format --check`. That
      glob matches 57 top-level files and misses all 22 under `app/routers/` and `app/core/`.
- [ ] **5.2** `mypy` with a per-module ratchet — no clean sweep of the 3515-line `storage.py`
- [ ] **5.3** Keep the `apps/web` vitest runner honest (one real test landed in #135)

## 6 · Track B2 — security scanning *(currently none of any kind)*

- [ ] **6.1** CodeQL (python + javascript)
- [ ] **6.2** `dependabot.yml`
- [ ] **6.3** `pip-audit` + `pnpm audit --audit-level=high`
- [ ] **6.4** `CODEOWNERS`

## 7 · Track B3–B6 — the revenue blockers *(weeks each)*

- [ ] **7.1** B3 audit immutability (2–3 wk) — **biggest deal-blocker**. `AuditEvent` has no
      hash chain and the archiver purges the hot copy, so an operator with DB access can
      rewrite history undetectably. The Alembic revision must mark pre-existing rows
      *pre-chain* rather than fraudulently verified.
- [ ] **7.2** B4 usage metering + quota (2–3 wk) — **no way to bill today**
- [ ] **7.3** B5 tenant-isolation matrix (1 wk), parameterised over the router list
- [ ] **7.4** B6 SCIM, backup with a *verified* restore, `Idempotency-Key`, OpenAPI diff gate
- [ ] **7.5** Close issues #123, #125, #127

## 8 · Phases 3–8 — parity *(months; each needs a checkpoint before starting)*

- [ ] **8.1** Phase 3 quick wins (2–3 wk)
- [ ] **8.2** Phase 4 outbound webhooks, p12, predictor, DLQ (4–6 wk)
- [ ] **8.3** Phase 5 canary, modelset, step-through debugger (6–8 wk)
- [ ] **8.4** Phase 6 Monaco + matrix entity (6–10 wk)
- [ ] **8.5** Phase 7 graph canvas, journey entity (**3–6 mo** — a re-architecture; do not
      start until 3–6 are green)
- [ ] **8.6** Phase 8 Kafka, feature factory (**6+ mo** — build only against a paying
      requirement)

---

## Blockers

**FinBox authorship — PR #1 only, and unfixable from here.** All 12 commits authored by
`Tanmay <tanmay.kumar@finbox.in>` lived on one stale branch, `claude/kind-herschel`, which is
now deleted. Verified: 0 FinBox commits across all 80 remote branches, 0 in main's 155
commits (author, committer **or** body), and `stats/contributors` now returns only
`tanmayiift` and `claude`.

What remains is `refs/pull/1/head` = `72ff307`, which GitHub retains permanently for the
merged PR #1. That ref is GitHub-managed and read-only; PRs cannot be deleted through the
API. **Only GitHub Support can purge it**, or the repo would have to be recreated from clean
history. The commit is unreachable from every branch.

Backups of the deleted branch: local ref `backup/kind-herschel-finbox`, bundle at
`/tmp/kind-herschel-finbox.bundle`.
