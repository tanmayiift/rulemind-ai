# Production readiness

An honest assessment, updated as gaps close. Every row below was **checked on a running
system**, not read off the source — which is the distinction the whole document turns on. Four
of the findings here were invisible to a green suite — which is the argument for checking a
running system rather than counting tests. (The suite was ~4,600 then and is 4,998 now; the
point is that the number was never the evidence.)

**Status: not yet production-ready, and the remaining gap is now almost entirely operational.**
The engineering blockers this document opened with are closed — CI runs on every commit, the
paging chain is proven end to end, dependencies are current and the audit is clean. What is left
is a first real deployment, thresholds calibrated against real traffic, and two surfaces that
still need someone's credential.

*Last re-checked 2026-09-18. Every claim below was re-verified against the running system on that
date rather than carried forward — an earlier revision of this file kept "what remains is
registering the runner" as its headline gap for days after the runner was registered and green,
which is exactly the drift it warns about elsewhere.*

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

### Alerting — nothing paged anyone, and then briefly still didn't

`observability/` had a dashboard and **no `alerting` block and no `rule_files`**. Six rules now
exist, each with a runbook anchor, validated three ways: `promtool check rules`, loaded into a
real Prometheus (`health=ok` against live series), and the comparison proven to fire *and* to
stay quiet at the right thresholds.

The one that matters most for this product is `RuleMindOutcomeMixShifted`. Every request can
return 200 in 15 ms while the engine approves everything it should reject — a bureau returning
nulls, a variable erroring to `None`, a policy promoted by mistake. Latency and error-rate
alerts are all silent for that.

**A correction worth keeping.** `alertmanager.yml` was committed with the claim that it "closes
the paging gap". It did not: `prometheus.yml` had no `alerting:` block and Alertmanager was not
in `docker-compose.yml`, so the rules evaluated, turned red on Prometheus's own page, and
reached nobody — indistinguishable from "everything is fine". A monitoring config that looks
present and does nothing is the exact failure this stack exists to prevent, reproduced inside
the monitoring stack itself. Now wired and verified:
`/api/v1/alertmanagers` reports one **active** endpoint,
`http://alertmanager:9093/api/v2/alerts`.

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

**4,998 tests across 15 graded rows, 0 failed, 0 skipped** — and "0 skipped" is enforced rather
than hoped for: `RULEMIND_REQUIRE_ALL_LANES=1` (set in CI) turns a skipped lane into a failure,
after a local run exited 0 having silently not executed 2,085 tests.

Both deployment paths proven by smoke scripts (compose 14/14, kind + Helm 16/16), five engines
held to generated corpora, and the parity backlog closed at 21 PRESENT / 1 CUT / 0 ABSENT.

**Five load-sensitive flakes were removed from the gate in September 2026**, each found by CI
going red on a commit that was fine: fixed millisecond ceilings on work made of `sleep`, a DNS
blip reported as `FAIL - 0 tests`, and a network outage reported as an audit failure. The timing
assertions are now relative — measured against an unbounded run on the same machine moments
apart, so load scales both and cancels. A gate that cries wolf is one people learn to re-run,
and re-running is what hides a real failure.

---

## Open

### ~~1. No CI has ever validated any of this~~ — CLOSED 2026-09-16

This was the headline gap, and it is gone. The self-hosted runner is registered and
`self-hosted tests` has been green on every run since — 15 lanes, 0 skipped.

What it took, and each failure was a real defect rather than a configuration nuisance: a missing
venv; `python3` on the host being Homebrew 3.14 when the project targets 3.11 (so psycopg2-binary
had no wheel and fell back to a source build); a test that silently depended on `boto3` being
installed; and CI silently covering *less* than a laptop because the Rust binding was not built,
which showed up only as a diff in skip counts (CI 29, local 26).

`ci.yml`, `sdk-verify.yml` and `test-report.yml` are retired — every non-emulator job they ran is
folded into the one workflow that runs the same `scripts/run_all_tests.sh` a developer runs.
`backup-verify`, `security` and `load-test` remain, all self-hosted, all green.

**The security constraint that goes with it:** every self-hosted workflow is `workflow_dispatch`
only, never `pull_request` or `push`. The runner executes as the maintainer's user, and there are
bot-authored dependency PRs. That is the decision most likely to be casually reversed.

### 2. Zero production hours — the monitoring chain is now proven; real traffic is not

Nobody has run this against real users. That part cannot be closed from here.

What **was** closed is the specific gap those hours stood for: whether the observability chain
actually works end to end, as opposed to each piece validating on its own. Proven under
sustained load on 2026-09-15 — **53,760 requests at concurrency 48, ~44 decisions/sec**:

```
rule state: FIRING
alerts received: 1
  RuleMindDecisionLatencyHigh severity=warning state=active
     summary: p95 decision latency above 250ms on api
     runbook: docs/runbooks/production.md#latency
```

The whole path: app emits → Prometheus scrapes → rule evaluates → the 600s `for` elapses →
alert fires → **delivered to Alertmanager**, carrying its severity, its `source` label and the
runbook link. `RuleMindDecisionLatencyCritical` stayed `inactive` throughout at p95 ~0.5 s, so
both directions are verified: it fires when it should and stays quiet when it should not.

**That exercise found a defect nothing else did.** Prometheus was never connected to
Alertmanager — no `alerting:` block, no service in compose. `promtool` passed the rules.
`amtool` passed the receiver config. Every component was valid and the chain was broken, so
alerts would have reached nobody while looking configured. It was found by *using* the pipeline,
not by testing its parts.

**Why this still is not "production ready":** three findings this session — absent `/decide`
metrics, absent alert rules, and the unwired Alertmanager — were each invisible to 4,642 passing
tests. Synthetic load found them. Real traffic finds a different class again: cardinality from
real tenant counts, connectors that time out under load rather than on a laptop, payload shapes
nobody modelled. The method is proven; the exposure has not happened.

### 3. Thresholds — calibrated 2026-09-15, against measurement rather than instinct

The first version used round numbers (p95 > 500 ms / > 2 s) chosen because they sounded
reasonable. They are now derived from the measured latency curve and the product's two fixed
points — its own SLO default (p95 ≤ 100 ms) and the evaluation budget (10 s, past which callers
get 504s):

| | was | now | why |
|---|---|---|---|
| warning | p95 > 500 ms | **p95 > 250 ms** | just above the measured knee (212 ms at concurrency 16), so it means "past the point where this scales" rather than "busy" |
| critical | p95 > 2 s | **p95 > 1 s** | only seen beyond 4× the knee; 10× the SLO; still 10× clear of the budget, so it fires long before 504s |

**The uncomfortable result, recorded rather than smoothed over: the product's own default SLO is
met only at concurrency 1, and is at the line by concurrency 4.** Maximum throughput (~94 rps)
arrives at roughly *twice* the SLO latency. So **SLO-respecting capacity (~60 rps) and maximum
capacity (~94 rps) are different numbers**, and the first is the one to size against. The
thresholds are deliberately not set at the SLO value — alerting at 100 ms would fire
continuously on reference hardware and be silenced within a week.

The derivation table lives at the top of `observability/alerts.yml`, because a threshold whose
origin nobody can reconstruct gets "tuned" during the first incident by whoever is most tired.

**Still to do:** re-derive from real production traffic after the first month. The table is a
laptop, and a threshold calibrated against someone else's hardware is still a guess.

### 4. Paging — delivery works; the last hop is one uncommented line

**Alertmanager is deployed and receiving** (compose `observability` profile), with two inhibit
rules so one outage pages once rather than five times. Proven under load: an alert fired and
arrived carrying its severity, `source` label and runbook link.

`scripts/k8s_alerts.sh` ships the same rules to a cluster from the **same file** — no second
copy to drift, which matters because a stale duplicate fails as *"the alert that would have
caught it was the old one"*.

**What is still open is deliberate and is one edit:** the default receiver is an explicit black
hole, because a committed Slack webhook is a leaked credential. Uncomment one receiver in
`observability/alertmanager.yml`, add the real credential, and change `receiver: "null"` in
**both** places — the top-level route *and* the critical sub-route. Missing the second means
criticals silently go nowhere while warnings work, which is worse than neither working.

Still genuinely absent: **no on-call rotation, no severity ladder, no comms template.** The
system knows what is worth waking someone for; the organisation around it does not yet say who.

### ~~5. Known Next.js vulnerabilities~~ — CLOSED 2026-09-17, at a different version than first recorded

First closed at **14.2.35**, a patch bump on the same line. That was superseded: two *critical*
unauthenticated-RCE advisories (`GHSA-p293-qw3h-jr36`, `GHSA-2xp9-vwfh-vxw4`) applied to the whole
14.x line and needed **>= 15.5.24**.

Now on **Next 15.5.25 / React 19.3 / vitest 5**. That upgrade was only possible because three
Dependabot PRs turned out to contain the fix between them — react (#147) and react-dom (#152)
fail individually and pass only together, since Dependabot splits them; and vitest 5 (#174)
closed a third critical (`GHSA-5xrq-8626-4rwp`) that its own description never mentions.

`pnpm audit`: **3 critical + 4 high -> No known vulnerabilities found.** The last four `high`
needed two different fixes — `xlsx` has **no patched version on npm at all** (SheetJS stopped
publishing there, so it is pinned to the tarball they do ship), while vite/postcss/nanoid/esbuild
are transitive and needed *selective* pnpm overrides; a bare range override silently left vite on
the vulnerable version while reporting only an "unmet peer" warning.

### ~~6. Dependency posture~~ — CLOSED 2026-09-18, queue is empty

**Zero open Dependabot PRs.** All 12 resolved: 2 merged, 7 applied to `main` and closed, 2
rejected with evidence, 1 (#171) superseded by #175 which was itself resolved — 17 of its 18
bumps applied, `fastapi` held.

Two things worth carrying out of that round:

* **`fastapi` is pinned deliberately**, and the reason is in `requirements.txt` rather than in a
  doc. 0.141 crosses to starlette 1.x and hangs `tests/test_collab_ws` indefinitely — bisected in
  both directions; uvicorn's own bump is fine. A major dependency jump wearing a minor-version
  label.
* **Two test defects made good dependencies look broken.** A PII-leak assertion that grepped the
  raw webhook body for `b"410"` and false-failed when a tenant UUID contained `4100`; and a
  temp-dir cleanup race that raised *after* the test passed. Had either red been trusted, a fine
  dependency would have been closed as "breaks the suite".

The Android toolchain was also upgraded (Gradle 9.7.1, Kotlin 2.2.20, AGP 9.4.0) because
"blocked on a toolchain bump" stays true forever unless someone does the bump.

Remaining issues: **#123** (AI generation never validated against a real provider — the suite
exists and is skipped without a key) and **#133** (see below).

### 7. Untested surface — the Android SDK module

`packages/sdk-android/rulemind-core` has 46 tests. `rulemind-android` — the module with the
crypto, the network client and the persistence — has **no test source set at all**: 1,644 lines
across NetworkClient (268), RuleMind (254), BundleParser (253), SecurePrefs (151),
SqliteDecisionOutbox (137) and the rest, none of it exercised.

That matters most right now because the Keystore-backed `SecurePrefs` that replaced the
deprecated `EncryptedSharedPreferences` in September 2026 is verified **by compilation**. The
migration it carries is not optional housekeeping: `CryptoManager` holds the device's RSA private
key, whose public half the server already has, so silently regenerating it produces a device that
cannot decrypt bundles addressed to it — a failure that surfaces later and somewhere else.

Issue **#133** predates this and describes emulator jobs in `sdk-verify.yml`, a workflow that no
longer exists; its premise is gone even though this gap is real.

### 8. Still needs the account owner

- **Render services are STILL LIVE and may still be billing.** Re-probed 2026-09-18: the API
  answers `{"status":"ok","version":"4.0.0"}` in **0.3 s** warm, and the dashboard returns 200.
  `scripts/render_teardown.sh` does it in one command once `RENDER_API_KEY` is exported. No
  credential exists on the build machine, and permanently deleting a hosted database is not
  something to automate on someone's behalf.

  Worth recording: the first probe of that re-check returned `000`, and a hasty reading would
  have reported the service dead. It is not. This document already records making exactly that
  mistake once.

- **AI generation has never run against a real provider (#123).** The suite
  (`tests/test_ai_live.py`) and its runner (`scripts/run_ai_live.sh`) exist and skip without a
  key, because the calls are billed. Six endpoints unvalidated.

- **Alertmanager's last hop.** The config ships with a black-hole receiver on purpose: a
  committed Slack webhook is a leaked credential, and a config that silently drops alerts while
  looking configured is worse than none. One uncommented block plus a real destination.

- **Branch protection is unavailable.** GitHub refuses it on a private repo without Pro:
  *"Upgrade to GitHub Pro or make this repository public."* Nothing currently prevents a direct
  push to `main`.

- **Vercel GitHub App** — uninstalled by the owner on 2026-09-16. Deployments and Environments
  have stayed at **0** across every push since, which is the evidence it held.

## What would change the verdict

In order of value. The first item on this list used to be "get CI running"; that is done, which
is why everything below it is now the operational half.

1. **Deploy to a staging environment and leave it running** under synthetic traffic for a week,
   with the alerts wired to something that actually notifies. Every finding in the Closed section
   above needed a running system to surface — none was visible to a green suite.
2. **Route the six alert rules somewhere real** (one uncommented block), so "we have alerts" and
   "we get alerted" stop being different claims.
3. **Recalibrate the thresholds from that traffic.** They are currently derived from measurements
   on one laptop, which is honest but is still a guess about someone else's hardware.
4. **Run one real integration** end to end — a genuine bureau connector, not a sample payload.
5. **Validate the AI endpoints against a real key (#123)** — the suite is written and waiting.
6. **Give `rulemind-android` tests.** 1,644 lines with none, including freshly rewritten crypto.
