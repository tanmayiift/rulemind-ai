# RuleMind AI — agent context

Open-source, source-agnostic **decisioning engine**. Teams plug in JSON data sources
(bureau, bank, GST, device, KYC, custom), author Python variables, click-built rules,
scorecards and multi-step policies in a web dashboard, then promote them
`dev → uat → prod` with full audit trails. Apache 2.0.

State as of 2026-09-14: ~78k LOC, **2 open issues** (#133 Android emulator, #123 live-AI
validation) and **22 open Dependabot PRs** — all red because Actions will not start a job for this repo,
not because they fail. The cause is a **billing block, not a consumed quota**: GitHub's own
annotation on every failed job reads *"The job was not started because recent account payments
have failed or your spending limit needs to be increased."* Jobs are assigned no runner and
execute `steps: 0`, so **consumption is already ~0** — trimming workflows would save nothing.
The fix is either settling billing or making the repo public (public repos get unlimited free
GitHub-hosted minutes). (This line said "0 open issues, 0 open PRs" from a 2026-08-08 snapshot; it is
the kind of count that is wrong the day after it is written, which is why the test numbers below
now come from a generated `TEST_RESULTS.md`.) **Every one of the 75 remote branches is already merged into
`main`** — verified by patch-id, not by branch name. Treat `main` as the single source
of truth and do not go mining branches for lost work.

## Layout (what actually exists)

```
apps/
  python-executor/   FastAPI backend — the canonical runtime. Despite the name this is
                     the whole API, not just a Python sandbox.
  web/               Next.js 14 dashboard, ~38 routes under app/
packages/
  rule-engine/       TypeScript engine (compiler, evaluator, expression, mece, excel-functions)
  rulemind-core-rs/  Rust core (pyo3 bindings)
  rulemind-decide-service/  Standalone Rust /decide service (Cargo, own Dockerfile)
  sdk-android/       Kotlin SDK + on-device engine + sample app
  sdk-flutter/       Dart SDK + on-device engine + sample app
  sdk-js/ sdk-python/ sdk/ shared/ ui/ widget/
e2e/    Playwright     test/   Vitest (TS)     qa/   automated QA suite
infra/ k8s/ helm/ observability/   deployment + Grafana/Prometheus/Tempo
```

`README.md`'s `## Architecture` tree is accurate and includes both Rust packages. This note
previously claimed it was stale and listed `apps/api/` — that was wrong; neither `apps/api/`
nor `apps/worker/` appears anywhere in the README. The genuinely stale block was the SECOND,
duplicate `## Project Structure` table, which named a nonexistent `packages/schemas/` and
carried stale test counts; corrected 2026-09-03.

`docker-compose.yml` is correct: its `api` and `worker` services both build from
`apps/python-executor/Dockerfile`, with `worker` running `python3 -m app.worker`.

### Backend internals — `apps/python-executor/app/`

`main.py` (1972 lines) wires **23 routers** in `app/routers/` (ai, authoring, collab, debugger,
evaluations, experiments, governance, identity, insights, journeys, models, modelsets,
onboarding, operations, platform, policies, predictors, reports, rules, runtime, scim, sdk,
webhook_subscriptions). `storage.py` is a **4979-line** persistence monolith — the largest single file and the usual merge-conflict hotspot. Migrations are
Alembic under `apps/python-executor/alembic/`, run on container start via `entrypoint.sh`.

### Five engines, one semantics contract

The same policy must evaluate identically in Python, TypeScript, Rust, Kotlin and Dart.
Cross-engine parity is enforced by tests, not convention — if you change operator
semantics anywhere, change it in all five and update:
`test/cross-platform-parity.test.ts`, `apps/python-executor/tests/test_operators.py`,
`.../test_large_policy_conformance.py`,
`packages/sdk-android/rulemind-core/src/test/kotlin/com/rulemind/core/LargePolicyConformanceTest.kt`,
`packages/sdk-flutter/test/large_policy_conformance_test.dart`.

## Running it

Prereqs: Node ≥ 18 + pnpm 9.15, Python ≥ 3.10 (Docker image uses 3.11), Docker for the
full stack. The backend is syntactically 3.9-clean, but pin ≥ 3.10 as documented.

```bash
pnpm install && pip install -r apps/python-executor/requirements.txt
pnpm dev                  # web + api in parallel
make up                   # full stack via docker compose (api, web, postgres, redis)
make observability        # + Grafana :3001, Prometheus :9090, Tempo, OTel collector
make test                 # python unittest + vitest
pnpm test:e2e             # Playwright
```

Tests: **4,624 across 11 lanes, all green** — 1928 Python SQLite + 1928 Python PostgreSQL 16,
462 Vitest, 19 + 10 Rust, 163 Flutter, 68 Playwright, 46 Kotlin, plus the contract gates and
`qa/`. **Do not hand-maintain these numbers here** — `make test-all` regenerates
`TEST_RESULTS.md` from the runners' own output, and that file is the authority. The counts in
this file were wrong by 500+ tests before that script existed. The Python suite runs in two lanes: SQLite by default, and real Postgres under
`DATABASE_URL` + `RULEMIND_TEST_PG_SCHEMA_PER_PATH=1` (each test path gets its own Postgres
schema — without that flag `Storage(path=...)` silently falls back to SQLite and the Postgres
lane is a near-no-op).

## Configuration

`.env.example` was regenerated on 2026-08-20 from the code that actually reads each value
(`os.environ` / `os.getenv` under `apps/python-executor/app/`) plus `docker-compose.yml`.
It now documents every backend-read variable, with in-code defaults, grouped by concern,
and marks which entries belong to the web app or to compose rather than the API.

What it looked like before, in case similar drift recurs: it was missing 46 variables the
backend reads — including `RULEMIND_CONFIG_KEY` (encryption master key) and
`RULEMIND_ADMIN_JWT_SECRET` — while carrying 8 that nothing reads (the whole MinIO block,
plus `LOG_LEVEL`, `RATE_LIMIT_RPM`, `DECISION_TIMEOUT_MS`, `AUDIT_RETENTION_DAYS`). It also
pinned `rulemind_v3.db`; `app/db.py` defines `DEFAULT_SQLITE_URL` as `rulemind_v4.db`.

Two gotchas worth keeping in mind:

- **`AUTH_MODE` unset is not the same as `none`.** `middleware.py` tests
  `os.getenv("AUTH_MODE") == "none"`, so leaving it unset leaves auth **on**. The README
  previously implied otherwise.
- **The dev secrets in `.env.example` are real defaults, not placeholders.**
  `RULEMIND_CONFIG_KEY`, `RULEMIND_ADMIN_JWT_SECRET`, `RULEMIND_ADMIN_PASSWORD` and
  `RULEMIND_DEV_API_KEY` all fall back to committed dev values if unset.

`INTEGRATION.md` §8 is correct as of 2026-09-05: it is titled "variables you are most likely
to need" and says in bold that it is a selection covering 24 of ~97. This note previously
claimed it still advertised "the full environment-variable list" — that was itself stale.
`.env.example` remains the authority.

## AI layer (`app/ai.py`)

BYO-key, **three providers** (Anthropic + OpenAI + Gemini — `SUPPORTED_PROVIDERS` at
`ai.py:24`, all three with a `_call_*` and a `_fetch_live_models_*`; this note said
"dual-provider" until 2026-09-14). Default is `claude-sonnet-5`. Three
model-list staleness bugs were **fixed on 2026-08-20**; described here so the shape of the
problem is recognisable if the lists drift again:

1. **`CURATED_MODELS["anthropic"]` omitted the current flagship** — it had no
   `claude-opus-5` and no `claude-haiku-4-5`. Now lists opus-5, sonnet-5, haiku-4-5,
   opus-4-8, sonnet-4-6. This is only a fallback: with a key configured, the live
   `/v1/models` fetch supersedes it.
2. **`_PRICING` over-billed Opus ~3x.** A bare `opus` substring mapped to $15/$75 per
   MTok, but Opus 4.6/4.7/4.8/5 are $5/$25. The table is now ordered specific-key-first
   (`opus-4-1` before `opus`, `haiku-4-5` before `haiku`) because `estimate_cost` takes
   the **first** key that is a substring of the model id — keep that ordering when
   adding rows.
3. **`_TEMPERATURE_FREE_RE` was too narrow.** It matched only the `-5` family, but
   Opus 4.7 and 4.8 also reject `temperature`. This never surfaced as a user-visible
   failure because `_temperature_rejected()` retries once without the field — it just
   cost an extra round trip and a logged 400 on every such call.

`packages/rule-engine/` and the SDKs contain no AI calls; AI is backend-only, gated by
`RULEMIND_SEED_DEMO`-style config flags (see the AI gating work in `main`).

## Conventions

- Commits are Conventional Commits with a scope: `feat(engine):`, `fix(web):`,
  `refactor(api):`. Every change lands through a squash-merged PR referencing `(#NNN)`.
- Docs are written eagerly alongside features and **drift**. So does this file: on 2026-09-05
  three of its own claims were checked and all three were wrong — `INTEGRATION.md` (fixed
  above), `CHANGELOG.md`'s "still says 0.1.0 initial scaffold" (it is a per-package index at
  4.0.0), and, in `PLAN.md`, "PostgreSQL 16 → 18" (16 is what docker-compose, the Helm chart,
  the k8s manifests and CI all pin). **Verify any doc claim against source before acting on
  it, including the claims in this file** — a stale note here costs every session, because a
  "known problem" that no longer exists is more expensive than an unknown one.
- CI: `.github/workflows/ci.yml` and `sdk-verify.yml`.
