# RuleMind AI — End-to-End Test Plan & Findings Log

**Living document.** Last full pass: 2026-08-27. Environment: macOS 26.6 (Darwin 25.6, arm64) · Python 3.11.16 · Node 20.20.2 ·
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

## 10. Regression baseline
| Suite | Result |
|---|---|
| Vitest (TS) | ✅ **157/157** across 9 files |
| Flutter SDK unit tests | ✅ **126/126** |
| Demo-app unit tests | ✅ 4/4 |
| Demo-app iOS integration tests | ✅ **20/20** |
| Python unittest | ✅ **708 passed**, 4 skipped, 0 failed (189 s) |
| Kotlin core (`rulemind-core`) | ✅ **18 passed** |
| Playwright `e2e/` | ⬜ (config pins ports 8100/3100 + system `python3`) |

---

# Findings

**21 found · 20 fixed · 1 open.** Every fix was verified against the running stack and
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
