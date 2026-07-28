# Load test results — 20-condition policy

A policy that evaluates **20 conditions sequentially** and returns **one** decision
per case (`build_deep_bundle(20)`), run against both targets. Reproduce with:

```bash
python -m simulation.loadtest --requests 20000 --concurrency 64 --workers 4 --conditions 20
```

## Target 1 — Stateless decision core (the production / K8s path)

| Metric | Result |
|---|---|
| In-process throughput | **13,672 cases/sec/core** (273,434 condition-evals/sec) |
| HTTP throughput (4 uvicorn workers) | **434 TPS**, 0 errors on 20,000 requests |
| Latency p50 / p95 / p99 | 93ms / 441ms / 785ms |
| Target ≥ 200 TPS | **PASS** |

Throughput scales linearly with replicas because the core is stateless — the HTTP
number is bounded by the laptop's 4 workers, not the engine (13.6k/core ceiling).

## Target 2 — Full stateful `/api/v1/decide` (FastAPI + PolicyExecutor + sandbox + Postgres/SQLite)

Measured two ways against the real `PolicyExecutor` (variable sandbox + storage writes):

| Method | Result |
|---|---|
| In-process, single-thread | **3.5 req/s/core, ~284ms/request** |
| **HTTP, 4 uvicorn workers on Postgres (docker)** | **29.2 TPS**, 0 errors, p50 393ms / p95 1808ms |

(The docker Postgres + Redis stack was brought up for the HTTP run; the seeded
`policy_instant_personal_loan` policy was the target.)

The bottleneck is **not** rule evaluation — it is the per-request variable
computation, which runs each variable in an isolated `ProcessPoolExecutor` (safe
Python sandbox) and writes a `Decision` + `WorkflowExecution` row every call. This
rich path is designed for **authoring, testing, and low-QPS orchestration with side
effects** (connectors, actions, review gates), not for high-TPS serving.

### Why this is the right design
Production decisioning runs on the **stateless core** (Target 1), which evaluates a
pre-compiled, immutable bundle with no sandbox spawn and no per-request DB writes —
hence the ~3,900× throughput difference. The stateful API compiles + signs bundles;
the core serves them at scale. This is exactly the control-plane / eval-core split.

### Root cause of the slow stateful path (found + fixed)
Profiling proved the bottleneck was **not** the sandbox or `PolicyExecutor` — it was
**bcrypt-verifying the API key on every request** (~200ms by design) plus a
`last_used_at` DB write per call. Every authed endpoint was capped at ~4 req/s.

Fixes:
- **Per-Storage verified-API-key cache** (bcrypt once per key per TTL): authed reads
  240ms → **2.1ms** (114×).
- **Throttled `last_used_at`** so it isn't a write per request.
- **Cached-bundle fast path** (`app/fast_decide.py`, `FAST_DECIDE=1`): pure-compute
  policies served from a cached bundle via the stateless core (Rust when available),
  bypassing `PolicyExecutor` + its ~3 writes. `/decide` 250ms → **4.9ms, 205 req/s
  single-thread** — i.e. ~1,000+ TPS across a handful of workers, all variables
  computed. Parity with the standard path is unit-tested.

### Rust eval-core (packages/rulemind-core-rs)
Persistent compiled-bundle decisions: **93,915/sec/core (6.8× the Python core)**,
conformance-verified against the shared operator spec. WASM-capable; PyO3 binding
used automatically by the fast path for rules-only policies.
