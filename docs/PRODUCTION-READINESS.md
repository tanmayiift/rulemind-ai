# Production readiness

An honest assessment, updated as gaps close. Every row below was **checked on a running
system**, not read off the source — which is the distinction the whole document turns on. Four
of the findings here were invisible to a green 4,600-test suite.

**Status: not yet production-ready, and the remaining gap is operational rather than
engineering.** The blockers are a first real deployment, CI that actually runs, and thresholds
calibrated against real traffic.

---

## Closed

### Sandbox security — audited adversarially

The variable sandbox runs **untrusted customer Python**. It was previously untested against
attack. It is now, and it held: **20 techniques, all refused.**

The reason it holds is not the restricted-builtins list — that stops nothing on its own,
because `().__class__.__bases__[0].__subclasses__()` reaches `subprocess.Popen` with *zero*
builtins. What holds the line is the static rejection of dunder attribute traversal, plus
blocking `str.format`/`format_map`, whose format-spec resolves attributes at **runtime** and so
walks past any static name check.

Covered: the canonical `__subclasses__` escape (two traversals), function `__globals__`,
`format`/`format_map` specs, `getattr` indirection, f-string attribute access, `__import__`,
`open`, `exec`, `eval`, exception `__traceback__` frame-walk, `type()`, attribute access hidden
inside a comprehension, `vars`, `import os`, `import socket`. Plus three resource-exhaustion
vectors — infinite loop, memory bomb, ReDoS — all bounded at the deadline.

Pinned by `tests/test_sandbox_escape_matrix.py`. Mutation-checked: re-adding `getattr` to
`SAFE_BUILTINS` — the plausible "helpful" regression — fails the suite *and* demonstrates the
escape is real, returning `<class 'tuple'>`.

**Residual, stated plainly:** `RLIMIT_AS` bounds memory on Linux but **darwin refuses to set
it**, so on a developer host the timeout is the only thing holding a memory bomb. Production
runs Linux containers, where both apply. And `SANDBOX_FALLBACK_MODE=inline` runs customer code
*in the API process* with no RLIMIT and no process boundary — `/ready` reports that as
`degraded`, and it should be treated as an incident, not a performance note.

### Observability — decisions were invisible

**`/api/v1/decide` emitted no metrics at all**, on either of its two paths. `DECISIONS_TOTAL`
and `DECISION_LATENCY` were incremented only in `routers/sdk.py`. So `/metrics` reported nothing
for production decision traffic — no rate, no latency histogram, no outcome mix — while the
Grafana dashboard sat there with nothing to draw.

Invisible to every other kind of test: the endpoint returned correct decisions and the suite was
green. It surfaced only by scraping `/metrics` on a running stack after driving real traffic
through it. Fixed, labelled `source=api` vs `api_fast`, wrapped so a metrics failure can never
break a decision, and pinned by `tests/test_decide_emits_metrics.py` (mutation-checked).

### Alerting — nothing paged anyone

`observability/` had a dashboard and **no `alerting` block and no `rule_files`**. Six rules now
exist, each with a runbook anchor, validated three ways: `promtool check rules`, loaded into a
real Prometheus (`health=ok` against live series), and the comparison proven to fire *and* to
stay quiet at the right thresholds.

The one that matters most for this product is `RuleMindOutcomeMixShifted`. Every request can
return 200 in 15 ms while the engine approves everything it should reject — a bureau returning
nulls, a variable erroring to `None`, a policy promoted by mistake. Latency and error-rate
alerts are all silent for that.

### Capacity — measured, and it degrades rather than collapsing

| | |
|---|---|
| Knee | **concurrency ~16, ~94 rps** (2 uvicorn workers, laptop, Colima VM) |
| Past the knee | 4× over: **0 server errors, 0 timeouts**, max 1.55 s — slower and still correct |
| Soak | 40 s at the knee, 10 windows, **zero errors**, p50 331–503 ms, no drift |
| Overload | HTTP 429 in ~44 ms with a retry hint, at 1000 rpm/tenant — shedding, not queueing |

See `docs/PERFORMANCE.md`. One unexplained observation is recorded there rather than dropped: a
single 120-second request at concurrency 64 that has not reproduced across two re-tests.

### Backup and restore — rehearsed on both lanes

| Lane | Result |
|---|---|
| SQLite | 41 tables / 165 rows, chain verified |
| **PostgreSQL 16** | 103 MB dump · 42 tables → 42 · decisions 11,801 → 11,801 · **audit chain verifies in the restored copy** |

**Finding: `pg_dump`/`pg_restore` are not in the API image.** The deployed container cannot run
its own documented backup procedure. See `docs/runbooks/production.md`.

### Correctness — the standing gate

4,600+ tests across 11 lanes, both deployment paths proven by smoke scripts (compose 14/14,
kind + Helm 16/16), five engines held to generated corpora, and the parity backlog closed at
21 PRESENT / 1 CUT / 0 ABSENT.

---

## Open

### 1. No CI has ever validated any of this — the biggest single gap

Every repo workflow has **zero successes**: ci (24 runs), sdk-verify (24), security (26),
test-report (3), backup-verify (3). Jobs are assigned no runner and execute `steps: 0`.

The cause is a **billing block, not an exhausted quota** — GitHub's own annotation: *"The job
was not started because recent account payments have failed or your spending limit needs to be
increased."* Consumption is already ~0, so trimming workflows saves nothing.

**Every test result in this repository is one person's local run on one machine.** That is the
honest description of the evidence, however green it is.

Three ways out: settle billing; make the repo public (public repos get unlimited free
GitHub-hosted minutes — `docs/PRE-PUBLICATION-AUDIT.md` establishes that would be safe); or a
self-hosted runner, which is free even on a private repo but should be wired to
`workflow_dispatch` only, never `pull_request`, with 20 open bot-authored dependency PRs.

### 2. Zero production hours

Nobody has run this against real traffic. The previous deployment cycle found **four bugs that
1,400 passing tests could not see**, including a readiness probe that never checked the
database. The two findings in this document — metrics and alerting — are the same species: both
needed a running system, not a test suite.

### 3. Thresholds are reasoned, not calibrated

Alert thresholds derive from laptop measurements and the product's own SLO defaults. Re-derive
them from real traffic after the first month. A threshold nobody has tuned against real data is
a guess written down confidently.

### 4. No paging path

The rules define what is worth waking someone for. **Alertmanager is not deployed**, so nothing
routes them to a human. No on-call rotation, no severity ladder, no comms template.

### 5. A known Next.js vulnerability is pinned, and Dependabot has not flagged it

The web dev server prints, on every start:

> `WARN deprecated next@14.2.34: This version has a security vulnerability. Please upgrade to a
> patched version. See https://nextjs.org/blog/security-update-2025-12-11`

**None of the 20 Dependabot PRs is a Next.js upgrade.** This is the most concrete security item
on the list and it needs a deliberate upgrade with the e2e lane run against it.

### 6. Dependency posture — triaged 2026-09-15

All 20 PRs verified locally, since no CI can run on them. **5 merged, 3 rejected, 2 blocked on a
toolchain bump, 5 unverifiable without Actions, 5 needing a Dependabot rebase.** Full reasoning
per PR in `docs/DEPENDABOT-TRIAGE.md`.

The three rejections each passed their own lane first, which is the point worth carrying:
numpy 2.5.3 cannot install on Python 3.11 (and a harness bug of mine hid that), TypeScript 7
type-checks clean but breaks `next dev` under Next 14, and Playwright 1.63's Chromium blocks the
localhost WebSocket a security test depends on.

2 open issues remain, including #123: the AI generation endpoints have never been validated
against a real provider key.

### 7. Still needs the account owner

- **Render services are live and may be billing.** Re-confirmed: 32.6 s cold, then 0.15 s warm.
  `scripts/render_teardown.sh` does it in one command once `RENDER_API_KEY` is exported. No
  credential exists on the build machine, and permanent deletion of a hosted database is not
  something to automate on someone's behalf.
- **Vercel GitHub App** — `gh api /user/installations` returns 403 with an *empty*
  `X-Accepted-Oauth-Scopes`; no scope grants it. Browser + 2FA is the only path.

---

## What would change the verdict

In order of value:

1. **Get CI running** — it converts "tested on one laptop" into "tested on every commit".
2. **Deploy to a staging environment and leave it running** under synthetic traffic for a week,
   with the alerts wired to something that actually notifies. Both of this round's findings
   needed a running system to surface.
3. **Deploy Alertmanager** and route the six rules somewhere real.
4. **Run one real integration** end to end — a genuine bureau connector, not a sample payload.
5. **Recalibrate thresholds** from that traffic.
