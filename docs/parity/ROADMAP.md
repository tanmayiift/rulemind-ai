# Roadmap

Sequenced by dependency and value, not changelog order. Sizes assume 1–2 engineers and are
honest: a "days" item has its substrate in the repo already; a "months" item needs a new
subsystem.

## Phases

| phase | scope | size | blocks / blocked by |
|---|---|---|---|
| **0** | CI unblock: artifact retention, runner-image workaround | 0.5 day | blocks everything |
| **1** | Responsive mobile, both Flutter apps + automated matrix | 7–10 days | — |
| **1.7** | SDK integration correctness, missing SDK READMEs | 4–6 days | credibility of the SDK story |
| **2** | This analysis (`docs/parity/`) | 3–4 days | blocks phases 3+ |
| **3** | Quick parity wins, no new entities (P01, P06, P08, P16, P17-twin, P18; ~~P21~~ and ~~P22-palette~~ shipped 2026-09) | 2–3 weeks | — |
| **4** | Outbound webhooks, p12, predictor entity, DLQ + Idempotency-Key (P02, P03, P09-partial, P20) | 4–6 weeks | feeds phase 5 metrics |
| **5** | Canary, modelset, step-through debugger (P04, P05, P11) | 6–8 weeks | pays the zero-coverage test debt |
| **6** | Monaco, matrix entity (P07, P13) | 6–10 weeks | — |
| **7** | Graph canvas, superflows, journey entity (P15, P19, P22-canvas) | **3–6 months** | needs 3–6 green |
| **8** | Kafka/exactly-once, feature factory (P09-full, P12) | **6+ months** | build on demand only |

## Track B — B2B readiness, in parallel

Not a phase. It runs alongside from Phase 3 onward, because B0–B2 gate the credibility of
every test claim made anywhere else.

| id | scope | size | why it matters |
|---|---|---|---|
| **B0** | Real linting: ruff + mypy | 2 days | `lint`, `typecheck` and `build` are all the same `py_compile app/*.py`, which misses all 22 files under `app/routers/` and `app/core/`. `apps/web` declares vitest with zero tests. |
| **B1** | Wire the unrun suites into CI + a Postgres service | 3–5 days | Playwright (52), all of `qa/e2e-full/` **including the 21-case sandbox-escape matrix**, k6, `cargo test` — none run in CI. CI is SQLite-only. |
| **B2** | Security scanning | 2 days | **None of any kind** today: no CodeQL, Dependabot, semgrep, trivy, bandit, pip-audit, `pnpm audit`; no `CODEOWNERS`. |
| **B3** | **Audit immutability** | 2–3 weeks | **Biggest deal-blocker.** No hash chain, no `prev_hash`, no WORM; the archiver purges the hot copy. |
| **B4** | **Usage metering + quota** | 2–3 weeks | **No way to bill.** Only AI token spend is metered. |
| **B5** | Tenant-isolation test matrix | 1 week | Two tests cover 17 routers. Parameterise over the router list so new routers are covered automatically. |
| **B6** | SCIM, backup/restore with a *verified* restore, `Idempotency-Key`, OpenAPI diff gate | 4–6 weeks | Enterprise review items. |

## What to say to whoever funds this

**B3 and B4 block revenue more than any parity feature does.** A decisioning platform that
cannot prove its audit log is immutable and cannot meter decision volume is not sellable to a
regulated B2B buyer, regardless of how many changelog rows it matches. Neither appears on the
changelog at all.

**Phase 7 is a re-architecture, not a feature.** Sequencing it after 3–6 is not conservatism —
it is the difference between a shippable product and a half-migrated one.

**Phases 0–2 are about two weeks** and are the prerequisite for trusting anything else here.

## Definition of done, per feature

No feature is complete until it satisfies the standing checklist in
[`LENS-QA.md`](LENS-QA.md): tenant isolation, RBAC, an audit event, an updated OpenAPI spec,
and at least one negative test — plus its named E2E flow added to `E2E_TEST_PLAN.md` and its
row in `parity.json` updated.
