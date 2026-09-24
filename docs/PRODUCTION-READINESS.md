# Production readiness

An honest assessment, updated as gaps close. Every row below was **checked on a running
system**, not read off the source — which is the distinction the whole document turns on. Four
of the findings here were invisible to a green suite — which is the argument for checking a
running system rather than counting tests. (The suite was ~4,600 then and is larger now — see
`TEST_RESULTS.md` for the current figure; the point is that the number was never the evidence,
which is also why three different totals were quotable from two documents at once.)

**Status: not yet production-ready.** The engineering blockers this document opened with are
closed — the gate runs every lane on a self-hosted runner, the paging chain is proven end to
end, dependencies are current and the audit is clean. What is left is a first real deployment,
thresholds calibrated against real traffic, and two surfaces that still need someone's
credential.

This paragraph used to say **"CI runs on every commit."** It does not, and it never did.
`self-hosted-tests.yml:45-46` is `workflow_dispatch` only — deliberately, for the
runner-security reason this document states correctly 200 lines below at *"the security
constraint that goes with it"*. Nothing runs automatically on a push. The summary contradicted
its own body, and a summary is what gets read.

Nor is the gap "almost entirely operational". Since that sentence was written, three code
defects have been found by running the system rather than testing it: `/metrics` reported one
uvicorn worker instead of the pod, the alert rule that watches the alerting system could never
fire, and **the ML model feature was dead in every Linux deployment**. Each was invisible to the
full suite. See *What running it found* below.

*Last re-checked 2026-09-19. Every claim below was re-verified against the running system on that
date rather than carried forward — an earlier revision of this file kept "what remains is
registering the runner" as its headline gap for days after the runner was registered and green,
which is exactly the drift it warns about elsewhere.*

**A green workflow is not evidence either, and this document nearly repeated its own mistake.**
On 2026-09-19 all four workflows were green and reported as such. `load-test` was then opened
rather than counted, and **both** of its k6 scripts turned out to be proving nothing: the
throughput sweep's correctness check failed on 100% of requests (it read `body.outcome` where the
endpoint nests it under `result`), hidden by `continue-on-error: true` and by the script
declaring no `checks` threshold; and the saturation sweep's assertion is conditional on a
variable failing to compute, which against a healthy server never happens — so it printed `PASS`
having executed no assertion at all. Both are fixed and both now gate the job. The lesson is the
same one this file opens with, one level up: **a green check mark is a claim, and the claim has
to be opened.**

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

**The `load-test` workflow now gates rather than reports** (run 35432346555, 2026-09-19):

| Sweep | Result |
|---|---|
| evaluate, 50 VUs / 30 s | **2,828 of 2,828 checks pass**, `http_req_failed` 0.00%, p(95) 1.4 s |
| saturation, ramp to 60 VUs | **1,894 degraded responses, 0 unsound decisions** |

The second row is the one worth reading twice. The sweep asserts that a decision is never derived
from an input that failed to compute, and that assertion only runs when something *does* fail —
so a healthy server produced a `PASS` that executed nothing. `test/load/saturation_setup.py` now
arms the run deliberately (one variable slower than the evaluation timeout, verified with a real
decision before the sweep starts) and the script refuses to call an unsaturated run a pass. The
1,894 is the guard actually firing under load, and holding.

Mutation-checked through the product's own escape hatch rather than a source edit: with
`engine_config.fail_open_on_variable_error` set, the same sweep reports unsound decisions by name
and exits non-zero.

**And the throughput half was measuring the wrong thing until 2026-09-19.** The lane never set
`RATE_LIMIT_ENABLED=0`, which `rate_limit.py`'s own docstring calls *"the supported way to run a
load test or benchmark without the limiter shaping the result"* — so it measured the rate
limiter, and its verdict depended on whether the run happened to fit under the 1500 rpm cap:

| Run | Requests | Failed | Verdict |
|---|---:|---:|---|
| 14:05 | 1426 | 0.00% | pass |
| 20:46 | 2014 | 25.57% | fail — **exactly 1499 succeeded** |
| 20:48 | 1990 | 24.67% | fail — **exactly 1499 succeeded** |
| after the fix | 1563 | 0.00% | pass, and *above* the old ceiling |

"Exactly 1499, twice" is a ceiling, not contention. Throughput varies with latency, latency
varies with whatever else the machine is doing, and the limit does not move — so the earlier
greens were not evidence the system was fast, only that the run was small enough to fit under
it. **A load test whose pass/fail flips on machine speed is worse than none**, because it reads
as a performance regression and sends someone hunting for one.

### Backup and restore — rehearsed on both lanes

| Lane | Result |
|---|---|
| SQLite | 41 tables / 165 rows, chain verified |
| **PostgreSQL 16** | 103 MB dump · 42 tables → 42 · decisions 11,801 → 11,801 · **audit chain verifies in the restored copy** |

**Finding: `pg_dump`/`pg_restore` are not in the API image.** The deployed container cannot run
its own documented backup procedure. See `docs/runbooks/production.md`.

### Correctness — the standing gate

**16 graded rows, 0 failed, 0 skipped lanes** — the count lives in `TEST_RESULTS.md`, which
`make test-all` regenerates from the runners' own output. Three different totals (4,998, 5,092,
5,142) were in circulation across two documents because the number kept being typed by hand;
cite the generated file, not a figure in prose.

"0 skipped" is enforced rather than hoped for — `RULEMIND_REQUIRE_ALL_LANES=1` (set in CI) turns
a skipped lane into a failure, after a local run exited 0 having silently not executed 2,085
tests. **But it is a LANE-level guarantee, not a test-level one.** 31 individual tests are gated
off by an unset environment variable and are invisible to it: `REDIS_URL` (10),
`RULEMIND_AI_LIVE` (5), `KAFKA_BOOTSTRAP_SERVERS` (5 — the whole delivery half of the P09
exactly-once row), `CLICKHOUSE_URL` (3), `S3_TEST_ENDPOINT` (3), and
`RM_E2E_ALLOW_DESTRUCTIVE` (5 Playwright). Two lanes, `device` and `smoke`, sit outside the
default `LANES` list in `scripts/run_all_tests.sh:41` altogether, while
`self-hosted-tests.yml:49` describes a blank `lanes` input as "blank for all" — blank runs 12 of
14.

The 16th row is new and it is the one that answers *can this still ship*: **the image build**.
On 2026-09-19 `docker compose build` failed outright — `requirements.txt` pinned
`scipy==1.18.1; sys_platform != "darwin"` with no `python_version` guard, scipy 1.18.x needs
Python ≥ 3.12, and the image is `python:3.11-slim` on Linux, so pip aborted the entire install.
**The deployable artifact had been unbuildable for days behind a fully green suite**, because
every lane installs into a host venv (darwin, a different marker) and `docker compose up` reuses
an already-built image. `smoke_compose.sh` would have caught it but is opt-in, since it needs
four ports to itself; the build half needs none, so it is now split out and always runs.

Both deployment paths proven by smoke scripts — **re-run on 2026-09-19 against a freshly built
stack, not carried forward: compose 14/14, kind + Helm 16/16, 0 skipped.** Between them they
cover the things a green unit suite cannot: liveness surviving the database being down while
readiness correctly reports 503, the sandbox spawning in a real process pool inside the
container, the API running as non-root (uid 10001), the migrate Job completing, no container
restarting, an unauthenticated caller rejected, and a real decision end to end through each.

`scripts/k8s_alerts.sh` also applied all 8 alert rules to the cluster — it detects the absence of
prometheus-operator and falls back to the sidecar ConfigMap form, which is the path a plain
Prometheus needs.

Five engines held to generated corpora, and the parity backlog closed at 21 PRESENT / 1 CUT /
0 ABSENT.

**Five load-sensitive flakes were removed from the gate in September 2026**, each found by CI
going red on a commit that was fine: fixed millisecond ceilings on work made of `sleep`, a DNS
blip reported as `FAIL - 0 tests`, and a network outage reported as an audit failure. The timing
assertions are now relative — measured against an unbounded run on the same machine moments
apart, so load scales both and cancels. A gate that cries wolf is one people learn to re-run,
and re-running is what hides a real failure.

---

## What running it found — 2026-09-23

Six defects, all found by driving the running system rather than by adding tests, and every one
of them invisible to a full green suite. They are grouped here because the pattern is the same
every time: **a check existed, reported success, and never executed its assertion.**

### 1. `/metrics` reported one uvicorn worker, not the pod

`Dockerfile:55` runs `--workers 2`; `prometheus_client` counts per process. Twelve consecutive
scrapes of `rulemind_decisions_total` on the running stack:

    280781 280781 175310 175310 280781 175310 175310 280781 175310 280781 280781 280781

A counter cannot decrease. `PROMETHEUS_MULTIPROC_DIR` fixes it and was already set in the Helm
chart — but not in `docker-compose.yml`, so the deployment everyone runs first was the broken
one. Five of the eight rules in `observability/alerts.yml` read these counters.
`app/metrics_registry.py` predicted it in its own docstring. **Every Prometheus-derived number
from before 2026-09-23 is void, including the soak's.**

### 2. The alert that watches the alerting system could never fire

`RuleMindPagingIsFailing` reads `alertmanager_notifications_failed_total`, and
`observability/prometheus.yml` had **no scrape job for Alertmanager**. The `alerting:` block
names `alertmanager:9093`, which looks like the two are wired together; it only says where to
*send* alerts. The metric had no series, so the rule evaluated empty forever and rendered green —
while this stack logged **440 real delivery failures** underneath it (a Colima DNS outage;
alert-relay could not resolve ntfy.sh, Alertmanager retried 16× per cycle and gave up, every five
minutes). `scripts/check_alert_metrics.py` is now a gate: every rule must query a metric
something can produce.

### 3. The ML feature was dead in every Linux deployment

`MODEL_MEMORY_MB` defaulted to 512. Bisected inside the shipped image: 512 MB fails,
**768 MB is the floor** for importing numpy + scipy + scikit-learn in a fresh worker. Model
upload returned `422 Invalid model: failed to map segment from shared object`, and every `model`
policy step failed. Verified live — 0 models could be uploaded before the fix, 10 after.

Why ~5,000 tests could not see it: they all run on darwin, where `setrlimit(RLIMIT_AS)` is
**refused outright**, `_model_worker_init` swallowed the `OSError` with a bare `pass`, and the
cap was therefore never applied. *The mechanism under test does nothing on the only platform
that tests it.*

### 4. An unrecognised RBAC role granted OWNER

`rbac.py` answered two questions with one default. Absent role → `owner` is correct (bootstrap
keys). **Present but unrecognised → `owner` as well**, so a typo, a NULL column, or a role
renamed in a future release granted full ownership. `test_rbac.py` contained a test named
`test_unknown_role_defaults_to_owner` that pinned the fail-open as intended behaviour.

### 5. Retention was advertised and never enforced

`DECISION_ARCHIVE_SINK` gates the whole retention job, not just archiving, and defaults to
`none` — so nothing was ever purged, while the settings screen showed *"Audit Retention Days:
90"*. Measured here: **13.2 KB per decision row** (trace 6.1 KB, rule_results 2.5 KB,
computed_variables 1.6 KB, scorecard_result 1.1 KB), 4,239 MB of 4,865 MB in TOAST. That is
~13 GB/day at 1M decisions/day, unbounded.

The disk consequence was imminent, not theoretical: the Colima VM was at 83% with 4.8 GB free
and the soak adds ~1.6 GB/day, so the "30-day" soak would have filled the disk and taken
Postgres down around **day three** — the same silent death the previous one had. The compliance
consequence is worse: a workspace that set a window believes data older than it is gone.

### 6. Seven endpoints had no test, and the tripwire was structurally blind to them

`test_tenant_isolation_matrix` counted a mutating route as covered when it matched a probed
*prefix*, so every verb hanging off `/api/v1/policies/{id}` rode in free. Narrowing coverage to
CRUD shapes surfaced **27 sub-routes** at once. Writing the tests found `GET`/`DELETE .../shadow`
answering 200 for another tenant's policy.

### And the one that had to be watched rather than tested

The dead man's switch **fired**, at 04:06:37Z on 2026-09-23: alertmanager stopped at 03:54:34Z,
priority-5 `[ALERTING IS DOWN] no heartbeat` after 16 minutes of silence, `[RECOVERED]` at
04:08:41Z once it came back. The first attempt at this test proved nothing, because the soak
supervisor noticed the stack was incomplete 100 seconds in and repaired it — correctly. *You
cannot test a dead man's switch while something is resurrecting what you killed.*

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

#### The Actions tab is still full of red, and almost none of it is a test failure

Worth writing down, because the tab shows history and history here is mostly an old billing
problem. Of **127 failed runs**, exactly **8 executed a single step**. The other 119 were
assigned no runner at all and ran `steps: 0` — GitHub's own annotation on each reads *"The job
was not started because recent account payments have failed or your spending limit needs to be
increased."* **78 of those belong to `ci`, `sdk-verify` and `test-report`, workflows that no
longer exist**; GitHub keeps a deleted workflow's runs in the tab forever.

The 8 that actually ran, all of them since fixed:

| Run | Workflow | What failed |
|---|---|---|
| 35046445624 | self-hosted tests | `.venv-rulemind` missing — the precondition doing its job |
| 35046620568 | self-hosted tests | bare `python3` was Homebrew 3.14; no psycopg2-binary wheel |
| 35046718374 | self-hosted tests | lanes failed |
| 35053890248 | backup-verify | restore failed — the Postgres lane had leaked 453 schemas |
| 35055005659 | security | `pnpm audit` gating on advisories now closed |
| 35133068883 | backup-verify | same schema leak, before the fix landed at both ends |
| 35243010034 | self-hosted tests | `python-pg` and `flutter` lanes |
| 35374621279 | self-hosted tests | **self-inflicted** — a `pkill -f scripts/run_all_tests.sh` on the build machine killed CI's own run (exit 143). The runner executes the same script on the same host; kill by PID, never by pattern |

Plus **4 cancelled** runs on one commit, also self-inflicted: a retry loop dispatched duplicates
after misreading a queued run as failed.

Current state is the thing to judge: **all four surviving workflows are green on `main`**, and
`load-test`'s green means something as of 2026-09-19 (see Capacity) where before it did not.

~~The 119 dead records can be removed with `gh api -X DELETE ...`~~ **DONE 2026-09-19 — 212
zero-step runs deleted** (the count had grown past 119 by the time it was actioned).
`PLAN.md` R8 records the sweep.

### 2. Zero production hours — the monitoring chain is now proven; real traffic is not

Nobody has run this against real users. That part cannot be closed from here.

#### How to actually close it

This is the one item on the list with no engineering answer, so here is the operational one. It
is four steps and roughly a month, and **the order matters** — each step makes the next one's
numbers mean something.

> **STARTED 2026-09-20, on this machine.** `scripts/soak_start.sh` runs the stack under the
> observability profile with shaped traffic and holds the machine awake; `--status` reports
> sampled minutes, which is the number that decides whether the month will be usable.
>
> Setting it up found two defects that would have quietly wasted the whole exercise: Prometheus
> was writing to an **anonymous** volume (a month of history lost on the next image rebuild, and
> you would only find out at the end), and retention was **15 days against a 30-day calibration
> window** — which would have halved the window and still produced a number. A third: twelve
> services had no restart policy, so a container dying at 3am stayed dead while the month carried
> on measuring a degraded stack.
>
> **A laptop is not a VPS** and the script says so: it sleeps, it is one failure domain, and its
> resources are shared. What it does give is *duration*, which is where the remaining value is.
> Steps 3 and 4 below are unchanged; step 1 is satisfied for free, imperfectly.

**1. Put it somewhere that stays up (week 1).** A single small VPS is enough: 2 vCPU / 4 GB runs
the compose stack comfortably at the measured load. Hetzner CX22 is ~€4/mo, Fly.io and Railway
have similar tiers. It does not need to be big, it needs to be *continuous* — the failures this
list is waiting on (memory creep, connection-pool exhaustion, log volume, cert expiry, disk
filling) are all functions of uptime, and none of them appear in a 30-second load test.
`make up` plus a reverse proxy is the whole deployment; `scripts/smoke_compose.sh` tells you it
worked.

**2. Point the alerting at it and leave it alone (week 1–2).** Both destinations are already
wired: `observability/secrets/topic` for the pager, `watchdog_url` for the dead man's switch.
The thing to resist is tuning during this window. Every alert that fires is data about your real
baseline, including the ones you think are wrong — especially those, because a threshold you
silence in week 1 is a threshold you never learn was miscalibrated.

**3. Drive it with traffic shaped like yours (week 2–4).** Synthetic load at a fixed rate finds
saturation, which this repo has already measured. What it does not find is what *real* shape
does: diurnal peaks, retry storms, one tenant sending 50× the others, payloads with fields
nobody anticipated. Two options, in order of value:

   - **Shadow a real integration.** Mirror production traffic from whatever system this is
     replacing and discard the decisions. You get real payloads and real distribution with zero
     blast radius, and you can compare outcomes against the incumbent — which is also the only
     honest way to validate the policies themselves.
   - **Replay, if there is nothing to shadow.** `qa/e2e-full/` and the seeded samples give you a
     corpus; vary the arrival rate on a daily sine and inject a 10× burst once a day.

**4. Then recalibrate, from the data (end of month).** This is now one command:

```bash
scripts/calibrate_thresholds.py --window 30d          # see what your traffic says
scripts/calibrate_thresholds.py --window 30d --apply  # write it into alerts.yml
```

It refuses below 200 sampled minutes rather than producing a number from a quiet afternoon, and
it derives warning and critical from the same measurement so they cannot cross. **Review the
diff** — it proposes where your system *is*, and what you consider unacceptable is still a
product decision it should not make for you.

#### The honest floor

You cannot shortcut this by running the load test for longer. Sustained synthetic load found
three real defects on 2026-09-15 (below) and would find none of the four in step 1, because
those are failures of *duration*, not of *rate*. A month of continuous operation under
realistic traffic is the shortest path, and roughly €4.

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

### ~~4. Paging — wired end to end; only the destination URL is missing~~ — CLOSED 2026-09-23

The destination is configured (Grafana OnCall free tier, escalation chain `rulemind-heartbeat` →
`tanmay_d21@iift.edu`), and — more to the point — **the switch has been watched firing**:

| | |
|---|---|
| 03:54:34Z | alertmanager stopped; heartbeats stop at source |
| 04:06:37Z | `[ALERTING IS DOWN] no heartbeat` — **priority 5**, on the on-call topic |
| 04:08:41Z | `[RECOVERED] heartbeat is back` after alertmanager restarted |

The original text below is kept because its reasoning is still right: a green tick on "the
endpoint accepted a heartbeat" only proves it RECEIVES. Whether it ALERTS on silence is a
different property and the only one that matters, and nobody had ever seen it happen.

Two things the test itself taught, both worth keeping:

* **The first attempt proved nothing.** The soak supervisor noticed the stack was incomplete 100
  seconds after alertmanager stopped and repaired it, so heartbeats resumed well inside the
  16-minute grace. You cannot test a dead man's switch while something is resurrecting what you
  killed; the supervisor has to be paused for the duration.
* **The monitor pages to ntfy and logs `PAGED:` locally** — the harness first grepped for the
  wrong word and reported a silent switch that had in fact fired.



**Alertmanager is deployed and receiving** (compose `observability` profile), with two inhibit
rules so one outage pages once rather than five times. Proven under load: an alert fired and
arrived carrying its severity, `source` label and runbook link.

`scripts/k8s_alerts.sh` ships the same rules to a cluster from the **same file** — no second
copy to drift, which matters because a stale duplicate fails as *"the alert that would have
caught it was the old one"*.

**The last hop is now closed** (2026-09-18). It used to route to an explicit black hole, with the
instruction "uncomment a receiver and paste your Slack webhook in" — which asks you to put a live
credential in a committed file, so the only safe way to follow it was not to.

A real `paging` receiver is enabled by default and reads its destination from
`observability/secrets/paging_url`, gitignored, via Alertmanager's `url_file`. Both halves were
verified rather than reasoned about:

- with a URL in place, a hand-fired critical alert **arrived** at the destination carrying its
  alertname, severity, summary and runbook link;
- with the file **absent**, Alertmanager still started (`ready=200`) and the first alert failed
  loudly — `read url_file: ... no such file or directory`. `url_file` is read at notify time, so
  a fresh clone is not broken by a missing secret and the failure surfaces when it matters.

It had to be a file, not an environment variable: Alertmanager does not expand env vars in its
config and rejects `${SOMETHING}` as `unsupported scheme ""`.

**What remains is one line of someone's credential** — a Slack/PagerDuty/Opsgenie URL in that
file. Nothing else to edit, and the "change it in BOTH places" trap is gone: the top-level route
and the critical sub-route now name the same receiver.

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

### ~~7. Untested surface — the Android SDK module~~ — CLOSED 2026-09-19

`rulemind-android` had **no test source set at all**: 1,644 lines of crypto, networking and
persistence, none of it exercised. It now has **12 test classes over 12 source files — every
file in `src/main`** — and the Kotlin lane went **46 → 140**.

The one that mattered most was `CryptoManager`. It holds the device's RSA private key, whose
public half the server already has, so a regenerated pair is not a cache miss: it is a device
that can no longer decrypt any bundle addressed to it, and the symptom appears later and
somewhere else. The class carried a comment saying exactly that, and nothing checked it.

Two things the tests turned up that reading the code did not:

* **`verifySignature` fails OPEN.** With no `serverPublicKeyPem` configured it returns `true`
  for every signature. That is a deployment consideration rather than a defect, and it is now
  pinned by a test so it cannot change silently in either direction.
* **A mutation survived, and the surviving test was the finding.** `ExistingPeriodicWorkPolicy`
  `UPDATE → KEEP` passed a test named *"scheduling twice replaces rather than stacks"*, because
  the unique-work name leaves exactly one request enqueued either way — a count cannot tell them
  apart. Under `KEEP` a changed `bundleSyncIntervalMinutes` is silently discarded while every
  outward sign still looks right. The test now asserts the interval.

**Still needs a device**, and the files say so rather than implying coverage they do not have:
that the `SecurePrefs` key is generated inside the AndroidKeyStore and is non-exportable, and
that `LegacySecureStoreMigration` actually COPIES a populated legacy store. Robolectric answers
`NoSuchAlgorithmException` for that provider, so neither is demonstrable on the JVM. The failure
path of the migration — which its own comment calls the common production case, a master key
dropped when the user changes their screen lock — **is** covered.

Issue **#133** describes emulator jobs in `sdk-verify.yml`, a workflow that no longer exists.
Its premise is gone; the two device-only properties above are what remains of its substance.

**Both were closed on real hardware on 2026-09-19** (#176). 14 instrumentation tests, run on a
Nothing Phone (1) (Android 16, `securityLevel=1` — TRUSTED_ENVIRONMENT), a Galaxy S23 and an
emulator: 42 runs, 0 failures. `getEncoded()` returning null **is** the non-exportability proof —
for an ordinary `SecretKeySpec` it hands back the 32 raw bytes; for a keystore key the platform
has nothing to give. The legacy migration's happy path is covered too: a populated
`EncryptedSharedPreferences` is copied, the old store is not deleted, and `CryptoManager` presents
the SAME public key afterwards.

Mutation-checked: swapping `keystoreKey()` for a plain in-process `SecretKeySpec` leaves every
functional test passing and fails 4 of these. That asymmetry is the argument for running them on
a device at all. The gate gains an opt-in `device` lane — opt-in because a gate that requires
hardware is a gate that gets disabled.

### 8. Still needs the account owner

- ~~**Render services are STILL LIVE and may still be billing.**~~ **CLOSED 2026-09-19 — the
  owner deleted the account.** Both hostnames now answer `404` with `x-render-routing:
  no-server`, which is Render's own header for "no service is bound here". That is the
  distinction that matters: a *sleeping* free-tier service answers 200 after a cold start (52 s,
  measured earlier the same day) and a *stopped* one answers 5xx — only a deleted one produces
  `no-server`. Checked that way deliberately, because this document already records calling
  these services dead once on the strength of a single timeout.
  `scripts/render_teardown.sh` does it in one command once `RENDER_API_KEY` is exported. No
  credential exists on the build machine, and permanently deleting a hosted database is not
  something to automate on someone's behalf.

  Worth recording: the first probe of that re-check returned `000`, and a hasty reading would
  have reported the service dead. It is not. This document already records making exactly that
  mistake once.

- ~~**AI generation has never run against a real provider (#123).**~~ **CLOSED 2026-09-19.**
  10/10 live tests pass against a real OpenAI key. The two that carry the weight: a generated
  rule is SAVED and comes back compiled to `IF (Bureau Score >= 600) → APPROVE ELSE → REJECT`,
  and the usage counters move against real tokens. Running it found the suite's own assertions
  were written against a shape the API never returns, so it would have failed on its own
  invention the first time anyone supplied a key.

  Also found: `GET /ai/models` defaulted to the literal `"anthropic"` and ignored the
  workspace's configured provider, so an OpenAI workspace got five stale Anthropic model names
  and `live: false` with no error — a well-formed list of real models, just the wrong
  provider's, which is why nothing surfaced it. 5 curated → 82 live.

- ~~**Paging has no destination**~~ — **A REAL PAGER, 2026-09-19.** An alert fired into
  Alertmanager now arrives as a push notification on a phone: titled, prioritised, carrying its
  runbook. Proven end to end by reading the notification's own fields back off the topic, not by
  reading config.

  `observability/alert-relay` sits between Alertmanager and the push service, because
  Alertmanager's webhook body is a JSON envelope — pointed straight at a phone it renders as a
  wall of JSON, which tells you nothing in the ten seconds that matter. The relay maps it onto
  `title` / `priority` / `tags` / `click`: priority 5 for `critical` so it cuts through silent
  mode, priority 1 for the `severity: none` heartbeat so it never buzzes.

  **ntfy, because it needs no account** — the topic name is the secret, the same model the rest
  of `observability/secrets` uses. That is the right trade to get paging working and the wrong
  one for production, since alert summaries leave your perimeter. `RULEMIND_NTFY_BASE` points at
  a self-hosted ntfy (one container, open source) and nothing else changes.

  **The dead man's switch now fires, and has been seen to.** `watchdog-monitor` polls the
  heartbeat's own topic for silence and pages on-call when it stops. Verified: stopping
  Alertmanager produced `[ALERTING IS DOWN] no heartbeat` at priority 5; restoring it produced
  `[RECOVERED] heartbeat is back`.

  It covers five of the six ways alerting dies — a dead relay, a broken Alertmanager reload, a
  misrouted receiver, Prometheus stopping, a container not returning. The sixth, *this host going
  away*, it structurally cannot. `observability/secrets/watchdog_url` covers that one and is
  still unset; the monitor pings it **only while it can see heartbeats**, so the external
  service's silence means "the local chain stopped proving itself", which includes the monitor
  and the machine. One URL in one file — `observability/secrets/README.md` has the Grafana OnCall
  click-path.

- ~~**Nothing watches the watchers**~~ — **PROVEN END TO END 2026-09-19**, against a live stack
  rather than by reading the config. `scripts/verify_paging.sh` walks all eight links green, and
  `--send` delivered a real alert:

  | Destination | Received |
  |---|---|
  | on-call (`paging`) | `RuleMindPagingTest`, severity `critical`, carrying its runbook link |
  | dead-man's-switch (`watchdog`) | `Watchdog`, `status=firing` |

  **The routing separation is the part worth checking, and it holds**: the paging destination
  received *only* the real alert and the heartbeat destination *only* the heartbeat. A heartbeat
  that reached the on-call channel would be paging a human every interval to say nothing is
  wrong, which is how a heartbeat gets muted and stops being one.

  Verified with two throwaway local sinks standing in for Slack/PagerDuty. What remains is
  pointing the two files at destinations you own — the chain above them is now measured, not
  assumed.

- **Nothing watches the watchers — now something does.** Every rule in `alerts.yml` assumed the
  paging pipe works, and nothing checked it. That failure is silent in the worst way: if paging
  breaks, the symptom is that you stop being woken up, which is indistinguishable from
  everything being fine. There is now a `Watchdog` dead man's switch (`expr: vector(1)`, always
  firing) routed to its OWN destination on a 5-minute repeat — a heartbeat in the on-call
  channel gets muted and stops being a heartbeat — plus an alert on
  `alertmanager_notifications_failed_total`, which catches a revoked webhook or a destination
  returning 5xx. `scripts/verify_paging.sh` walks the whole chain link by link and `--send`
  injects a real alert to prove delivery.

  **You still have to configure the other half**: a heartbeat nobody watches is decoration. Point
  `observability/secrets/watchdog_url` at a dead-man's-switch monitor (Grafana OnCall heartbeat,
  PagerDuty, Opsgenie Heartbeats, healthchecks.io — all have free tiers) set to alert if it has
  not heard from you in 10–15 minutes.

- **Alertmanager's last hop — the receiver is now real; only the destination is missing.** Both
  routes name a `paging` receiver that reads its URL from `url_file`
  (`/etc/alertmanager/secrets/paging_url`, gitignored, mounted read-only by compose), so the
  credential never enters git and nothing has to be uncommented. `url_file` is read at **notify**
  time, not load time — verified, not assumed, and it needs Alertmanager ≥ 0.26 (this pins
  0.27.0). With no file present Alertmanager starts cleanly and fails **loudly** on the first
  alert: `notify retry canceled ... read url_file: no such file or directory`. Write one file
  with a real webhook URL and paging works; the chain either delivers or complains, and no
  longer has a state where it looks configured and silently drops.

- **Branch protection: a local guard is installed; server-side is deferred to next month.**
  `.githooks/pre-push` refuses a direct push to `main`, installed via
  `bash scripts/install_hooks.sh` (which sets `core.hooksPath`, so the hook is tracked and
  cloned rather than living in one person's `.git/hooks`).

  **It is a guardrail, not a gate**, and it says so in its own refusal message: a hook runs on
  the machine doing the pushing, `--no-verify` walks past it, and a fresh clone has none until
  the installer runs. It reads refs from stdin rather than inspecting HEAD, which is what makes
  it catch `git push origin HEAD:main` — the form someone actually pushes to main by accident.
  All four paths verified: direct refused, `HEAD:main` refused, feature branch allowed,
  `--no-verify` bypassed.

  Server-side protection is still unavailable, by both routes. Re-checked 2026-09-19 rather than
  carried forward, and the newer API is blocked too:

  | Attempt | Result |
  |---|---|
  | `PUT /branches/main/protection` (classic) | 403 *"Upgrade to GitHub Pro or make this repository public"* |
  | `POST /rulesets` (the modern replacement) | 403, same message |

  So nothing currently prevents a direct push to `main`, and no free configuration changes that.
  Making the repo public would — `docs/PRE-PUBLICATION-AUDIT.md` establishes it would be safe —
  but that remains the owner's call.

- **Vercel GitHub App** — uninstalled by the owner on 2026-09-16. Deployments and Environments
  have stayed at **0** across every push since, which is the evidence it held.

## What would change the verdict

In order of value. The first item on this list used to be "get CI running"; that is done, which
is why everything below it is now the operational half.

1. **Deploy to a staging environment and leave it running** under synthetic traffic for a week,
   with the alerts wired to something that actually notifies. Every finding in the Closed section
   above needed a running system to surface — none was visible to a green suite.
2. **Put a webhook URL in `observability/secrets/paging_url`.** The chain above it is wired and
   proven; this is the only step left before "we have alerts" and "we get alerted" are the same
   claim. It is one line and it never enters git.
3. **Recalibrate the thresholds from that traffic.** They are currently derived from measurements
   on one laptop, which is honest but is still a guess about someone else's hardware.
4. **Run one real integration** end to end — a genuine bureau connector, not a sample payload.
5. ~~**Point `observability/secrets/watchdog_url` at a dead-man's-switch monitor.**~~
   **CLOSED 2026-09-23** — wired to a Grafana OnCall alertmanager heartbeat (free tier),
   escalation chain `rulemind-heartbeat` → `tanmay_d21@iift.edu`. The endpoint returns 200 and
   `scripts/verify_paging.sh` asserts it. See *The silence test* below for whether it FIRES,
   which is the only property that matters.
6. ~~**Delete the Render services.**~~ **CLOSED 2026-09-19 — the owner deleted the account.**
   This line contradicted the struck-through entry 140 lines above it in this same document,
   which is how a resolved item keeps costing attention.

*(#123 and #176 closed 2026-09-19 — see above.)*
