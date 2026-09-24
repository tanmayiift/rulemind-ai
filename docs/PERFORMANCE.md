# Performance and footprint

Measured, not asserted. Every number here came from a command in this repo, on one machine, and
each section says which command. Where a change is claimed, both sides were measured on the same
hardware in the same session — a before-number quoted from an older run is not a comparison.

Last updated 2026-09-14.

## Image footprint

| Image | Before | After | Change |
|---|---:|---:|---:|
| `rulemind-ai-web` | **2.54 GB** | **232 MB** | **-91%** (10.9x) |
| `rulemind-ai-api` | 923 MB | 923 MB | unchanged so far |
| `rulemind-ai-worker` | 923 MB | 923 MB | same image as api |

```bash
docker images --format '{{.Repository}}:{{.Tag}}\t{{.Size}}' | grep rulemind
```

### What the web image was carrying

`apps/web/Dockerfile` was a single stage. It did `COPY . .` — the entire repository — on top of a
full workspace `pnpm install` that includes devDependencies (`next` and `typescript` are dev
deps, so they cannot be skipped at build time), and then kept every bit of it in the shipped
layer alongside the build output. The running server uses a small fraction of that.

The fix is three stages where only the third ships, enabled by `output: "standalone"` in
`next.config.js` — Next traces the files actually reachable from the server and writes them to
`.next/standalone`. Without that trace the runner stage has no way to know which of the
workspace's packages are needed at request time, so the split is not possible.

Two details that are easy to get wrong and both fail *silently at runtime* rather than at build:

- **`outputFileTracingRoot` must point at the repo root.** pnpm symlinks into a store above
  `apps/web`, so a trace rooted at the package omits every workspace dependency and the
  container dies on `MODULE_NOT_FOUND` for `@rulemind/shared`.
- **`.next/static` is not traced into standalone.** Next expects it served from disk beside the
  server, so it is copied explicitly. Miss it and the app boots, answers 200, and serves a page
  with no CSS and no JS — which reads as an application bug, not a packaging one.

Verified beyond the size number: the container serves `/` and a deep link at 200, runs as uid
10001, and the CSS and JS the HTML references both resolve 200 (referenced is not the same as
served). `scripts/smoke_compose.sh` is 14/14 with the new image.

## A flake that was shared state, not the code under test

Worth recording beside the benchmark story, because it is the same mistake twice: a failure
blamed on the thing in front of me rather than on what was shared.

`sqlite3.OperationalError: unable to open database file` hit a RANDOM test in an unrelated
module — never the test that caused it, and always a module that passed cleanly alone.

**Root cause:** 51 of 65 test modules did `app_main.storage = Storage(path=<tempdir>)` in setUp
and never restored it. When their tempdir was cleaned, the global still pointed at a deleted
directory, so the *next* module to read `app_main.storage` failed. Every symptom follows from
that: the victim is whoever reads the global next, and which test that is depends on discovery
order.

Fixed in 38 modules (capture in setUp, restore + dispose at the end of tearDown). Three
consecutive full runs went from 1–8 errors to **zero**.

**Two attempts before that were wrong, and are reverted:**

| Attempt | Result |
|---|---|
| Cap `db._engine_cache` at 64 | **worse** — no cap ~1 error, FIFO 3, LRU 4, removed 8 |
| Sweep stale temp dirs | helped (8 → 1) but treated a symptom |

The cap also broke something on its own: FIFO evicts the oldest engine, which is
`app_main.storage`'s — created at import and used by most of the suite — so it disposed the
shared engine out from under live tests, producing the exact error it was meant to prevent. And
I reported it as fixed on a *single* clean sample, which is not evidence for an intermittent
failure.

The lesson, now also in `dispose_engine`'s docstring: **a test that only fails in company is a
shared resource running out. Find what is shared, not what the failing test does.**

### The same shape again, in Postgres

That lesson paid off within the hour. The Postgres lane then died with
`psycopg2.errors.DiskFull: could not create file`, cascading into **430 errors** across modules
with nothing to do with each other.

`RULEMIND_TEST_PG_SCHEMA_PER_PATH=1` gives each test path its own Postgres SCHEMA — the
equivalent of its own SQLite file — and **nothing ever drops them**. After a day of repeated
runs: **1,569 stale schemas, 3.5 GB**, until the volume filled.

Identical shape to the temp dirs: a per-test resource that is created and never reclaimed, whose
exhaustion surfaces far from the test that created it. `scripts/run_all_tests.sh` now drops
stale `rmtest_*` schemas before the lane starts — safe because the name is ours and
deterministic (`db.test_schema_for` → `rmtest_<sha>`), and the lane recreates what it needs.

Result: 1920 tests, **OK**, zero DiskFull.

## What the P09 outbox costs

Every decision now writes an outbox row inside its own transaction. That is not free, so it is
measured rather than waved at:

| | p50 |
|---|---:|
| `add_decision` without the outbox | 0.445 ms |
| `add_decision` with the outbox | 0.543 ms |
| **cost** | **+0.098 ms (+22% of `add_decision`)** |

+22% sounds alarming and is the wrong denominator. `add_decision` is 0.445 ms of a ~29 ms
decision, so the outbox adds **+0.3% end to end** — the price of never losing a decision event,
paid once per decision.

### A benchmark that lied, and why it is written down

An end-to-end re-run first reported p50 rising 29 ms → 53 ms, an apparent 83% regression. It was
not real. `uptime` showed a load average of **17.45** with four processes at ~97% CPU:

- three orphaned sandbox pool workers, reparented to launchd after their test runs exited, each
  spinning at ~97%
- **a uvicorn started on 2026-08-29 — from a previous session, still listening on :8091 — whose
  sandbox worker had been burning a full core for over two weeks**

Both are now dealt with. The stray uvicorn was killed, and a sweep found **159 orphaned sandbox
workers in total**, dating back to 2026-08-26 — every one reparented to launchd. `sandbox.py`
gained a `shutdown_pool()` wired into the FastAPI shutdown event, so a graceful stop reaps its
child *processes* the way `decision_log` already reaped its threads.

To be precise about what that does and does not fix: it closes the clean-exit path. It cannot
close SIGKILL, where no handler runs at all — for that the only real answer is a PID namespace,
which is to say a container, which is what production already is. This was a developer-host leak
that ~1,900 tests and a containerised CI structurally could not see.

One INSERT cannot cost 24 ms, which is what made the number suspect enough to check. The rule
this file opens with exists for exactly this: a latency number taken on a loaded machine measures
the machine. The measurement above avoids the problem entirely by timing the thing that changed,
in-process, alternating between arms so drift hits both equally.

## Runtime memory

`docker stats --no-stream`, compose stack idle after a benchmark run:

| Service | Resident |
|---|---:|
| api (2 uvicorn workers) | 388 MiB |
| postgres | 490 MiB |
| kafka (KRaft, `kafka` profile) | 564 MiB |
| redis | 3.6 MiB |

Kafka is the largest single consumer, which is why it sits behind a compose profile rather than
in the default `make up`.

### The API's ML stack is already lazy — a planned optimisation that was not needed

The plan for this pass assumed sklearn/lightgbm were imported at startup and resident per uvicorn
worker, on the strength of comments in the codebase saying so (`decision_table_import.py:10`
describes "a resident sklearn/lightgbm stack"). Measured instead of trusted:

```bash
python -X importtime -c "import app.main" 2>&1 | grep -E "sklearn|lightgbm|scipy|numpy"
```

That prints **nothing**. None of the four loads when the API is imported; they are already
deferred to the code paths that need them. So there was no win here to take, and the honest
outcome of this item is "already done", not a change.

What remains in the 923 MB API image is the *wheels on disk* — a 545 MB `pip install` layer —
not resident memory. Those wheels are needed whenever a model feature is used, so a multi-stage
split would move them rather than remove them. That is a real but much smaller lever than the
web image was, and it is not taken here.

## Decision latency

Baseline, `policy_pl_underwriting` through `POST /api/v1/decide` on the compose stack, with the
Kubernetes cluster torn down and the stray test containers stopped:

```bash
RM_BASE=http://127.0.0.1:8080 RM_POLICY=policy_pl_underwriting \
  RM_LABEL=baseline python3 qa/e2e-full/bench.py
```

| Concurrency | p50 | p95 | p99 | throughput |
|---:|---:|---:|---:|---:|
| 1 | **19.78 ms** | 21.50 ms | 34.13 ms | **49.2 rps** |
| 4 | **35.48 ms** | 45.57 ms | 83.72 ms | **103.1 rps** |
| 16 | **134.66 ms** | 178.46 ms | 223.02 ms | **117.5 rps** |

### This table replaces an earlier one that was wrong

The first version of this baseline read 29.03 / 95.77 / 213.92 ms with 32.9 / 40.8 / 72.5 rps. It
was measured on a machine carrying **159 orphaned sandbox workers**, one of which had been
spinning at 99% CPU since 2026-08-29. Clearing them and re-measuring on a quiet host:

| Concurrency | p50 then | p50 now | throughput then | now |
|---:|---:|---:|---:|---:|
| 1 | 29.03 ms | 19.78 ms | 32.9 rps | 49.2 rps |
| 4 | 95.77 ms | 35.48 ms | 40.8 rps | 103.1 rps |
| 16 | 213.92 ms | 134.66 ms | 72.5 rps | 117.5 rps |

**Nothing in the engine changed between those two runs.** This is not an optimisation and must
not be read as one — it is the difference between measuring the product and measuring a machine
with a stuck process on it. Recorded in full because the wrong table looked entirely plausible,
and the conclusion drawn from it was also wrong: it read as "latency rises linearly while
throughput barely moves, the signature of a serialization point". On a quiet host throughput
actually scales 49 → 103 → 118 rps, which is a system approaching CPU saturation, not one
serializing on a lock.

The residual flattening between concurrency 4 and 16 is real: two uvicorn workers and
`SANDBOX_POOL_SIZE` bound how many variable evaluations can run at once.

### Where the time goes

Measured, because the codebase's own comments guess at "8+N queries per decision" and the guess
is wrong:

| | measured |
|---|---|
| SQL queries per warm decision | **6** (1 INSERT decisions, 1 UPDATE, 4 SELECT) — **superseded**, see "The other 63%" below: re-measured 2026-09-24 at 10 statements + 5 commits (14 + 7 with a `review_gate`) |
| Sandbox calls per decision | **55** |
| Time inside the sandbox | 11.6 ms of 31.5 ms — **37% of wall clock** |
| Variables the policy actually reads | **6** |

The query count is not the problem; the shared catalog cache already serves connectors,
variables and settings. **The problem is that a decision computes all 55 of the tenant's
variables when the policy reads 6.**

### The flag that fixes it, and why it is off

`executor.py` already has `COMPUTE_ONLY_USED_VARS=1`, which restricts computation to the
variables the policy transitively uses. It is opt-in, and the comment says why: the surplus
variables are recorded in the decision's `computed_variables` and are "available for
audit/downstream".

Measured in-process, 40 iterations per arm, **alternating** so machine drift hits both equally
(the first attempt at this via HTTP gave contradictory numbers across concurrency levels — that
was noise, and is why the A/B moved in-process):

| | p50 | p95 | variables computed |
|---|---:|---:|---:|
| default (all variables) | 32.70 ms | 40.87 ms | 55 |
| `COMPUTE_ONLY_USED_VARS=1` | **16.78 ms** | **27.32 ms** | **6** |

**48.7% faster at p50, and 89% less sandbox work**, with an identical outcome on every one of
the 80 runs.

### Do not enable it yet — it is measurably faster and currently incorrect

The next step after measuring a win is checking whether it is safe, and this one is not. The
full suite with `COMPUTE_ONLY_USED_VARS=1`:

```
Ran 1895 tests ... FAILED (failures=6, errors=10, skipped=21)
```

Sixteen tests break, almost all in `test_connector_fetch`, and they fail the same way:

```
KeyError: 'p01_score'   (tests/test_connector_fetch.py:413)
```

The variable was never computed. `_needed_variable_ids` walks rule conditions and scorecard
bins, so it finds variables a policy *reads through a rule* — but a variable whose data arrives
via a **connector step** is not reachable that way, and the analysis drops it. Enabling the flag
therefore skips variables the decision genuinely depends on. That is the same class of bug this
codebase has already been bitten by three times: **a performance flag that changes correctness**
(`FAST_DECIDE` skipping input validation, then the evaluation budget, then sample seeding).

**Reachability completed 2026-09-14. One blocker remains, and it is not reachability.**

`_needed_variable_ids` is now a recursive walk over every step type that can name a variable —
rule conditions, scorecard bins, decision-table inputs, connector sources, and **nested** steps
inside loops, branch arms and sub-workflows, plus variables named in *string* expressions (a
loop's `over: "variables.applicants"`, a branch's `"app['score'] < 600"`). Failures with the flag
on went **16 → 7 → 3**.

Three shapes had to be learned the hard way, each found by a test rather than by reading:

| Assumed | Actually |
|---|---|
| top-level steps are enough | a connector inside a branch is the *common* case, not the exotic one |
| `inputs` is a map | it is a **list**; `decision_tables._input_index` normalises it, so that is reused |
| conditions are `{type: condition}` nodes | a loop's `over` and a branch's condition are plain **strings** |

**The remaining 3 failures were one cause, and it was the important one:** with the flag on the
executor correctly computed nothing for a policy that reads no variables, while `fast_decide`
still computed the whole bundle — so the fast and slow paths **disagreed**. That is precisely the
FAST_DECIDE divergence class this repo has already fixed three times (input validation, the
evaluation budget, sample seeding).

### Closed 2026-09-14 — the fast path now runs the same analysis

The walk moved out of `PolicyExecutor` into `app/variable_reachability.py` and **both paths call
the one implementation**. A second copy in `fast_decide` would have been that same bug
pre-written.

Three things had to be right, and each would have been a defect on its own:

- **The reachable set is computed at bundle build and applied per request.** The flag is read per
  request and the parity tests flip it mid-process, so baking the flag's answer into a cached
  bundle would make it sticky per cache entry — flipping the flag would appear to do nothing
  until the cache expired. The set depends on the policy, not the flag, so caching it is free.
- **`core_bundle["variables"]` stays the full catalog.** The core reads it as a lookup for trace
  labels, not a work list, and the executor's `variable_lookup` is unfiltered too. Filtering it
  would have changed fast-path traces only: divergence in the other direction.
- **One reader for the flag.** The executor used `os.getenv(...) == "1"` and `env.env_bool` also
  accepts `true/yes/on`. Two answers to "is the flag on" is its own divergence, so both paths now
  call `variable_reachability.compute_only_used_vars()`.

**A fail-open had to be closed first.** `core/engine.py` did
`dict(context.get("variables") or payload or {})` — truthiness, so an **empty** variables map
fell back to **the raw payload as the feature map**. Unreachable while the fast path always
computed the whole catalog; reachable the moment it filters, because a policy that reads no
variables produces exactly that empty map. The fix for the divergence would have introduced a
fail-open in the exact shape this codebase keeps fixing. Presence now decides, not truthiness.

**Two test fixtures were measuring the unfiltered catalog rather than the guard they name**, and
both were fixed rather than skipped — the earlier note below predicted one of them:

- The **budget** tests ran a policy that reads ONE variable and drained the budget only because
  all 55 were computed anyway. Under the flag the budget never drains: 504s become 200s and
  "did the per-variable timeout shrink" compares one element with itself. They now run a policy
  that genuinely reaches 20 variables, with a fixture guard asserting it does.
- The **sample-seeding** tests used a bare `outcome` step — zero variables — yet asserted on
  `variables["seed_score"]`. Both paths correctly compute nothing and `KeyError`. A rule that
  reads the variable now sits in front; both its branches approve, so the outcome is unchanged.

Agreement is **proven, not asserted**: `test_compute_only_used_vars_parity.py` runs both paths
over every fast-servable step type, flag on and off, asserting identical outcomes **and**
identical computed-variable keys — plus that the flag actually filters (a non-empty strict
subset on both paths, since an empty set is a subset of anything and would prove the reverse).
Mutation-checked in both directions separately: disabling the fast path's filter fails 3 named
tests, making the executor ignore the flag fails 2.

### The measured win, and whether to default it on

Re-measured 2026-09-14 on a quiet machine (0 orphaned workers), in-process, 40 iterations per
arm, **alternating** so drift hits both equally. Three repeats agreed to within 0.2 points:

| Path | flag off | flag on | |
|---|---:|---:|---:|
| `fast_decide` | 7.3 ms (55 vars) | **0.53 ms (4 vars)** | **−92.8%** |
| `PolicyExecutor` | 9.2 ms (55 vars) | **2.21 ms (4 vars)** | **−76.0%** |

Both paths compute **the same 4 variables** — which is the point, and what the previous version
of this flag got wrong.

These are in-process figures for the variable-computation phase with a warm sandbox, so they are
much smaller than the 19.78 ms end-to-end HTTP p50 above. **Do not quote them as request
latency.** They measure the thing the flag changes, which is why the A/B is done here rather
than over HTTP.

*(A benchmark gotcha worth recording: `sandbox.py` spawns workers, and `spawn` RE-IMPORTS the
main module. Without an `if __name__ == "__main__":` guard the whole benchmark runs again inside
each worker and the timings interleave two runs — the first numbers off this script were 0.45 ms
and 7.16 ms for the same arm.)*

**Verdict: safe to enable per deployment. Not safe to flip the default.** Two reasons, and
neither is about correctness any more:

1. **It changes the audit surface.** The decision record shrinks from 55 computed variables to
   4. Nothing about the *decision* changes, but anything reading `computed_variables` for a
   variable the policy did not use will find it missing. Flipping the default would silently
   change what is recorded for every existing tenant — a product decision, not a performance
   one, and not one to make in a release note.
2. **The string-expression arm of the walk is heuristic.** A loop's `over` and a branch's
   condition name variables as *text*, so they are matched by substring. That is deliberately
   over-inclusive, and a false positive only costs one sandbox call — but it is pattern
   matching, not parsing, and a variable named at runtime rather than literally would not be
   found. Fine as an opt-in a deployment measures; not something to turn on for everyone
   silently.

The earlier objection — that two budget tests went trivially true under the flag — **no longer
applies**. Those fixtures were measuring the unfiltered catalog rather than the budget, and both
were fixed rather than skipped.

**Evidence for the verdict:** the full Python suite, 1928 tests, run twice consecutively with
`COMPUTE_ONLY_USED_VARS=1` — **OK both times, zero failures, zero errors.** That had never been
true before.

## The other 63% — profiled 2026-09-24, because nobody ever had

Everything above measures the sandbox, which the repo already knew about. The rest of a
decision — middleware, the per-decision `asyncio.run()`, the SQL, serialization, encryption,
the outbox — had **never been broken down**, so every optimisation proposed for it was a guess.
This is the breakdown. `cProfile` inside the running API container, warm caches, 20 decisions,
`policy_instant_personal_loan` (the shape the README's front page describes) and
`policy_pl_underwriting` (rules + scorecard, no human step) side by side.

Percentages are of profiled total, which is inflated ~45% by the profiler itself; the ratios
hold, the absolute milliseconds do not. Warm wall-clock without the profiler is given first.

| | `instant_personal_loan` | `pl_underwriting` |
|---|---:|---:|
| **warm wall clock** | **15.5 ms** | **13.5 ms** |
| sandbox IPC wait (`_thread.lock.acquire`) | **29.8%** · 165 acquires | **35.6%** · 165 |
| **deepcopy** | **14.6%** · **7,079 calls** | **10.3%** · **3,691** |
| Postgres `execute` | 9.0% · **14 statements** | 6.8% · **10** |
| Postgres `commit` | 4.8% · 7 | 4.6% · 5 |
| `dataclasses.asdict` | 2.2% | 0% |
| JSON encode/decode | 1.2% · 438 calls | 1.1% · 428 |
| Fernet encrypt/decrypt | **0.3%** · 63 calls | 0.3% · 63 |
| accounted | 61.9% | 58.8% |

### What this changes

**1. `deepcopy` is the second-largest cost and appears in no document, comment or plan.**
7,079 calls per decision. It is not one call site — it is the ORM-to-dict conversions
(`_variable_to_dict` × 55, `_rule_to_dict` × 39, `_connector_to_dict` × 18,
`_scorecard_to_dict` × 8, each deep-copying its JSON columns), plus
`ExecutionContext.to_dict()`'s `asdict(self)`, which deep-copies the *entire* context —
payload, all 55 computed variables, rule results and trace — on any policy that persists.
That last one explains the whole 2.2% / 0% asdict split: `pl_underwriting` never pauses, so
it never persists, so it never pays it.

**2. Two suspected costs are not costs.** The plan listed "4 Fernet encrypts" and "13.2 KB of
JSON" among the unmeasured 63%. Measured: **Fernet is 0.3%** and **JSON is 1.2%**. Neither is
worth touching, and saying so is the point of profiling before optimising.

**3. The query count in this document is stale.** "6 SQL queries per warm decision", above and
in `docs/runbooks/production.md`, is now **14 statements + 7 commits** for a policy with a
`review_gate` and **10 + 5** without one. The number moved when execution persistence and the
decision outbox landed; nobody re-measured it. Counts like this are why the test totals were
moved to a generated file.

**4. The sandbox is still the headline, and it is the IPC, not the work.** 29.8–35.6% is spent
*waiting on a lock* — 165 acquires for 55 variables, three per round trip. The variable bodies
themselves measure 0.3 µs (see the prepare-cache note in `PLAN.md` R10.12). This is the case
for attacking the variable *protocol* — batching independent variables, or sending only what
the callee declares it reads — rather than making the variables faster. They are already fast.

**5. 38% is unaccounted and that is honest.** It is spread across 843 functions with no single
contributor above 1% — interpreter overhead, SQLAlchemy ORM machinery, attribute access. There
is no big win hiding in it, which is itself a finding: the remaining levers are the four above,
not a mystery.


### The variable protocol — three hypotheses, two of them measured wrong

The profile says 30–36% of a decision is spent waiting on sandbox round trips. `PLAN.md` listed
three ways to attack it, "cheapest first". Two were tried, measured, and **reverted**. Recording
that, because a negative result nobody wrote down gets re-attempted.

**Hypothesis 1 — "send only the variables the callee declares it reads" (the O(n²) pickling).**
The loop passes each variable the accumulated results map, so the Nth call re-pickles N−1
earlier results. Real, and worth almost nothing: a round trip costs **0.130 ms with an empty
map and 0.136 ms carrying 55 values** — and 0.133 ms carrying 55 × 200-character strings.
**0.006 ms of difference, ~5% of one trip.** The cost is the trip, not the payload. Trimming
the payload is not worth the divergence risk of two paths disagreeing about what a variable can
see.

**Hypothesis 2 — dispatch independent variables concurrently across the pool.** A variable can
only be reordered if it never reads the accumulated map, which is statically decidable from its
source; measured on the seeded tenant, **55 of 55 variables are independent**. None reads
another. So the sequential loop was keeping exactly one of `SANDBOX_POOL_SIZE` workers busy for
no reason.

Implemented, measured, **reverted — it was slower.** End to end: 15.5 → **18.3 ms**. Isolated,
55 calls:

| | |
|---|---:|
| 55 sequential round trips | **6.97 ms** |
| waves of 4, a thread pool per wave | 8.28 ms |
| waves of 4, one shared thread pool | **5.90 ms** |

A shared pool is **15% better, not 4×**. `ProcessPoolExecutor` serialises every submission and
every result through one work queue and one management thread, and the work per variable is
0.3 µs — so the bottleneck is the queue, not the workers, and adding concurrency in front of a
serial queue buys only overlap of the pickling. Creating a thread pool per wave costs more than
that.

**Hypothesis 3 — one round trip for the whole group, executed inside the worker.** The only
design that attacks the actual cost. By parts: one trip ~0.13 ms, plus 55 × ~3 µs of in-worker
execution with compilation already cached (see the prepare-cache note) ≈ **0.35 ms against
6.97 ms** — roughly **6.6 ms off a 15.5 ms decision, ~40%.** Estimated by parts and **not
measured end to end**, because measuring it requires building it.

Not built here, and the constraint list is the reason — every one of these is a guarantee the
current per-call design gets for free:

1. **Per-variable timeout.** Today each call has its own `future.result(timeout=…)` and a
   timeout recycles the pool so a wedged worker cannot pin a slot. One trip means one deadline
   for the group. A `while True` variable then burns the group's budget rather than its own —
   the same wall clock and the same fail-closed outcome, but the trace names a cluster instead
   of a culprit. Restoring per-variable granularity needs `signal.setitimer` inside the worker,
   which works for the pure-Python the AST allowlist permits, and is new security-boundary code.
2. **Error isolation.** One variable raising must not lose the other 54's results.
3. **Budget abandonment.** `_check_budget` between dispatches is what turns an over-budget run
   into a 504. One dispatch means one checkpoint — so the group has to be a pool-sized wave, not
   the whole catalog. (The first attempt at hypothesis 2 dispatched everything at once and the
   budget suite went from 504 to 200. The tests were right.)
4. **Fail-closed on a broken pool**, per `SANDBOX_FALLBACK_MODE`, for every item in the group.

`app/variable_independence.py` — the AST analysis that decides which variables can be reordered
— was written and tested for hypothesis 2 and removed with it rather than left as dead code.
It is small, and hypothesis 3 needs the same analysis; the tests for it are in the commit that
reverted it.

**Hypothesis 3, built (plan 4.4, 2026-09-24).** `sandbox.execute_variable_group`, called by both
decision paths through `app/variable_runner.py`. Measured on this laptop, seeded catalog, pool
of 2, 200 alternating samples per side, three rounds that agree to within 0.3 ms:

| | one trip per variable | one trip per decision | change |
|---|---:|---:|---:|
| 55 variables, p50 | 5.6 ms | 0.34 ms | **−94%** |
| `POST /api/v1/decide`, p50 | 12.6 ms | 6.8 ms | **−46%** |
| `POST /api/v1/decide`, p95 | 14.6–15.2 ms | 7.8–9.3 ms | |
| 8 concurrent clients, in-process | 88.0 decisions/s | 89.8 decisions/s | none |

The by-parts estimate (0.35 ms, ~40%) held. **Throughput did not move, and that is not a
contradiction:** at saturation this harness is bound by the API process's own CPU (~11 ms a
decision under the GIL), and the per-call trips were mostly time spent *waiting* on IPC, which
releases the GIL. The change buys latency at any load below saturation; it adds throughput only
where the pool itself was the bottleneck.

How each constraint above was met, and where the prediction was wrong:

1. **Per-variable timeout** — an interval timer in the worker raises at min(timeout, budget
   left), re-firing every 50 ms, and a result that arrives after its deadline is a timeout even if
   the code caught the timer and returned (variable code may use a bare `except`). Code that
   swallows every timer is killed at the group's own deadline and every variable in the group
   errors — which sends the decision to review, exactly as one failed variable already does.
2. **Error isolation** — per variable, including a return value that cannot be pickled.
3. **Budget abandonment** — *no waves were needed*, which the note above predicted they would
   be. The worker is given the remaining budget and stops starting variables when it is spent;
   the caller's own check then raises the 504 as before.
4. **Fail-closed on a broken pool** — the whole group follows `SANDBOX_FALLBACK_MODE`; the inline
   fallback keeps accumulation.

Also kept, and not on the list above: **isolation**. The per-call path handed every variable a
freshly unpickled payload and map, so a variable appending to a list could not change what the
next one saw. Inside one worker that is done on purpose — one pickle per source, one unpickle
per variable, 0.13 ms for 55 (deepcopy measured 0.8 ms). **No independence analysis was needed**:
the group runs in order, so accumulation is exactly what it was. `SANDBOX_BATCH=0` is the kill
switch; tests that replace `execute_variable` keep the per-call path, and 13 new tests run the
real pool.


## Where it saturates, and how it behaves past that

`qa/loadtest.py` — ramp to find the knee, then soak at it. Run against compose on this laptop
(2 uvicorn workers, Colima VM, 4 CPU), rate limiting **disabled** so the measurement is of the
engine rather than of the limiter:

| Concurrency | rps | p50 | p95 | p99 | max | 5xx | shed | hung |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 32.8 | 24.8 ms | 51.6 ms | 56.6 ms | 121 ms | 0 | 0 | 0 |
| 4 | 59.7 | 64.3 ms | 102.8 ms | 137.6 ms | 471 ms | 0 | 0 | 0 |
| 8 | 78.1 | 94.6 ms | 164.8 ms | 199.4 ms | 208 ms | 0 | 0 | 0 |
| **16** | **93.5** | 166.7 ms | 212.2 ms | 226.0 ms | 236 ms | 0 | 0 | 0 |
| 32 | 87.3 | 388.7 ms | 547.1 ms | 592.3 ms | 606 ms | 0 | 0 | 0 |
| 64 | 82.1 | 669.5 ms | 1401.0 ms | 1462.8 ms | 1553 ms | 0 | 0 | 0 |

**The knee is concurrency ~16 at ~94 rps.** Past it throughput stops rising and *slightly
falls* while latency climbs roughly linearly — the signature of saturation, not of a lock. Two
uvicorn workers and `SANDBOX_POOL_SIZE` are what bound it.

**It degrades rather than collapsing.** At 4x past the knee: 0 server errors, 0 timeouts, and a
max of 1.55 s. Slower and still correct is the behaviour you want; the alternative is a queue
that grows until something falls over.

**Soak: 40 s at the knee, 10 windows, zero errors, no drift.** p50 held between 331 and 503 ms
and throughput between 65 and 88 rps. Nothing leaked — which is the thing a burst test cannot
tell you and the reason the soak exists.

### Shedding is not failing

With rate limiting **on** (the default: 1000 rpm per tenant on the `decision` budget), sustained
overload produces exactly what it should:

```
{"error":"Rate limit exceeded",
 "detail":"Limit is 1000 requests/minute on the 'decision' budget for this tenant. Retry in 1s."}
```

A clear refusal with a retry hint, in ~44 ms, instead of a queue. **The first run of the load
script counted those 429s as errors** and reported "152 errors per window" for a system that was
behaving perfectly. The script now classifies 429 and the 504 evaluation-budget response as
*shed*, separately from real failures — conflating them makes a correctly-shedding service look
broken and a genuinely broken one look merely busy.

### One observation that did not reproduce

The first ramp recorded a **120,007 ms** max at concurrency 64 — a client-timeout-length hang.
Re-tested twice: with the limiter off, max was 1.55 s; with it on, 1280 requests at concurrency
64 all returned 200 with a max of 4.1 s. **It has not reproduced and I do not have a complete
explanation.** It occurred on the one run whose rate-limit bucket was already drained by prior
traffic, so the limiter path is the place to look if it recurs. Recorded rather than dropped,
because an unexplained 2-minute request is worth knowing about even once — and recorded as
*unexplained* rather than dressed up as diagnosed.

## The disk that matters is Docker's, not the host's

A full run filled Docker's disk and broke the kind cluster with `No space left on device` —
while the host reported **650 GB free**. Both numbers were true; only one was relevant, and the
irrelevant one was the one being checked.

This machine runs **Colima**, whose VM disk is a **fixed allocation**:

```yaml
# ~/.colima/default/colima.yaml
disk: 30          # GiB — a hard ceiling, not a share of the host's 650 GB
```

Inside the VM, `/dev/vdb1` is 29.4 G and that is the entire budget for images, containers,
volumes and build cache. Docker Desktop behaves identically for the same reason. So `df` on the
host answers a question nobody asked — and answering it confidently is worse than not checking,
because it reads as an all-clear.

**The failure is badly disguised.** The disk fills while a kind node is starting Postgres, and
what surfaces is an `initdb` crash inside the node. Nothing in that message mentions disk, so the
next hour goes into the database.

`scripts/lib/docker_disk.sh` now holds the check, sourced by `k8s_up.sh` (fails fast) and
`run_all_tests.sh` (warns, since most lanes need no container). It reads free space **from inside
the VM** when there is one, which is the only place the number is real.

`k8s_up.sh` was also the heaviest consumer in the repo, and it costs roughly **double** the image
size per run: it rebuilds both images *and* `kind load`s them into the node container, so each run
writes two copies. It now prunes dangling layers — but **only after a successful run**. After a
failure those layers are the evidence you inspect to find out why the build broke; pruning them
turns a diagnosable failure into a mystery.

```bash
docker system df                 # the daemon's own accounting — the number to trust
docker image prune -f            # dangling layers only
colima stop && colima start --disk 60   # or just give the VM more room
```

To check the disk directly:

```bash
colima ssh -- df -h /var/lib/docker
```

---

## Step 14 closed — the hotspot audit, with verdicts instead of a checkbox

Round 1 step 14 said "fix the hotspots the measurements actually name, each with a before/after
number", and listed three candidates "to be confirmed not assumed". It stayed open for two
rounds. Auditing it rather than trusting the list: **five of the six candidates were already
done, one was measured and fixed, and the sixth turned out to be a non-issue once measured.**
Recorded here so the box is closed with evidence and nobody re-opens it from the same guess.

### `fast_decide._build_tenant_secrets` — already optimised, before the baseline was taken

The N in "8+N queries" was eliminated on **2026-09-03**, which is *before* the step-12 baseline
this workstream was measuring against. `fast_decide.py:311-313` resolves secrets from the shared
catalog rather than per connector. Proven, not asserted: `tests/test_catalog_cache.py:105-111`
drives 5 requests and asserts exactly **2** `get_connector` calls, and `:113-123` asserts a
policy that reads no secrets pays **0**.

### `_SHARED_CATALOG_SOURCES` — already optimised

TTL + Redis-epoch invalidation + single-flight + a bounded cache, all covered
(`fast_decide.py:64-153, 237-291`). No miss-rate number exists. That is a gap in *evidence*, not
in behaviour, and it is cheaper to say so than to manufacture a metric for it.

### Query count — 6, not "8+N"

Measured above. The "8+N" figure was a comment's guess that outlived the code. It survived in
`docs/runbooks/production.md` until 2026-09-23; corrected there too.

### The one real hotspot — found, fixed, measured

*A decision computes all 55 of the tenant's variables when the policy reads 6.* Fixed via one
shared reachability walk (`app/variable_reachability.py`), measured at **−92.8%** on the serving
path and **−76.0%** on the full executor, mutation-checked in both directions. Deliberately left
opt-in, because it shrinks the recorded `computed_variables` from the whole catalog to what the
policy read — a change to the audit surface, which is the operator's call and not a default.

### Modelset `POST /compare` — the last unmeasured candidate. Measured; it is fine.

`routers/modelsets.py:151-163` loops members and calls `execute_model` per member, each a
`ProcessPoolExecutor` round trip, and because the loop awaits each call the pool's second worker
is never used. `docs/parity/parity.json:65` called this "acceptable but worth naming" on the
grounds that it is an authoring tool rather than a decision path. That was an opinion. Here is
the number — 10 identical scikit-learn members, same features, request-paced under the tenant
rate limit:

| members | p50 | p95 | marginal ms/member |
|---:|---:|---:|---:|
| 1 | 7.3 ms | 9.5 ms | — |
| 2 | 8.4 ms | 18.4 ms | 1.1 |
| 5 | 11.0 ms | 12.4 ms | 0.9 |
| 10 | **14.7 ms** | 19.3 ms | **0.8** |

**0.8 ms per additional member.** Ten members cost 14.7 ms, not the ~73 ms a per-member round
trip would imply — the fixed ~6.5 ms of request overhead dominates, and the worker keeps the
deserialised model, so the marginal cost is one pool hop. The serial loop is real and is not
worth parallelising: at 50 members this extrapolates to ~45 ms, for a screen an operator opens
deliberately. The parity row's assertion was right; it now cites a measurement instead.

---

## Sizing — what a decision costs on disk

Throughput has been measured for a while; **storage had not been measured at all**, and it is the
number that decides what volume a deployment can carry. Taken from the soak stack's Postgres
(385,076 real decisions, 3 days of diurnal traffic):

| | bytes | share |
|---|---:|---:|
| `trace` | 6,149 | 47% |
| `rule_results` | 2,454 | 19% |
| `computed_variables` | 1,638 | 12% |
| `scorecard_result` | 1,110 | 8% |
| `payload_preview` | 604 | 5% |
| row + index overhead | ~1,250 | 9% |
| **total per decision** | **~13,250** | |

    decisions: 385,076 rows -> 4,865 MB total
      heap  538 MB · indexes 88 MB · TOAST 4,239 MB

87% of the table is TOASTed JSON. So:

| decisions/day | growth/day | 90-day window | 1-year window |
|---:|---:|---:|---:|
| 10,000 | 130 MB | ~12 GB | ~48 GB |
| 100,000 | 1.3 GB | ~120 GB | ~480 GB |
| 1,000,000 | 13 GB | ~1.2 TB | ~4.8 TB |

**Size the volume for the retention window, and make sure the window is actually enforced** —
`DECISION_ARCHIVE_SINK` gates the purge job, and its default (`none`) means nothing is ever
deleted however the settings screen is configured. See the retention finding in
`docs/PRODUCTION-READINESS.md`.

### Two levers, if that is too much

`COMPUTE_ONLY_USED_VARS=1` removes the variables the policy never read from
`computed_variables` — 12% of the row here, and much more on a tenant with a large catalog than
on the 55-variable demo. It is off by default because it shrinks the audit surface, which is a
deliberate trade rather than a default.

`trace` is 47% on its own and has no equivalent knob. A per-policy or per-tenant "record the
trace only on non-approve outcomes" option is the obvious next win and does not exist yet; it is
named here rather than quietly implemented, because how much of a decision a regulated customer
must be able to reconstruct is their call, not ours.

### Why this was not known

The soak had been running for three days into a stated 30-day window on a Colima VM with a hard
30 GiB allocation, 83% full and 4.8 GB free, growing 1.6 GB/day. It would have filled the disk
and taken Postgres down around day six — silently, the same way the previous soak died. Nothing
measured bytes-per-decision, nothing alerted on database growth, and the retention setting that
looked like it bounded all this was inert.
