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

## A flake that was the environment, not the code

Worth recording next to the benchmark story, because it is the same mistake in a different
place: a measurement degraded by machine state, diagnosed as a code problem.

`sqlite3.OperationalError: unable to open database file` started hitting a RANDOM test in an
unrelated module — never the test that caused it. Three consecutive runs failed on three
different tests, each of which passed cleanly alone. I capped `db._engine_cache` at 64 to fix it.
Measured across full runs on the same tree:

| | errors per run |
|---|---:|
| no cap (before) | ~1, intermittent |
| cap, FIFO eviction | 3 |
| cap, LRU eviction | 4 |
| cap removed again | 8 |
| **stale temp dirs cleared, no cap** | **1** |

The cap was never the variable. The actual cause is **leftover temp directories accumulating
across runs** — 73 of them by that point — until SQLite cannot open new files. Clearing them took
the same tree from 8 errors to 1.

And they were not leaked by the fixtures: only 10 of 112 temp-dir fixtures pass
`ignore_cleanup_errors=True`, and all 10 dispose their engine first. They came from runs I
**interrupted** — killed suites, timed-out jobs — where no teardown runs at all. So the fix
belongs in `scripts/run_all_tests.sh`, which now sweeps stale ones before starting, and not in
112 test modules.

The cap also introduced a bug of its own: FIFO evicts the oldest engine, which is
`app_main.storage`'s — created at import, used by most of the suite — so past 64 entries it
disposed the shared engine out from under live tests. LRU fixed that specific case and still did
not fix the flake. The general lesson is in `dispose_engine`'s docstring now: **the cache cannot
know what is still referenced, so every eviction policy is a guess.**

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
| SQL queries per warm decision | **6** (1 INSERT decisions, 1 UPDATE, 4 SELECT) |
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

**Partially fixed 2026-09-14, and still not safe.** `_needed_variable_ids` now walks connector
steps too — via `_collect_connector_sources`, so nested ones inside a branch or loop are found,
which a first attempt at a flat top-level loop missed. That took the failures from **16 to 7**.

The seven that remain are the same bug in other step types. `_needed_variable_ids` walks rule
conditions, scorecard bins and now connector sources; it still cannot see a variable referenced
by a **decision table** input (`inputs[].variable_id`) or reached inside a **loop** step's nested
`config.steps`. The surviving failures name exactly those:

```
test_decision_tables.test_decision_table_runs_inside_a_policy
test_fastpath_dt_parallel.test_fast_and_heavy_paths_agree
test_workflow_loops.test_loop_inside_policy
test_fast_path_sample_seeding_parity  (x2)
...
```

So the flag stays **off**, and this is deliberately not finished in a hurry. Completing it means
a reachability analysis over every step type that can reach a variable, each with its own
lookup — and a *partially* correct analysis is worse than none, because it makes the flag look
safer than it is while still silently skipping a variable the decision depends on. That is the
FAST_DECIDE failure mode exactly: this repo has already fixed three bugs where a performance
flag changed correctness, and the way to avoid a fourth is not to ship a reachability walk that
covers most step types.

What is safe to conclude: the mechanism is right, the win is ~48.7%, the connector half is done
and tested, and the remaining work is bounded and named.

There is a second, smaller tradeoff even once that is fixed: the decision record shrinks from 55
computed variables to 6. Nothing about the *decision* changes, but anything reading
`computed_variables` for a variable the policy did not use will find it missing. That is a
product decision about the audit surface, not a performance one.
