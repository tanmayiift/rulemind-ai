# RuleMind.AI — Integration & Operations Guide

Operational reference for the resilience, security, and evaluation features — how they behave,
how to configure them, the edge cases they cover, and what is intentionally left to the operator.
Complements `README.md` (product/API) and `HLD.md` (architecture). Every behavior below has a
corresponding executable test (see [Testing](#testing)).

---

## 1. Cross-engine evaluation contract

Decisions must be **identical** across all five engines — Python (`app/logic.py`), Rust
(`packages/rulemind-core-rs`, native + PyO3 + WASM), TypeScript (`packages/rule-engine`),
Kotlin (`sdk-android`), Dart (`sdk-flutter`). The contract is pinned by
`packages/shared/operators.spec.json` (90 cases) and enforced by a conformance suite per engine.

Operators: `== != > >= < <= between in not_in regex exists !exists`.

### Type-coercion edge cases (locked by spec — these bit us, now regression-tested)

| Case | Result | Why |
|---|---|---|
| `"720" >= 700` | true | numeric strings coerce to numbers |
| `"Infinity" >= 700`, `NaN == x` | **false** | non-finite values are rejected (never clear a numeric gate) |
| `True == 1` (untyped) | **false** | a boolean is **not** the number 1 — bools never numerically coerce |
| `"true" == True` (untyped) | true | bools compare by lowercase string form (`"true"`/`"false"`) |
| `True >= 0` | false | a boolean is not numeric in ordered comparisons |
| `null == ""` | **false** | a null/missing value only ever equals another null |
| missing variable `< 700` | false | a missing field stays `null` (not `0`) on-device and server-side alike |
| `date` field type | epoch-normalized | ISO dates compare by UTC epoch (`+05:30` and `Z` resolve equal), integer civil-days math |
| `regex` | ReDoS-guarded | input bounded to 4 KB, interruptible `regex` engine with a 50 ms timeout, fail-closed |

> Booleans and nulls are **distinct types**, not numbers or strings. This is the one behavior
> where Python's native semantics (`True == 1`, `str(True) == "True"`) diverged from the other
> four engines; `_coerce_number`/`_loose_equal` now reject bools and lowercase them so all five agree.

---

## 2. Connector circuit breaker  (`app/circuit_breaker.py`)

Protects the decision path when an outbound connector/action (`http_request`, monitor, action step)
is failing: instead of every decision paying the full timeout + retry budget, the breaker **fails
fast**.

**State machine (per target, per replica):**
- `CLOSED` → calls flow; consecutive failures counted.
- `OPEN` → after `CB_FAILURE_THRESHOLD` consecutive failures, calls **short-circuit immediately**
  (`{"circuit_open": true, "attempts": 0}`) for `CB_RECOVERY_SECONDS`.
- `HALF_OPEN` → after the cooldown, up to `CB_HALF_OPEN_MAX` probe calls are allowed; a success
  closes the breaker, any failure re-opens it for another cooldown.

Keyed by connector id (`step.ref_id`) when known, else the URL host. `onFailure` still applies to a
short-circuited call (`continue` / `abort` / `review_gate`).

| Env var | Default | Meaning |
|---|---|---|
| `CIRCUIT_BREAKER` | `1` | `0` disables the breaker entirely |
| `CB_FAILURE_THRESHOLD` | `5` | consecutive failures before OPEN |
| `CB_RECOVERY_SECONDS` | `30` | cooldown before a HALF_OPEN probe |
| `CB_HALF_OPEN_MAX` | `1` | probe calls permitted while HALF_OPEN |

**Edge cases:** state is in-process (per replica) by design — a breaker is a local latency/health
signal, not shared truth, so there is no extra network hop on the hot path. `circuit_breaker.all_snapshots()`
exposes live state for a health endpoint.

---

## 3. Durable decision outbox / WAL  (`app/decision_wal.py`)

Closes the **hard-kill loss window**: the decision-log write runs off the request path on a
background pool, and a graceful stop (SIGTERM/rolling deploy) flushes it — but a **SIGKILL/OOM**
between a decision being returned and its DB write landing could previously drop that decision.

**Lifecycle (when enabled):** `append()` writes the decision to a per-worker append-only file and
**fsyncs it before** the async DB write is scheduled → on `POLICY` startup, `recover()` replays any
WAL entry whose id is **not already in the DB** (idempotent — no double-count), then compacts.

| Env var | Default | Meaning |
|---|---|---|
| `DECISION_WAL` | `0` | `1` enables the WAL (off by default; the async path already flushes on SIGTERM) |
| `DECISION_WAL_DIR` | `<cwd>/data/wal` | per-worker WAL files (`decision_wal_<pid>.jsonl`) |
| `ASYNC_DECISION_LOG` | `1` | `0` = fully synchronous writes (zero-loss, no WAL needed, higher latency) |
| `DECISION_LOG_MAX_QUEUE` | `1000` | outstanding async writes before `submit()` runs inline (backpressure) |
| `DECISION_LOG_WORKERS` | `4` | background writer threads |

**Edge cases handled:** a torn final line (process killed mid-write) is skipped on replay; the
decision it represented was never acked to the caller, so dropping it is correct. Per-worker files
avoid write contention; startup replay scans them all, so whichever process boots first heals the
others' orphans. Recovery closes the in-process append handle after compaction so a subsequent
append reopens a fresh file.

> **When to enable:** turn `DECISION_WAL=1` on where a hard-kill/OOM audit gap is unacceptable
> (regulated decisioning). Otherwise the SIGTERM-flush of the async path covers graceful stops, or
> `ASYNC_DECISION_LOG=0` gives synchronous zero-loss at a latency cost.

---

## 4. Redis, rate limiting & multi-replica safety  (`app/runtime.py`, `app/middleware.py`)

- **Rate limit** is per-tenant, plan-based: **5000/60 s enterprise, 1000/60 s standard**. With
  `REDIS_URL` set, the bucket (`ratelimit:<tenant>:<window>`) is **shared across all replicas** (one
  true global limit). Without Redis, each replica keeps its own in-memory bucket (≈ limit × replicas).
- **Fail-closed on Redis outage:** if Redis dies mid-request, the limiter falls back to the bounded
  in-memory limiter so limits still apply (a blip must not remove all per-tenant limits — DoS/cost
  exposure). `RATE_LIMIT_FAIL_OPEN=1` opts into the old availability-over-protection behavior.
- **SSE / live decision feed** (`app/decision_bus.py`) publishes to a **per-tenant channel**
  (`rulemind:decisions:<tenant>`). Cross-tenant isolation is structural — a subscriber only ever
  subscribes to its own tenant's channel.
- **Scheduler leader election** (`app/scheduler.py`, `storage.try_acquire_scheduler_lease`): a
  single-row atomic DB lease means exactly one replica runs scheduled jobs; leadership is released
  on graceful shutdown so a survivor takes over immediately.

| Env var | Default | Meaning |
|---|---|---|
| `REDIS_URL` | unset | enables shared rate-limit bucket + SSE pub/sub fan-out |
| `RATE_LIMIT_FAIL_OPEN` | `0` | `1` = allow all when Redis is down (not recommended) |

---

## 5. PII redaction  (`app/logic.py: redact_payload`)

Sensitive keys are masked from the **stored decision payload preview** (payloads are also
Fernet-encrypted at rest). Redaction recurses into nested objects.

**Built-in default keys** (identity-focused): `name, email, phone, telephoneNumber, idNumber, pan,
aadhaar, address, line1, line2`.

**Operator extension:** add any domain-specific keys — case-insensitive, comma-separated:

```bash
RULEMIND_PII_REDACT_KEYS="ssn,password,api_key,token,secret,card_number,patient_id"
```

> **Edge case / operator action required:** credential-like keys (`password`, `ssn`, `api_key`,
> `token`, `secret`, `card_number`) are **not** in the default set — they are masked only when you
> add them via `RULEMIND_PII_REDACT_KEYS` (or per-tenant PII keys in the UI). If your decision
> payloads can carry such fields, configure this. (Whether these should ship as defaults is a
> product decision tracked separately.)

---

## 6. Backtest — sample vs full-population  (`app/backtest.py`)

`POST /api/v1/policies/{id}/backtest` replays historical decisions through a candidate bundle and
reports the outcome impact (change rate + from→to transition matrix). Two modes:

| Mode | Request | Behavior | Use when |
|---|---|---|---|
| **sample** (default) | `?sample=200` (≤ 2000) | replays the N most-recent decisions, loaded in memory | fast, representative pre-promotion check |
| **full** | `?full=true` | streams the **entire** decision population for the policy, one keyset-paginated page at a time (memory bounded to `page_size`, default 2000) — handles millions of rows | exhaustive impact audit |

Both replay through the same deterministic `core.engine.decide`, so results are reproducible to the
exact outcome. The response carries `mode` (`sample`/`full`) and `scanned` (rows actually replayed).
Full mode uses `storage.iter_policy_decisions` (keyset cursor on `(created_at, id)`), so it stays
O(page) at any offset — no LIMIT/OFFSET degradation on large tables.

```bash
# fast sampled impact (default)
curl -X POST .../api/v1/policies/POL-500/backtest -H "x-api-key: $KEY"
# exhaustive full-population audit (streamed, memory-bounded)
curl -X POST ".../api/v1/policies/POL-500/backtest?full=true" -H "x-api-key: $KEY"
```

---

## 7. Variable sandbox  (`app/sandbox.py`)

Custom variable code runs under an **AST allowlist** (no imports, no `eval`/`exec`/`open`/`compile`,
no dunder name or attribute access) so `().__class__.__bases__`-style escapes are rejected. Define a
named function (any name except the reserved `variable`), e.g. `def score(payload, ctx): ...`.

| Env var | Default | Meaning |
|---|---|---|
| `SANDBOX_MODE` | `pool` | `inline` runs in-process (allowlist still enforced; skips ~500 ms process spawn) — use for dev/tests |
| `SANDBOX_FALLBACK_MODE` | `inline` | fallback path if the process pool can't start |

---

## 8. Full environment-variable reference

| Var | Default | Area |
|---|---|---|
| `RULEMIND_CONFIG_KEY` | `rulemind-dev-master-key` | **master key** for Fernet-encrypted config/BYOK keys — **must stay stable** or stored keys 401 |
| `RULEMIND_ADMIN_JWT_SECRET` | (dev default) | platform-admin JWT signing secret |
| `RULEMIND_SEED_DEMO` | unset | `1` seeds demo tenant/policies on boot |
| `REDIS_URL` | unset | shared rate limit + SSE fan-out |
| `RATE_LIMIT_FAIL_OPEN` | `0` | rate-limit behavior on Redis outage |
| `CIRCUIT_BREAKER` / `CB_FAILURE_THRESHOLD` / `CB_RECOVERY_SECONDS` / `CB_HALF_OPEN_MAX` | `1` / `5` / `30` / `1` | connector circuit breaker |
| `DECISION_WAL` / `DECISION_WAL_DIR` | `0` / `<cwd>/data/wal` | durable decision outbox |
| `ASYNC_DECISION_LOG` / `DECISION_LOG_MAX_QUEUE` / `DECISION_LOG_WORKERS` | `1` / `1000` / `4` | async decision logging |
| `RULEMIND_PII_REDACT_KEYS` | unset | extra PII keys to redact |
| `SANDBOX_MODE` / `SANDBOX_FALLBACK_MODE` | `pool` / `inline` | variable sandbox execution |
| `ANALYTICS_WINDOW_DAYS` | `90` | dashboard analytics window |

Production **fail-closed guard**: the API refuses to start with default/unset critical secrets when
`NODE_ENV != development` (see `verify_production_secrets`).

---

## 9. Change-management & experimentation features

Four release-safety features, all replaying through the same deterministic `core.engine.decide`.

### Shadow execution / dark launch  (`app/shadow.py`)
Run a candidate policy on live traffic without affecting the returned decision — validate a change
against production before promoting it.
```bash
# dark-launch POL-CAND behind live POL-LIVE
curl -X POST .../api/v1/policies/POL-LIVE/shadow -H "x-api-key: $KEY" -d '{"candidate_policy_id":"POL-CAND"}'
# divergence report: how much of live traffic the candidate would flip
curl .../api/v1/policies/POL-LIVE/shadow -H "x-api-key: $KEY"
curl -X DELETE .../api/v1/policies/POL-LIVE/shadow -H "x-api-key: $KEY"   # stop
```
Registered in `engine_config.shadow_map`. The shadow runs best-effort after the live decision and is
logged with `source="shadow"` — it can never break or alter the live path. (Adds one settings read
per decision while active; leave unregistered for zero overhead.)

### What-if KPI simulation  (`app/whatif.py`)
Replay decisions through a candidate bundle and compute **caller-defined KPIs** on baseline vs
candidate. `full=true` streams the whole population, memory-bounded.
```bash
curl -X POST ".../api/v1/policies/POL-500/whatif?" -H "x-api-key: $KEY" -d '{
  "full": true,
  "kpis": [
    {"name":"approval_rate","type":"outcome_rate","outcome":"approve"},
    {"name":"avg_score","type":"avg","field":"score"},
    {"name":"high_value","type":"count_where","field":"amount","op":">=","value":50000}
  ]}'
```
KPI types: `outcome_rate, outcome_count, count, count_where, avg, sum, min, max`. Specs are
declarative (no code exec) — safe to accept from the API.

### Release snapshots + one-click rollback  (`app/release.py`)
Every promotion captures a full definition snapshot (`Promotion.snapshot_json`). List the release
timeline and roll a policy (and its referenced rules/scorecards/tables) back to any prior release.
```bash
curl .../api/v1/policies/POL-R/releases -H "x-api-key: $KEY"
curl -X POST .../api/v1/policies/POL-R/rollback -H "x-api-key: $KEY" -d '{"promotion_id":42,"reason":"regression"}'
```
Rollback is recorded as a forward `rollback` promotion — history is **append-only**, never rewritten,
so you can always roll forward again.

### Collaborative editing (CRDT) + time-travel  (`app/crdt.py`)
Multi-author editing without lost updates: a per-field **Last-Writer-Wins register CRDT**. Merging
two concurrent edits is associative/commutative/idempotent, so replicas converge. Different-field
edits both survive; same-field edits resolve deterministically by `(timestamp, actor)` and are
reported as conflicts. Time-travel reads/diffs any past version from an ordered history.
```bash
curl -X POST .../api/v1/collab/merge -d '{
  "base": {"threshold": 700, "name": "Loan"},
  "edit_a": {"name": "Loan v2"}, "edit_b": {"threshold": 750},
  "actor_a": "alice", "actor_b": "bob", "ts_a": 1, "ts_b": 2 }'
# -> {"merged": {"threshold": 750, "name": "Loan v2"}, "conflicts": []}
```
**Live collaborative editor UI** (`apps/web/app/collab/page.tsx`, `/collab`): a real multi-author
editor over a WebSocket — live presence (who's editing, and which field their cursor is on),
conflict-free field edits, a version timeline, and a time-travel slider that reconstructs and can
restore any past version. Backed by:
- `WS /ws/v1/collab/{doc_id}?actor=<name>` — join → `init`, then `edit`/`presence` broadcasts; the
  client sends `edit` / `cursor` / `restore`. Server-authoritative merge via the CRDT above.
- `GET /api/v1/collab/{doc_id}/history` and `/as-of/{version}` — the time-travel reads.
- `POST /api/v1/collab/{doc_id}/seed` — seed a document's initial fields.

> **Requires the `websockets` library** for uvicorn's WebSocket support (in `requirements.txt`); a
> plain `uvicorn` without it will 404 the WS upgrade. Presence/state is in-process per replica (like
> the SSE feed); for cross-replica fan-out, layer Redis pub/sub the same way. The durable artifact is
> the version history.

## 10. Testing

| Suite | Command | Covers |
|---|---|---|
| Backend unit/integration | `python3 -m unittest discover -s apps/python-executor/tests` | 640+ tests, incl. circuit breaker, WAL (real SIGKILL subprocess), backtest full mode |
| **Executable matrix** (2,000+ real assertions, 12 domains) | `python3 -m simulation.full_matrix` (or `unittest tests.test_full_matrix`) | conformance, sandbox, RBAC, dedupe, AI grounding, breaker, WAL, PII, load |
| Cross-engine conformance | Rust `cargo test --no-default-features`; Dart `flutter test`; Kotlin `./gradlew :rulemind-core:test`; TS via evaluator | the 90-case operator spec on all 5 engines |
| Rust/PyO3 build | `cd packages/rulemind-core-rs && maturin build --release && pip install target/wheels/*.whl` | native accelerator (fast path) |

---

## 11. Known limitations / operator to-dos

- **Default PII set** covers identity fields only — configure `RULEMIND_PII_REDACT_KEYS` for
  credential-like fields (§5).
- **`DECISION_WAL` is opt-in** — enable it where a hard-kill audit gap is unacceptable (§3).
- **Rust/PyO3 wheel** is built on demand (not committed) — build it in CI/image for the native fast
  path; the pure-Python core is the always-available correct fallback.
- **Circuit-breaker state is per-replica** — intentional; if you need a globally-coordinated breaker,
  that is a future enhancement (the local breaker already bounds blast radius per replica).
- Shadow/dark-launch, what-if KPI simulation, release snapshots + one-click rollback, and the
  **collaborative editor** (CRDT live-sync + presence + time-travel UI) are now **shipped** (§9) with
  tests — no longer backlog. The collab WebSocket needs the `websockets` lib (§9).
