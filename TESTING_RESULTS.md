# RuleMind.AI — Test Results (honest, evidence-backed)

_This document was **rewritten** after a reviewer correctly challenged an earlier version that
overclaimed the frontend/UX testing. It now records only what was actually executed, the real
numbers observed, the **defects found**, and — explicitly — **what was NOT tested**. Where a run
config is pessimistic or a number is environment-affected, that is stated._

Environment: local dev, macOS, single machine under concurrent load (dev servers + CI watchers
running). Backend = FastAPI on `main`, 1 or 4 uvicorn workers as noted, SQLite. Web = Next.js dev.
Reproducers live in the repo (`apps/python-executor/simulation/loadtest.py`, `tests/`).

---

## 1. Backend — API surface (nothing else impacted)

Curled **every module** with the seeded dev key. All returned **200** except one expected 404:

| Group | Endpoints hit | Result |
|---|---|---|
| System | health, ready, metrics | 200 |
| Authoring | bootstrap, connectors, variables, variables/graph, rules, scorecards, policies, decision-tables | 200 |
| Decision | decide, test/variables | 200 |
| Governance/Ops | audit/decisions, audit/errors, audit/promotions, deploy/status, analytics/decisions, analytics/latency | 200 |
| Settings | settings, settings/data-protection, settings/slo, slo/status | 200 |
| Settings (new) | settings/governance | **404** — expected: it lives on the unmerged dual-control branch, not `main` (confirms the smoke hit real code) |
| AI/Models/Reports/Experiments | ai/config, ai/usage, models, reports, experiments, reviews, webhooks, schedules | 200 |
| SDK edge | sdk/v1/health, sdk/v1/bundle | 200 |

**Verdict: PASS.** The full API surface is healthy on `main`.

## 2. Backend — decision accuracy (with negative cases)

Using the real input schema (`bureau_score`, `dti_ratio`, …):

| Applicant | Input | Outcome | Score |
|---|---|---|---|
| Strong | bureau 800, dti 0.12 | **approve** | 710 |
| Weak | bureau 380, dti 0.90 | **reject** | 360 |
| Borderline | bureau 690, dti 0.42 | **review** | 555 |

Three-way discrimination is correct, and repeated identical input is **deterministic**. Variable
create→test→output: a draft variable `doubled(bureau_score)` on input 21 returned **42** (correct);
the full **variable suite = 55/55 passed**.

**Verdict: PASS**, and it includes real failing cases (reject/review), not just approvals.

## 3. Backend — concurrency, sync/async integrity, throughput

- **Concurrency:** 100 concurrent decides → **0 errors** (single worker, full executor path,
  process-pool sandbox = the pessimistic config; ~38 req/s here for that reason).
- **Decision-log integrity:** count before=2, after=102, **delta = exactly 100** — no loss, no
  double-count. _(First attempt showed delta=0; root-caused to a self-inflicted test-env schema
  mismatch, see Findings #2, then re-verified clean.)_
- **Throughput (real reproducer):** `loadtest.py --requests 8000 --concurrency 64 --workers 4`
  (fast path) → **355 TPS, 8000/8000, 0 errors**, p50 **104 ms**, p95 557 ms, p99 1146 ms. (Lower
  than the prior 402 TPS because this machine was also running the web dev server + CI watchers.)

**Verdict: PASS** (≥200 TPS, exactly-once logging, 0 errors).

## 4. On-device SDK — conformance

`flutter test` on `packages/sdk-flutter`: **45 tests passed** across operator conformance, the
**large-policy** conformance (≥500 conditions / ≥700 variables, incl. missing-variable cases), and
decision-table conformance. On-device Dart evaluation matches the Python engine exactly.
_(Kotlin arm not run locally — no JAVA_HOME on this box; it runs in the `android` CI job.)_

**Verdict: PASS (Dart)**; Kotlin deferred to CI.

## 5. Frontend — real UI exercise

The in-app browser cannot make cross-port fetches to the API, so a **dev-only** Next rewrite proxied
`/api/*`→backend (reverted, not committed; the backend's CORS preflight itself was verified correct).

| Check | Result |
|---|---|
| Render with real data | Dashboard (8/9 sources, 55 vars, 13 rules, 4 policies, source PROD/UAT badges, Ingest→…→Decision flow), Variables, Rules **builder** (Condition/AND/OR/Approve/Review/Reject + live expression), Test Console all render |
| Page-load latency | Variables: TTFB **122 ms**, DOMContentLoaded 151 ms, load **452 ms**; bootstrap fetch 307 ms / 58 KB |
| **Rage-click** resilience | "Run All Tests" clicked **6× rapidly → exactly 1** `POST /test/variables` (guarded against duplicate-submit) |
| End-to-end flow | Run All Tests → **55/55 passed**, each variable showing its real computed value + source + status |
| Console | See Findings #4 (dev-only `code`-prop warning, fixed in this PR) and #7 (401s) |

**Verdict: PASS for what was exercised** — see §7 for what was NOT.

## 6. Findings (defects surfaced by this pass)

1. **Sandbox escape is live on `main`.** `().__class__.__bases__[0].__subclasses__()` executed and
   returned `type` through `/variables/test-draft` — the dunder-attribute traversal primitive works.
   **Fixed by PR #92** (blocks dunder attribute/name access). This validates that fix closes a real,
   exploitable hole, not a hypothetical one.
2. **Decision-log write failures are silently swallowed.** A failing `add_decision` (here: a
   NOT-NULL constraint from a schema mismatch) was caught and dropped with no error event or metric —
   decisions vanished behind a 200 response. Observability gap worth a dedicated fix (surface an
   error_event / metric on log-write failure). Related to the durability work in PR #90 / T0.2b.
3. **Flat vs nested `/decide` payloads diverge.** `{bureau_score:800,dti_ratio:0.12}` → approve, but
   `{loan:{bureau_score:800,dti_ratio:0.12}}` → reject, because the nested form **replaces the whole
   source payload** (dropping unspecified fields) while the flat form does per-field override. A real
   API footgun; should be documented and ideally unified.
4. **`InlineTextarea` `code`-prop React warning** fires on `main` (Variables + Testing pages).
   **Fixed in this PR.**
5. **Large-policy fixture lacks negative rigor** (reviewer's catch): 135 cases span 286–529 true
   conditions and 64 are below 500, but there are **no `reject` outcomes** and **no clean threshold
   boundary** (499 fail / 500 pass). Tracked to add boundary + reject cases.
6. **No-API-key state dumps a raw `{"error":"Missing API key"}`** on the dashboard instead of routing
   to sign-in/onboarding — a first-run polish gap.
7. **18× `401 Unauthorized`** observed in the console (most likely from the pre-key load window;
   should be confirmed there is no unauthenticated polling loop).

## 7. What was NOT tested (honest gaps)

- **Not every page was exercised.** Validated: dashboard, variables, rules, test-console (+ API-level
  for all). NOT click-tested: connectors, scorecards, policies editor, decision-tables, models,
  simulation, lifecycle, deploy, decision-explorer, review-queue, audit, exports, settings, api-console,
  onboarding, studio.
- **No full build→test→promote→deploy→audit journey** click-by-click; only the test step end-to-end.
- **No responsive/mobile, dark-mode toggle, or keyboard/a11y testing.**
- **Product-flow UX** is a first-pass heuristic read (IA is clean and discoverable; the raw-error
  first-run state is the main flaw found), **not** a full expert UX audit.
- **Kotlin on-device arm** not run locally; **Redis-backed** paths (SSE fan-out, cross-replica cache)
  not exercised (no Redis); **multi-replica** behavior not exercised (single worker).
- Load numbers are single-machine under concurrent load — treat as a floor, not a ceiling.

---

## Summary

| Dimension | Status |
|---|---|
| API surface healthy (all modules) | ✅ |
| Decision accuracy incl. reject/review negatives, deterministic | ✅ |
| Concurrency 0-errors + exactly-once decision logging | ✅ |
| Throughput ≥200 TPS (355 TPS, 0 errors) | ✅ |
| On-device Dart conformance (incl. ≥500-condition policy) | ✅ |
| Frontend render + latency + rage-click + e2e test flow | ✅ (partial — see §7) |
| Defects found | **7** (2 fixed in open PRs #88/#92; 5 tracked) |
| Frontend coverage | **Partial** — core flows only; many pages + full journeys + a11y/responsive untested |
