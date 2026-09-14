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
  sandbox worker has been burning a full core for over two weeks**

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
| 1 | 29.03 ms | 35.85 ms | 70.01 ms | 32.9 rps |
| 4 | 95.77 ms | 117.98 ms | 142.65 ms | 40.8 rps |
| 16 | 213.92 ms | 283.27 ms | 304.94 ms | 72.5 rps |

Latency rises almost linearly with concurrency while throughput barely moves — 32.9 to 72.5 rps
for a 16x increase in offered load. That is the signature of a serialization point, not of work.

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

So: the 48.7% is real, the mechanism is right, and the reachability analysis is incomplete. The
work is to teach `_needed_variable_ids` about connector-supplied variables and re-run the suite —
not to flip a default that fails 16 tests. Recorded here rather than quietly left in the
backlog, because a measured-but-unsafe win is easy to mistake for a free one.

There is a second, smaller tradeoff even once that is fixed: the decision record shrinks from 55
computed variables to 6. Nothing about the *decision* changes, but anything reading
`computed_variables` for a variable the policy did not use will find it missing. That is a
product decision about the audit surface, not a performance one.
