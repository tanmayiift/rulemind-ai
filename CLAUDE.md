# RuleMind AI — agent context

Open-source, source-agnostic **decisioning engine**. Teams plug in JSON data sources
(bureau, bank, GST, device, KYC, custom), author Python variables, click-built rules,
scorecards and multi-step policies in a web dashboard, then promote them
`dev → uat → prod` with full audit trails. Apache 2.0.

State as of the last push (2026-08-08, `main` @ `41ed11f`): 113 merged PRs, ~78k LOC,
0 open issues, 0 open PRs. **Every one of the 75 remote branches is already merged into
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

`main.py` (1629 lines) wires 18 routers in `app/routers/` (ai, authoring, collab,
evaluations, experiments, governance, identity, insights, models, onboarding, operations,
platform, policies, reports, rules, runtime, sdk). `storage.py` is a 3515-line persistence
monolith — the largest single file and the usual merge-conflict hotspot. Migrations are
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

Tests: 124 Python modules (1369 tests), 13 Vitest files (379), 4 Playwright specs (58), plus
`qa/`. The Python suite runs in two lanes: SQLite by default, and real Postgres under
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

`INTEGRATION.md` still advertises "the full environment-variable list" but covers only 14
of them — treat `.env.example` as the authority.

## AI layer (`app/ai.py`)

BYO-key, dual-provider (Anthropic + OpenAI). Default is `claude-sonnet-5`. Three
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
- Docs are written eagerly alongside features and **drift** — `README.md`, `.env.example`
  are behind the code. `CHANGELOG.md` is no longer among them: it is now a per-package index
  and this note's "still says 0.1.0 initial scaffold" was itself the stale claim.
  Verify any doc claim against source before relying on it.
- CI: `.github/workflows/ci.yml` and `sdk-verify.yml`.
