# RuleMind AI — End-to-End Test Plan & Findings Log

**Living document.** Counts in §10 refreshed **2026-09-05**; the narrative findings below are from the last full pass: 2026-08-27, against **PostgreSQL 16** and a **full Docker stack**, with the **AI layer validated against a live provider key**. Environment: macOS 26.6 (Darwin 25.6, arm64) · Python 3.11.16 · Node 20.20.2 ·
pnpm 9.15.4 · Flutter 3.47.1 / Dart 3.13.1 · Xcode 26.6 · CocoaPods 1.11.3 · **no Docker**.

Legend: ✅ pass · ⚠️ pass-with-finding · ❌ fail · ⛔ blocked · ⬜ not run

---

## 0. Environment bring-up  — ✅ complete

The machine had **no Docker, no Node, no pnpm, no Homebrew, and only Python 3.9** (project requires ≥3.10).
Everything below was installed from official sources into `~/.local` without sudo.

| # | Step | Status |
|---|------|--------|
| 0.1 | Python 3.11.16 (astral python-build-standalone) | ✅ |
| 0.2 | Node 20.20.2 + pnpm 9.15.4 | ✅ |
| 0.3 | `pip install -r requirements.txt` | ✅ (**F-01** fixed) |
| 0.4 | xgboost / lightgbm loadable (libomp) | ✅ (**F-02** documented) |
| 0.5 | Alembic migrations → head (16 revisions) | ✅ |
| 0.6 | API boots, `/health` responds | ✅ |
| 0.7 | `.env` actually consumed | ✅ (**F-03** fixed) |
| 0.8 | Web dashboard builds + serves (Next 14, :3000) | ✅ |
| 0.9 | Flutter demo app builds — iOS ✅ / Android ✅ | ✅ |
| 0.10 | CocoaPods (system Ruby 2.6 dependency chain) | ✅ |

---

## 1. Platform / tenancy / auth — ✅
| # | Case | Result |
|---|------|--------|
| 1.1 | Unauthenticated request rejected | ✅ 401 |
| 1.2 | Seeded dev API key accepted | ✅ |
| 1.3 | Malformed / empty key rejected | ✅ 401 |
| 1.4 | API-key CRUD + `viewer` role cannot mutate | ✅ no privilege escalation |
| 1.9 | Rate limiting enforced + configurable | ✅ token bucket, env-configurable, `RateLimit-*` headers (**F-12** fixed) |

## 2. Data sources & import — ⚠️
| # | Case | Result |
|---|------|--------|
| 2.1 | Connector list + schema | ✅ 9 connectors |
| 2.2 | `/import/validate` well-formed | ✅ |
| 2.3 | `/import/validate` malformed entity | ❌→✅ **F-09** was a 500; fixed |
| 2.7 | CSV → decisions (simulation page parser) | ✅ RFC-4180 correct (quotes, escaped quotes, embedded commas/newlines, BOM) |
| 2.8 | CSV parser on **evaluations** page | ✅ shared RFC-4180 parser (**F-10** fixed) |

## 3. Authoring & Python sandbox — ✅ (21/21)
| # | Case | Result |
|---|------|--------|
| 3.1 | CONTROL — benign variable returns correct value | ✅ (validates the whole matrix) |
| 3.2.1–15 | Escape matrix: `import os`, `__import__`, `open()`, subclass-walk to `Popen`, `eval`, `exec`, `globals()`, `__globals__`, `__mro__`, `str.format` leak, `importlib`, env read, socket egress, `subprocess`, file write | ✅ **all blocked** |
| 3.3 | Infinite loop terminated by timeout | ✅ ~2 s |
| 3.3b | 1 GB memory bomb — API survives | ✅ |
| 3.3c | Unbounded recursion — worker survives | ✅ |
| 3.3d | Catastrophic regex (ReDoS) | ✅ guarded ~2 s |
| 3.10 | Excel function library | ✅ 232 functions |

> The first run produced 7 false "blocked" passes because a schema error was being read as a block.
> Added a mandatory benign control case; the matrix above is the trustworthy re-run.

## 4. Rules evaluation & decisioning — ✅
| # | Case | Result |
|---|------|--------|
| 4.1 | `/decide` returns an outcome | ✅ |
| 4.1b | Unknown policy → 404 not 500 | ✅ |
| 4.4 | Empty / null / wrong-type / oversized payloads | ✅ no 5xx |
| 4.7 | Determinism (same payload ⇒ same outcome) | ✅ |
| 4.9 | Batch decide | ✅ (contract differs from `/decide` — **F-13**, open) |
| 4.5 | `FAST_DECIDE` vs executor **parity** | ✅ **120/120 agree** |
| 4.10 | Cross-engine parity (TS suite) | ✅ 22/22 |
| 4.11 | Operator conformance (TS suite) | ✅ 91/91 |

## 5. Rule visibility & explainability — ✅
| # | Case | Result |
|---|------|--------|
| 5.1 | Trace covers every policy step | ✅ |
| 5.2 | Per-condition variable / operator / threshold / value / passed | ✅ |
| 5.3 | Decision id returned by `/decide` | ✅ (**F-11** fixed) |

## 6. Case-wise history & audit — ⚠️→✅
| # | Case | Result |
|---|------|--------|
| 6.1 | Per-subject decision history | ❌→✅ **F-04** `client_id` never persisted; fixed |
| 6.1b | Query decisions by `client_id` | ❌→✅ **F-05** no filters existed; added |
| 6.2 | Audit records mutations | ✅ (initial failure was **F-06**, an id-rewrite artefact) |
| 6.6 | PII redaction honoured on fast path | ❌→✅ **F-07** fixed |
| 9.x | Reports: create/preview/run/CSV export | ✅ 947 rows, correct `content-type` + `content-disposition` |
| 9.6 | CSV-injection on export | ✅ 0 dangerous cells |

## 7. Performance — ✅
Engine measured in-process (HTTP numbers are dominated by the rate limiter).

| Metric | Executor (full, logging) | `FAST_DECIDE` engine | Ratio |
|---|---|---|---|
| p50 | 1.792 ms | **0.129 ms** | **13.9×** |
| p95 | 1.989 ms | 0.164 ms | |
| p99 | 2.138 ms | 0.193 ms | |
| throughput | 552 rps | **7 429 rps** | 13.5× |

HTTP `/decide`: p50 12.3 ms, p95 13.4 ms, p99 15.0 ms at concurrency 1.
At concurrency 16, 222/400 requests returned **429** — the hardcoded 1 000 req/min tenant cap (**F-12**).

## 8. SDK & on-device engine — ✅ working
| # | Case | Result |
|---|------|--------|
| 12.1 | Edge bundle served | ✅ |
| 12.2 | SDK parses the bundle | ✅ (**F-15b** fixed) |
| 12.3 | SDK decrypts the bundle | ✅ (**F-16** fixed — keyless bundles are readable) |
| 12.3b | SDK verifies the signature | ✅ (**F-22/F-23** — key published, ASN.1 parser fixed) |
| 12.4 | Demo app — iOS build + 20 integration tests | ✅ |
| 12.5 | Demo app — Android APK build | ✅ (**F-14** fixed) |
| 12.6 | Flutter SDK unit tests | ✅ 126/126 |
| 12.7 | **Cross-engine parity, server vs on-device** | ✅ **16/16 exact match on outcome AND score** |

## 9. Web dashboard — ✅ 33/36 clean
36 routes swept headless at 1440×900 (`qa/e2e-full/web_sweep.mjs`).
* 33 render clean, no console/page/network errors.
* `/admin`, `/branding` — 401 console noise (admin JWT required; expected, ungraceful).
* `/settings` — React prop warning → **F-17**, fixed.
* Sidebar IA — **F-18**.

## 10. Regression baseline — re-run against merged `main` on **PostgreSQL 16**

Refreshed **2026-09-05**. Every count in the previous version of this table was from the
2026-08-27 pass and had roughly halved in accuracy since: Python read 733 against an actual
1457, Vitest 157 against 443, Flutter 127 against 163. A stale baseline is worse than no
baseline, because a real regression that lands between two passes reads as "the number went up
anyway".

| Suite | Result | Was (2026-08-27) |
|---|---|---|
| Python unittest — SQLite | ✅ **1457 passed**, 21 skipped, 0 failed | 733 |
| Python unittest — **PostgreSQL** | ⬜ not re-run in this pass — see the note below | 733 |
| Vitest (TS) | ✅ **443 passed** (15 files) | 157 |
| Flutter SDK | ✅ **163 passed** | 127 |
| Kotlin `rulemind-core` | ✅ **46 passed** | 20 (both modules) |
| Kotlin `sample-app` | ✅ **4 passed** | — |
| Rust `rulemind-core-rs` | ✅ **19 passed** (`--no-default-features`) | — |
| Rust `rulemind-decide-service` | ✅ **10 passed** | — |
| Playwright `e2e/` | ✅ **58 / 58** | ⚠️ 44 / 48 (#125) |

The Postgres lane is marked not-run rather than assumed. It is the same suite, but the two lanes
diverge for real — `RULEMIND_TEST_PG_SCHEMA_PER_PATH=1` is required or 66 modules construct
`Storage(path=…)` and silently re-run SQLite, which is exactly how a "Postgres pass" can be
green without having tested Postgres. Reporting yesterday's number as today's would be the same
mistake in a different place.

Playwright now runs 58, not 48: `e2e/authz.spec.ts` and `e2e/authz-boundaries.spec.ts` were added
for #139, and five of those tests are gated behind `RM_E2E_ALLOW_DESTRUCTIVE=1` — set in CI, and
required to reproduce 58/58, because without it the two tenant-boundary tests skip.

**24** Alembic migrations (not 16) — `apps/python-executor/alembic/versions/`.

PostgreSQL 16 is correct and unchanged: `docker-compose.yml`, the Helm chart, the k8s manifests
and CI all pin `postgres:16`.

Two claims that stood here are **removed rather than carried forward**, because both were
cross-lane assertions and the Postgres lane was not re-run: "all 16 journey scenarios return
outcomes identical on Postgres and SQLite", and the table count "(32 tables)". They go back in
when the Postgres lane does.

## 11. Load — measured on PostgreSQL 16, 4 workers, limiter off
| Config | conc 4 | conc 16 | conc 32 |
|---|---|---|---|
| defaults | 73 rps · p50 50 ms | 109 rps · p50 138 ms | 107 rps · p50 246 ms |
| `COMPUTE_ONLY_USED_VARS=1` | 135 rps · p50 24 ms | 157 rps · p50 93 ms | 151 rps · p50 199 ms |
| `+ FAST_DECIDE=1` | 115 rps · p50 31 ms | 198 rps · p50 73 ms | **207 rps · p50 132 ms** |

A single worker on Postgres plateaus at ~47-51 rps and holds there; SQLite fell from
45 rps to 28 rps over the same range. Throughput scales with worker **processes**,
not with request concurrency. Outcome distributions were byte-identical throughout.

## 12. AI layer — validated against a live Anthropic key
| # | Case | Result |
|---|------|--------|
| 12.1 | Connectivity (`/ai/test`) | ✅ |
| 12.2 | Live model list supersedes the curated fallback | ✅ 11 models incl. `claude-fable-5`, `claude-opus-4-7` |
| 12.3 | `generate-rule` — valid draft, real variable ids | ✅ |
| 12.4 | `generate-policy` — valid steps, real rule ids | ✅ |
| 12.5 | `explain-decision` — reason codes match the gates that fired | ✅ |
| 12.6 | `generate-predictor` — valid scorecard | ✅ |
| 12.7 | `analyze-experiment` — real variant statistics | ✅ |
| 12.8 | `analyze-rejections` | ✅ (was a **500** — truncated JSON, fixed) |
| 12.9 | Scope guardrail refuses off-topic **without a provider call** | ✅ 0.00 s, usage unchanged |
| 12.10 | Usage accounting vs real token counts | ✅ **exact** — $0.050757 for 7,079 in / 1,968 out |
| 12.11 | Budget guardrail | ✅ 402 once exceeded |
| 12.12 | Live suite `tests/test_ai_live.py` (key-gated) | ✅ 8/8 |

### Accuracy test — 500 simulated claims, known ground truth
Constructed a 62.4% reject rate with a deliberate dominant driver, then asked the
product's own "Explain rejections" feature (API **and** the UI's AI Copilot) to find it.

| Ground truth (by construction) | AI reported | |
|---|---|---|
| 266 rejects — prior claims > 2 | 266 fails · 53.2% of claims · 65.5% of outcomes | ✅ exact |
| 94 reviews — missing documents | 94 fails · 18.8% · 23.2% | ✅ exact |
| 46 rejects — policy age < 3 months | 46 fails · 9.2% · 11.3% | ✅ exact |
| 94 clean approvals | approve: 94 | ✅ exact |

It named **"Prior Claims (12m) <= 2"** as the dominant driver — correct — and reported
claim amount as a *non*-factor rather than inventing one.

## 13. Docker — full stack, no sudo
Docker Engine 29.5.2 via Colima + Lima on Apple's Virtualization framework (Docker
Desktop needs admin; this does not). `docker compose up --build` from an empty volume:

| Service | Status |
|---|---|
| api · db · redis · web · worker | ✅ all healthy |

All 16 journey scenarios correct against the containerised stack. Two startup bugs
found and fixed that **no repo-based test could reach** — see F-30 and F-31.

## 14. Frontend — element-level audit against the containerised production build
Not "does the page render" but every interactive control on every route, at three
viewports, in both themes.

| Check | Before | After |
|---|---|---|
| Routes with element findings | 8 / 36 | **0 / 36** |
| Controls with no accessible name | 24 (8 buttons, 16 inputs) | **0** |
| Layout/theme problems across 3 viewports | 1 genuine | **0** |
| Keyboard focus ring on sampled controls | — | ✅ all |
| Dark mode via the in-app toggle | — | ✅ applies |

**F-32 — 24 controls had no accessible name.** Connector on/off switches and variable
source-filter chips are icon-only, so a screen reader announced 8 unnamed buttons with
no state. Sixteen inputs across `/batch`, `/collab`, `/exports`, `/rules`, `/settings`
and `/test-console` had `<label>` elements sitting *beside* them with no `htmlFor`/`id`
pairing, which assistive tech does not associate. WCAG 2.1 4.1.2 and 3.3.2.

**F-33 — the Decision Explorer table was clipped on mobile.** Its container used
`overflow: hidden` while the table needs ~429 px in a ~341 px column at 375 px wide, so
the Latency column was cut off with no way to scroll to it and the page did not scroll
either. Now scrolls inside its own container.

Two findings my first audit reported turned out to be **false positives**, checked
before reporting: the "spill" on 13 mobile routes was entirely content inside
`overflow-x: auto` containers (54/54 on `/simulation`, genuine = 0) — correct
behaviour for wide tables; and dark mode "not applying" was the app deliberately using
an explicit toggle rather than following `prefers-color-scheme`. Both audit scripts
were corrected so they do not mislead the next person.

Flows driven in the browser against the Docker build: environment switching filters
DEV/PROD correctly, a policy trace renders the full explainability chain
(connector → per-condition rule results with actual values → scorecard), dark mode
holds legibility, and report preview returns real rows.

---

## 15. Mobile responsiveness & SDK integration — ✅ (with one limitation named)

Both Flutter apps had **zero** width-reactive primitives: no `MediaQuery`,
`LayoutBuilder`, `FractionallySizedBox`, `AspectRatio`, `OrientationBuilder` or
`ConstrainedBox`, and no breakpoint logic anywhere.

**Automated matrix** — 6 surfaces (320×568, 393×851, 430×932, 320×1024 iPad split,
834×1112, 1194×834) × text scales 1.0 / 1.3 / 2.0, in `demo-app/test/responsive_test.dart`
and `packages/sdk-flutter/example/test/responsive_test.dart`. Written **before** any fix:

| app | before | after |
|---|---|---|
| `demo-app` | 28 / 54 failing (8 at default 1.0x) | 54 / 54 ✅ |
| `sdk-flutter/example` | 58 / 126 failing (18 at default 1.0x) | 126 / 126 ✅ |

Overflows reached 506 px. Failing at 1.0x means these were broken at normal settings, not
only at accessibility sizes. Proven load-bearing: reverting only the INPUTS-row fix turns
10 cases red.

**The harness is itself tested** (`packages/flutter-ui/test/harness_self_test.dart`, 6
cases) because making the spill check clip-aware could otherwise have reduced it to a
no-op. Three framework internals were probed and confirmed benign rather than assumed —
at 320×568 a `ChoiceChip` sits at 16.0..248.9 while its hidden checkmark slot reports
−1.0..25.0; a `NavigationBar` sits at 0.0..320.0 while its indicator reports 280.0..344.0;
a `TextFormField` sits at 16.0..304.0 while its editable region reports 16.0..1091.5.

**⛔ Known limitation — widget tests cannot catch real-font text clipping.** Flutter renders
tests with a placeholder font, so text width in a test ≠ text width on a device. Proven, not
theorised: at 2.0x on an iPhone simulator the chip "Serial claimant, new policy" was
truncated by the screen edge while the same screen at 393×851 @2.0x reported nothing out of
bounds in a test. **A device pass is therefore mandatory, not optional.** Use
`demo-app/lib/main_visual_probe.dart` (documented `simctl` commands in its header).

**Device pass** — iPhone 17 @1.0x and @2.0x, iPad Pro 11 @1.0x. Screenshots in
`qa/results/mobile-visual/`, including before/after of the chip fix. iPad confirms the
adaptive path is real: scenario chips collapse to one row and the INPUTS row becomes
horizontal above the 600 dp breakpoint. ✅

Android emulator not exercised locally — the machine has the Android SDK but no
`cmdline-tools` and no AVD, and CI's emulator is blocked by a runner-image regression
(issue #133).

**SDK integration.** The demo never drained the decision outbox and never read
`serverOnlyStepsSkipped`, so a partial on-device decision was shown with a green
"as expected" tick against the server's expected outcome — which quietly undermined the
parity claim the app exists to make. Now disclosed as "partial — not comparable" with every
skipped step named, and the outbox is drained per evaluation with depth shown. `sdk-js`,
`sdk-python` and `sdk` had no README at all; all three now have one.


# Findings

**46 found · 45 fixed · 1 open.** Every fix was verified against the running stack and
the full regression suite re-run afterwards.

### Shipped — PR #114 · setup & configuration
| ID | Sev | Finding |
|----|-----|---------|
| **F-01** | High | `requirements.txt` pinned `scipy==1.18.0` for *all* of macOS, but 1.18.0 needs Python ≥3.12 — on macOS with the documented 3.10/3.11 the **entire** `pip install` aborted. Added `python_version` guards. |
| **F-03** | High | **Nothing loaded `.env`** — the README's `cp .env.example .env` + `AUTH_MODE=none` was a silent no-op, so auth stayed on and every request 401'd. Added `python-dotenv` + a loader in `app/__init__.py`; real env still wins. |
| **F-06** | Medium | `.env.example` assigned inline **comment text as the value** for 8 empty vars, switching tracing on with a sentence as the OTLP endpoint. Not a security hole — the two dangerous flags use strict allowlists. |
| **F-02** | Medium | macOS `libomp` prerequisite for xgboost/lightgbm was undocumented. Now in the README and `requirements.txt`. |
| **F-21** | Low | `package.json` `migrate` pointed at a different `alembic.ini` than `entrypoint.sh`. |

### Shipped — PR #115 · configurable rate limiting
| ID | Sev | Finding |
|----|-----|---------|
| **F-12** | High | Limit was **hardcoded** (`5000 if enterprise else 1000`/min) with no env var and no per-tenant setting; `.env.example` recorded `RATE_LIMIT_RPM` as "read by nothing". Replaced with a Redis-Lua token bucket: per-tenant → per-plan env → global env → built-in default, `RATE_LIMIT_ENABLED=0` for load tests, `RateLimit-*` headers on every response. |

### Shipped — PR #116 · case history, ids, health
| ID | Sev | Finding |
|----|-----|---------|
| **F-04** | High | `client_id` was **never written** on the `/decide` path, so per-customer history was unreachable through the API. |
| **F-05** | High | `/api/v1/audit/decisions` had **no filters at all**; the Decision Explorer fetched unfiltered and filtered client-side. Added SQL-level `client_id`/`policy_id`/`outcome`. |
| **F-07** | High | `FAST_DECIDE=1` silently **disabled tenant PII redaction** and dropped `user_id`. |
| **F-08** | High | `k8s/rulemind.yaml` probed `/health/ready`, which **404'd** → pods never Ready. `/ready` returned **200** even when `ready:false`, so the Helm gate never fired. Both fixed; 503 on failure. |
| **F-06b** | High | `POST` **silently rewrote client-supplied ids** on every entity type, breaking idempotent provisioning and export→import. Now honoured, 409 on collision. |
| **F-11** | Medium | `/decide` returned no decision id, so a caller could not deep-link the case it just created. |
| **F-20** | Low | Unset Redis reported as `error`/`degraded` though it is a documented supported mode. |
| **F-09** | Medium | `/import/validate` returned **500** on a malformed tree — the endpoint exists to survive malformed input. |

### Shipped — PR #117 · SDK on-device decisioning
| ID | Sev | Finding |
|----|-----|---------|
| **F-16** | **Critical** | The bundle was **cryptographically undecryptable**: a fresh AES key per request, discarded when no client key was sent. Keyless bundles are now readable; `SDK_BUNDLE_REQUIRE_CLIENT_KEY=1` enforces the per-device wrap. |
| **F-22** | High | The server **never published its signing public key**, so the SDK — which refuses any bundle it cannot verify — could never verify one. Added `GET /sdk/v1/signing-key`. |
| **F-23** | High | `RSAKeyParser` did not skip the DER BIT STRING's unused-bits octet, so key parsing threw and surfaced as "Invalid bundle signature". |
| **F-24** | High | The SDK sent the **server's signing key** as `X-Client-Public-Key` — two opposite roles in one field, and a raw PEM is not a legal header value. |
| **F-25** | **Critical** | **Every compiled variable evaluated to 0 on all three edge engines.** The compiler passes the lookup key and default through registers; Dart, Kotlin and Rust read them as literals. Dart/Kotlin also never parsed `default` and bound `payload` to the whole payload instead of the source-scoped slice. |
| **F-15** | High | `RuleMind.initialize()` never initialised Hive → threw on any real device. CI missed it because the unit tests call `Hive.init` themselves. |
| **F-15b** | High | `BundleEnvelope.fromJson` hard-cast nullable fields, throwing before the SDK's own fallback could run. |
| **F-14** | High | `workmanager: ^0.9.0` resolved to a plugin using the legacy Kotlin Gradle Plugin — **every Android build depending on the SDK failed**. |

### Shipped — PR #118 · web
| ID | Sev | Finding |
|----|-----|---------|
| **F-10** | High | The model-evaluation CSV parser used naive `split(",")`: quoted numbers became `NaN`-strings and quoted commas **silently dropped data**, so Gini/AUC/KS were wrong on a standard Excel export. Both pages now share one RFC-4180 parser. |
| **F-18** | Medium | The shell grid let the sidebar stretch past the viewport where `body{overflow:hidden}` clipped it — nine nav items unreachable at 1280×720. |
| **F-19** | Low | 25 collapsed nav links had **no accessible name** (WCAG 2.4.4 / 4.1.2). |
| **F-17** | Low | `InlineInput`/`InlineSelect` spread a custom `testId` prop into the DOM. |

### Shipped — PR #120 · AI model catalogue
| ID | Sev | Finding |
|----|-----|---------|
| **F-26** | Medium | `_PRICING` **over-billed Opus by 3x** — a bare `opus` key mapped to the legacy $15/$75 while Opus 4.6–5 are $5/$25. Fable 5 and Haiku 4.5 were absent and fell through to the mid-range default. |
| **F-27** | Low | `_TEMPERATURE_FREE_RE` missed Opus 4.7/4.8, costing an extra round trip and a logged 400 on every such call (the retry masked it). |
| **F-28** | Low | `CURATED_MODELS["anthropic"]` omitted the current flagship — what a keyless install sees. |
| **F-29** | Low | `pnpm-lock.yaml` still carried importers for `apps/api` and `apps/worker`, workspaces that no longer exist. |

### Shipped — PR #132 / #134 · mobile responsiveness & SDK integration

| ID | Sev | Finding |
|---|---|---|
| F-34 | High | Neither Flutter app used any width-reactive primitive; 28/54 and 58/126 matrix cases failed, 26 of them at default text size. Overflow up to 506 px. |
| F-35 | High | `demo-app` never read `serverOnlyStepsSkipped`, so a partial on-device decision was reported as agreeing with the server — undermining the parity claim. |
| F-36 | Medium | The decision outbox was never drained by the demo, so `POST /sdk/v1/decisions` was never exercised; no visibility into pending depth (`RuleMind.pendingDecisionCount()` added). |
| F-37 | Medium | `sdk-js`, `sdk-python` and `sdk` shipped with no README. |
| F-38 | Medium | The responsive harness intersected every ancestor clip **including the root view**, so anything the screen cut off looked safely clipped. |
| F-39 | Medium | Scenario chips were uncapped; at 2.0x a content-driven name was truncated by the screen edge. Only visible on a device — see the limitation in §15. |
| F-40 | Low | `demo-app/lib/engine.dart:64` hardcodes an API key in a shipped app (same smell as `test/load/evaluate.k6.js:15`). |
| F-41 | Low | CI uploaded the entire Flutter build tree every run with no retention: 817 artifacts / 134 GB, which blocked all uploads once the repo went private. |

### Still open
| ID | Sev | Finding |
|----|-----|---------|
| **F-13** | Low | `/decide` takes `{policyId, payload, userId}` but `/decide/batch` takes `{targetType, targetId, payloads}` — the same shape as the internal `/test/batch`. Harmless but inconsistent; changing it is a breaking API change, so it wants a deliberate decision rather than a drive-by fix. |

## Artefacts produced
```
E2E_TEST_PLAN.md                     this document
qa/e2e-full/harness.py               result-recording harness
qa/e2e-full/suite_core.py            auth · import · authoring · decide · trace · audit
qa/e2e-full/suite_sandbox.py         21-case sandbox escape + resource-limit matrix
qa/e2e-full/bench.py                 HTTP latency/throughput benchmark
qa/e2e-full/seed_journeys.py         4 non-lending journeys (idempotent, id-aware)
qa/e2e-full/journey_expectations.json 16 scenario expectations
qa/e2e-full/web_sweep.mjs            36-route headless sweep
qa/e2e-full/web_sweep_results.json   sweep output
demo-app/                            Flutter demo — iOS + Android, 4 journeys
demo-app/integration_test/           20 on-device E2E tests
```

## Non-lending journeys (built for the demo app + used as live conformance cases)
| Sector | Policy | Gates | Scenarios |
|---|---|---|---|
| Insurance | Motor Claim Triage | documentation · fraud signals · high-value referral | clean → approve · missing docs → review · serial claimant → reject · high value → review |
| E-commerce | Checkout Fraud Screening | chargeback block · velocity · device & address | trusted → approve · chargeback abuser → reject · card testing → review · new account → review |
| Marketplace | Seller Onboarding (KYB) | sanctions · identity & banking · category risk | verified → approve · sanctions → reject · incomplete KYB → review · high-risk category → review |
| Telecom | Postpaid Eligibility | KYC · payment behaviour · line limit | loyal → approve · no KYC → reject · late payer → review · line limit → review |

All 16 scenarios produce the designed outcome, with the correct gate named as the reason. ✅
