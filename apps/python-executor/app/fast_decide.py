"""Cached-bundle fast decision path (the scalable hot path).

Serves production decisions by:
  1. caching a tenant/policy's compiled serving bundle **cross-request**
     (invalidated on publish/update),
  2. computing variables inline (no DB, no deep-copies),
  3. evaluating via the stateless core — the Rust `Bundle` when available (and the
     policy is rules-only), else the pure-Python `core.decide`.

This bypasses the heavy `PolicyExecutor` (per-step deep-copies, full trace, and
~3 DB writes per request) which is retained for authoring and for policies that
need live I/O (action / review_gate / transform / model steps).

Enable with FAST_DECIDE=1. Falls back to the standard path for any policy it
cannot safely serve.
"""
from __future__ import annotations

import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, Optional, Tuple

from . import env, input_schema
from . import variable_reachability
from .core import decide as core_decide
# Imported (not redeclared) so the exception the fast path raises is the SAME CLASS the
# executor raises: main.py dispatches its 504 on `@app.exception_handler(EvaluationBudgetExceeded)`
# by class identity, and a look-alike declared here would miss that handler and surface a
# retryable timeout as a 500 that pages on-call. `PolicyExecutor` is imported for
# DEFAULT_BUDGET_MS alone, so the two paths cannot drift to different defaults.
# executor.py imports this module only from inside functions, so this edge is not a cycle.
from .executor import EvaluationBudgetExceeded, PolicyExecutor
from .logic import now_iso, redact_payload
from .sandbox import _BoundedCache, execute_variable

# No `import rulemind_core_rs` here any more. This module used to build a Rust bundle and
# serve decisions from it; that branch returned score/rule_results/trace hardcoded empty and is
# gone -- the full reasoning is in _build_serving_bundle below. The Rust core is still used by
# packages/rulemind-decide-service and the WASM entry point, where the caller asks for a traced
# or untraced answer explicitly instead of having one silently emptied.

# step types that need live I/O or human interaction -> not fast-servable
# Step types the fast path CANNOT serve, so a policy containing one falls through to the full
# executor. This is a correctness list, not a performance one.
#
# loop/branch/workflow/monitor were missing, and the consequence was the T1.2 failure mode
# again in a new place: is_fast_servable() said yes, the compiled core did not recognise the
# step, and it was silently skipped. A parent policy shaped [run KYC child, outcome approve]
# therefore returned APPROVE at HTTP 200 with the gating child never executed -- but only under
# FAST_DECIDE=1, which turns a performance flag into a correctness switch. The slow path was
# fixed to fail closed on an unreachable child; the fast path has to DECLINE the policy instead,
# because it cannot execute one at all.
_IO_STEP_TYPES = {
    "action", "review_gate", "transform", "model",
    "loop", "branch", "workflow", "monitor",
}

_CACHE_LOCK = threading.Lock()

# Every cache in this module is bounded. `_SERVING` is keyed by tenant+policy and
# `_LOCAL_EPOCH`/`_EPOCH_CACHE` by tenant, so on a multi-tenant deployment they grew with the
# number of distinct tenant/policy pairs ever served and were never trimmed — a slow leak in
# the hot path. `_BoundedCache` (sandbox.py) is the one bounded cache in this tree; an evicted
# entry is simply rebuilt on next use, so eviction costs latency, never correctness.
_CACHE_CAPACITY = env.env_int("FAST_CACHE_MAX_ENTRIES", 2048)

# key -> (epoch_built_at, built_at_monotonic, bundle). The epoch stamp lets a replica that
# never received the in-process invalidate() call still detect a stale bundle and rebuild —
# see _current_epoch. The timestamp is the TTL backstop described on _CACHE_TTL.
_SERVING: Dict[str, Tuple[int, float, Dict[str, Any]]] = _BoundedCache(_CACHE_CAPACITY)

# TTL ceiling on any epoch-stamped entry, INDEPENDENT of the epoch.
#
# The epoch is bumped by main.maybe_compile_bundle, which the routers call on rule/policy/
# prod-variable writes — but NOT on connector create/update/delete, and not on a `dev`
# variable edit. Those still change what a decision computes (a connector's sample_payload is
# the field set feeding every variable of that source, and its config holds the secrets). With
# epoch coherence alone, such an edit would be invisible until something else bumped the
# epoch, which could be never. The TTL bounds that to seconds without giving up the epoch's
# instant invalidation for the cases that do bump it.
_CACHE_TTL = env.env_float("FAST_CACHE_TTL_SECONDS", 5.0)


class _SingleFlight:
    """One build per key at a time; everybody else waits and reuses the result.

    Without this, a TTL/epoch cache converts an N+1 into a thundering herd: every request
    in flight at the instant an entry expires misses, and they all run the full rebuild
    concurrently — which on the executor catalog means N connector reads and N Fernet
    decrypts each. The stampede lands precisely at peak load, because that is when the most
    requests are in flight per expiry.

    Self-cleaning: the per-key lock is dropped once the last waiter leaves, so this does not
    become another unbounded dict of locks.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: Dict[str, threading.Lock] = {}
        self._waiters: Dict[str, int] = {}

    @contextmanager
    def lock_for(self, key: str) -> Iterator[None]:
        with self._guard:
            entry = self._locks.get(key)
            if entry is None:
                entry = self._locks[key] = threading.Lock()
            self._waiters[key] = self._waiters.get(key, 0) + 1
        entry.acquire()
        try:
            yield
        finally:
            entry.release()
            with self._guard:
                remaining = self._waiters.get(key, 1) - 1
                if remaining <= 0:
                    self._waiters.pop(key, None)
                    self._locks.pop(key, None)
                else:
                    self._waiters[key] = remaining

    def in_flight(self) -> int:
        """Number of keys currently being built — test/observability hook."""
        with self._guard:
            return len(self._locks)


_FLIGHT = _SingleFlight()

# --- Cross-replica cache coherence -------------------------------------------------
# The in-memory serving cache is per-process. In a multi-replica deployment (many workers
# across many pods), invalidate() on the worker that handled the edit clears only THAT
# process; siblings would serve a stale bundle indefinitely (until restart). To fix this
# without a per-request DB/Redis round-trip, every tenant carries a monotonic cache
# "epoch": bumped on invalidate() (Redis INCR when Redis is present, else a process-local
# counter), and read on the decide path with a short TTL cache. A cached bundle whose
# stamped epoch != the tenant's current epoch is rebuilt. Worst-case cross-replica
# staleness is therefore FAST_CACHE_EPOCH_TTL (default 1s; set 0 to check every decision),
# never unbounded. Single-process/dev is unaffected (local counter + local clear).
_EPOCH_KEY = "rulemind:fastcache:epoch:{0}"
_EPOCH_TTL = env.env_float("FAST_CACHE_EPOCH_TTL", 1.0)
_LOCAL_EPOCH: Dict[str, int] = _BoundedCache(_CACHE_CAPACITY)
# tenant_id -> (fetched_at_monotonic, epoch)
_EPOCH_CACHE: Dict[str, Tuple[float, int]] = _BoundedCache(_CACHE_CAPACITY)

# The tenant catalog the SLOW path (PolicyExecutor) needs: connectors, variables, settings
# and decrypted connector secrets. Same store shape and same epoch/TTL rules as _SERVING —
# deliberately the same mechanism rather than a third caching layer.
_CATALOG: Dict[str, Tuple[int, float, Dict[str, Any]]] = _BoundedCache(_CACHE_CAPACITY)


def _redis():  # pragma: no cover - trivial indirection, exercised via integration
    """The shared sync Redis client, or None when Redis is not configured/reachable."""
    try:
        from .runtime import redis_client

        return redis_client()
    except Exception:
        return None


def _current_epoch(tenant_id: str) -> int:
    """The tenant's current cache epoch, read at most once per FAST_CACHE_EPOCH_TTL.

    Redis-backed (authoritative across replicas) when available, else a process-local
    counter (correct for single-process). Never raises — a Redis hiccup falls back to the
    last known / local value so decisions keep serving.
    """
    now = time.monotonic()
    hit = _EPOCH_CACHE.get(tenant_id)
    if hit is not None and (now - hit[0]) < _EPOCH_TTL:
        return hit[1]
    epoch = _LOCAL_EPOCH.get(tenant_id, 0)
    client = _redis()
    if client is not None:
        try:
            raw = client.get(_EPOCH_KEY.format(tenant_id))
            if raw is not None:
                epoch = int(raw)
        except Exception:
            pass  # keep the local/last value; correctness degrades to single-process semantics
    _EPOCH_CACHE[tenant_id] = (now, epoch)
    return epoch


def is_fast_servable(policy: Dict[str, Any]) -> bool:
    steps = policy.get("steps", []) or []
    return not any(step.get("type") in _IO_STEP_TYPES for step in steps)


def fast_path_eligible(storage: Any, policy: Dict[str, Any], tenant_id: str) -> bool:
    """THE single authority for whether a LIVE decision may take the fast path. Both the eligible
    shape (pure-compute steps only) AND the runtime guard (no running A/B experiment — the fast
    path applies no experiment overrides) live here, so the /decide endpoint can't drift from the
    executor on when the fast path is safe. (The A/B assignment bug came from these being split.)"""
    if not is_fast_servable(policy):
        return False
    if policy.get("id") in running_experiment_policy_ids(storage, tenant_id):
        return False
    return not tenant_fetches_sources(storage, tenant_id)


def running_experiment_policy_ids(storage: Any, tenant_id: str) -> frozenset:
    """Policies with a RUNNING experiment, cached like the rest of the tenant catalog.

    This was `storage.list_experiments(tenant_id=...)` inline, uncached, on EVERY decision --
    and worse, inside the eligibility check, so it was paid even when the policy turned out to
    be ineligible and `FAST_DECIDE` did nothing at all. Every other catalog read on this path
    (variables, connectors, settings, secrets) already goes through the epoch+TTL cache; this
    one did not, purely because it was written as a loop rather than a lookup.

    CACHING IT IS ONLY SAFE BECAUSE THE WRITES INVALIDATE. A stale answer here means the fast
    path serves a policy whose experiment has just started, with no variant assignment and no
    overrides -- which is the A/B assignment bug this function exists to prevent, reintroduced
    through a cache. So `Storage.create_or_update_experiment` and `Storage.delete_experiment`
    now call `invalidate(tenant_id)`. Placed at the STORAGE layer rather than in the router
    because the scheduler's auto-rollback writes experiment status too, and a cache correct
    only for HTTP callers is not correct.
    """
    def _build() -> frozenset:
        return frozenset(
            experiment.get("target_policy_id")
            for experiment in storage.list_experiments(tenant_id=tenant_id)
            if experiment.get("status") == "running" and experiment.get("target_policy_id")
        )

    result: frozenset = _epoch_cached(_CATALOG, f"{tenant_id}:running_experiments", tenant_id,
                                      _build)
    return result


def tenant_fetches_sources(storage: Any, tenant_id: str) -> bool:
    """Has this tenant switched decision-time source fetching on for any connector?

    If so the fast path must DECLINE the decision, exactly as it declines a policy containing an
    IO step: this module retrieves nothing. A connector configured to pull a live bureau report
    would contribute no data at all here, so every variable reading it would fail and the
    decision would come back `review` — a policy that works through the executor answering
    "cannot decide" purely because a performance flag was set.

    (This paragraph used to say the decision would be made on the connector's `sample_payload`
    "at HTTP 200", which was true when `_compute_variables` seeded every source unconditionally.
    It no longer does on production traffic — see the seeding comment there — so the failure mode
    moved from silently-wrong to loudly-unavailable. Still a reason to decline, and still a
    performance flag acting as a correctness switch, but worth stating accurately: the earlier
    version would send someone hunting for a fail-open that is now closed.)

    Declining rather than implementing: `fast_decide` is a synchronous function called from
    Starlette's threadpool and from a 32-way batch ThreadPoolExecutor, and a source fetch is an
    async HTTP call inside a wall-clock budget. Bolting one in here would be a second transport,
    a second cache and a second fail-closed policy to keep in step with the executor's — the
    divergence this decline exists to prevent. Tenants that opt in pay the full executor;
    everyone else is untouched, because the check is false for every connector without the flag.
    """
    from .connector_fetch import fetch_enabled

    catalog = tenant_catalog(storage, tenant_id)
    return any(fetch_enabled(connector) for connector in catalog["connectors"].values())


def invalidate(tenant_id: Optional[str] = None) -> None:
    """Drop cached bundles (all, or one tenant) — call on any publish/update.

    For a specific tenant this also **bumps the tenant's cache epoch** so sibling replicas
    (which never saw this call) rebuild on their next epoch read. With Redis the bump is a
    global INCR (authoritative for all replicas); without Redis it is a process-local
    counter (single-process correctness). tenant_id=None is a full local reset (tests).
    """
    with _CACHE_LOCK:
        if tenant_id is None:
            _SERVING.clear()
            _CATALOG.clear()
            _EPOCH_CACHE.clear()
            _LOCAL_EPOCH.clear()
            return
        prefix = f"{tenant_id}:"
        for store in (_SERVING, _CATALOG):
            for key in [k for k in store if k.startswith(prefix)]:
                store.pop(key, None)
        _LOCAL_EPOCH[tenant_id] = _LOCAL_EPOCH.get(tenant_id, 0) + 1
        # Force this process to re-read the epoch next time (don't serve from its TTL cache).
        _EPOCH_CACHE.pop(tenant_id, None)
    client = _redis()
    if client is not None:
        try:
            client.incr(_EPOCH_KEY.format(tenant_id))
        except Exception:  # pragma: no cover - best-effort; local clear already happened
            pass


def _epoch_cached(store: Dict[str, Tuple[int, float, Any]], key: str, tenant_id: str,
                  build: Callable[[], Any]) -> Any:
    """Cross-request cache: epoch-coherent, TTL-bounded, single-flighted.

    Read fast without any lock; on a miss take the per-key flight lock and re-check, so a
    burst of concurrent misses on one key produces exactly one build.
    """
    epoch = _current_epoch(tenant_id)
    now = time.monotonic()
    cached = store.get(key)
    if cached is not None and cached[0] == epoch and (now - cached[1]) < _CACHE_TTL:
        return cached[2]
    with _FLIGHT.lock_for(key):
        # Re-check: whoever held the lock before us has just published a fresh entry, and
        # rebuilding it again is exactly the stampede this lock exists to prevent.
        cached = store.get(key)
        now = time.monotonic()
        if cached is not None and cached[0] == epoch and (now - cached[1]) < _CACHE_TTL:
            return cached[2]
        built = build()
        with _CACHE_LOCK:
            store[key] = (epoch, time.monotonic(), built)
        return built


def _build_tenant_catalog(storage: Any, tenant_id: str) -> Dict[str, Any]:
    """The connectors / variables / settings a PolicyExecutor needs for a tenant. 4 DB reads.

    Secrets are deliberately NOT built here: only action / transform / review-gate steps read
    them, so folding them in would charge every pure-rule decision — and every case in a
    backtest — for N connector reads it never uses. They get their own cache entry below.
    """
    variables = storage.list_variables(tenant_id=tenant_id)
    return {
        "connectors": {item["id"]: item for item in storage.list_connectors(tenant_id=tenant_id)},
        "variables": variables,
        "variable_lookup": {item["id"]: item for item in variables},
        "settings": storage.get_settings(tenant_id=tenant_id),
    }


def _build_tenant_secrets(storage: Any, tenant_id: str, connectors: Dict[str, Any]) -> Dict[str, Any]:
    """Decrypted connector secrets: one DB read plus one Fernet decrypt per connector.

    This loop is the "+N" in the executor's 8+N queries per decision — it ran on every request
    that touched an action/transform step, because the executor memoizing it was itself
    per-request."""
    secrets: Dict[str, Any] = {}
    for connector_id in connectors:
        raw = storage.get_connector(connector_id, include_secrets=True, tenant_id=tenant_id) or {}
        for key, value in (raw.get("config", {}) or {}).items():
            if any(marker in key.lower() for marker in ("token", "secret", "password", "api_key", "apikey")):
                secrets[key] = value
    return secrets


def tenant_catalog(storage: Any, tenant_id: str) -> Dict[str, Any]:
    """Shared, cross-request tenant catalog for the SLOW path (PolicyExecutor).

    A PolicyExecutor is constructed fresh per request (main.workflow_executor), so its
    per-instance memo never survived one. Every /decide therefore re-read variables,
    connectors and settings from the DB.

    **Treat the returned dict as read-only.** It is shared across requests and threads; the
    executor's old `cat["secrets"] = ...` write-back is gone precisely because a shared entry
    must not be mutated in place.
    """
    result: Dict[str, Any] = _epoch_cached(_CATALOG, f"{tenant_id}:catalog", tenant_id,
                                           lambda: _build_tenant_catalog(storage, tenant_id))
    return result


def tenant_secrets(storage: Any, tenant_id: str, connectors: Dict[str, Any]) -> Dict[str, Any]:
    """Shared, cross-request connector secrets. Read-only, same epoch/TTL rules as the catalog."""
    result: Dict[str, Any] = _epoch_cached(_CATALOG, f"{tenant_id}:secrets", tenant_id,
                                           lambda: _build_tenant_secrets(storage, tenant_id, connectors))
    return result


# ── Evaluation budget ──────────────────────────────────────────────────────────────
# The Tier 1.3 wall-clock bound was executor-only: this module had no deadline of any kind, so
# FAST_DECIDE=1 removed the bound on a decision entirely. The bound matters MORE here, not less
# — with COMPUTE_ONLY_USED_VARS off (the default) the fast path computes the tenant's whole
# variable catalog, and the seeded demo tenant is 55 variables at the 2000ms per-variable
# timeout, i.e. 110 seconds of one Starlette threadpool thread for a single request.
#
# Same engine_config key, same default, same clamping, same exception as PolicyExecutor.


class _Budget:
    """Wall-clock deadline for one fast-path decision. budget_ms 0 disables it.

    `anchor` is a time.perf_counter() reading taken before ANY work, so the bundle build — up to
    6 DB reads on a cache miss, plus a single-flight wait behind another thread's build — is
    inside the budget. A decision that spends its whole budget in the DB is just as timed out as
    one that spends it in the sandbox, and the caller cannot tell the difference anyway.

    Cooperative, like the executor's: /decide is a sync `def` served from the threadpool, so
    cancelling an await would return 504 while the work carried on holding its thread.
    """

    __slots__ = ("budget_ms", "deadline")

    def __init__(self, budget_ms: int, anchor: float) -> None:
        self.budget_ms = max(0, budget_ms)
        self.deadline = (anchor + self.budget_ms / 1000.0) if self.budget_ms else None

    def remaining_ms(self) -> Optional[int]:
        """Milliseconds left, or None when no budget is set."""
        if self.deadline is None:
            return None
        return int((self.deadline - time.perf_counter()) * 1000)

    def check(self, stage: str) -> None:
        remaining = self.remaining_ms()
        if remaining is not None and remaining <= 0:
            # budget + overshoot: how far past the deadline the last unit of work actually ran,
            # which is what tells an operator whether the budget is too tight or one step misbehaves.
            raise EvaluationBudgetExceeded(self.budget_ms, self.budget_ms - remaining, stage)


def _budget_ms(engine_config: Dict[str, Any]) -> int:
    """`engine_config.evaluation_budget_ms`, read exactly as PolicyExecutor._start_budget reads it.

    A malformed value falls back to the default rather than disabling the budget — reading
    `"abc"` as "no limit" would turn a typo into the unbounded request this exists to prevent.
    """
    try:
        budget = int((engine_config or {}).get("evaluation_budget_ms", PolicyExecutor.DEFAULT_BUDGET_MS))
    except (TypeError, ValueError):
        budget = PolicyExecutor.DEFAULT_BUDGET_MS
    return max(0, budget)


def _build_serving_bundle(storage: Any, tenant_id: str, policy: Dict[str, Any]) -> Dict[str, Any]:
    variables = storage.list_variables(tenant_id=tenant_id)
    rules = {item["id"]: item for item in storage.list_rules(tenant_id=tenant_id)}
    scorecards = {item["id"]: item for item in storage.list_scorecards(tenant_id=tenant_id)}
    decision_tables = {item["id"]: item for item in storage.list_decision_tables(tenant_id=tenant_id)}
    connectors = {
        item["id"]: (item.get("sample_payload") or {})
        for item in storage.list_connectors(tenant_id=tenant_id)
    }
    # A source's DECLARED field set, carried separately from its sample.
    #
    # The flat-field override below used to ask `if field in sample`, which reads a source's
    # field set off whatever keys its sample_payload happened to have. That worked only because
    # samples were seeded unconditionally, so switching production traffic off seeding (correctly)
    # broke it -- an empty source has no keys, so {"bureau_score": 590} reached nothing. The
    # executor never had that coupling: _payloads_by_source iterates
    # `connector["schema_paths"]`, which is the declaration and does not depend on a sample
    # existing. Same source of truth here now.
    schema_paths = {
        item["id"]: [str(field) for field in (item.get("schema_paths") or [])]
        for item in storage.list_connectors(tenant_id=tenant_id)
    }
    settings = storage.get_settings(tenant_id=tenant_id).get("engine_config", {})
    # core_bundle["variables"] is the FULL catalog and stays that way even when the compute
    # loop is filtered. The core reads it as a LOOKUP (labels, field types for trace), not as
    # a work list, and PolicyExecutor does the same -- its `variable_lookup` comes from the
    # unfiltered catalog while only _compute_variables honours needed_ids. Filtering here
    # would change trace labels on the fast path only: divergence, in the opposite direction.
    core_bundle = {"policy": policy, "rules": rules, "scorecards": scorecards,
                   "decision_tables": decision_tables, "variables": variables}

    # NO RUST BUNDLE IS BUILT HERE ANY MORE, and that is the fix rather than an omission.
    #
    # What this used to do: when the Rust extension was importable and the policy had no
    # scorecard or decision table, build a `rulemind_core_rs.Bundle` and serve the decision
    # from it. The branch that consumed it returned
    #
    #     {"score": None, "rule_results": [], "trace": []}
    #
    # hardcoded -- while the Python branch one line below returned all three populated.
    # `trace` and `rule_results` are ~66% of the stored Decision row and they are the entire
    # answer to "why was this rejected", which is the question this product exists to answer.
    # So a decision taken through the Rust branch was recorded as UNEXPLAINABLE: same outcome,
    # no reasons, and nothing anywhere said so. For a regulated buyer that is not a performance
    # trade-off, it is an unusable record.
    #
    # Three ways to fix it, and the reason for the one taken:
    #
    #   1. Populate from `decide_traced` (the Rust side HAS it, and its per-rule JSON matches
    #      the Python evaluation dict key for key). But the STEP-level trace also carries an
    #      entry per `outcome` step with the running merged outcome, so reconstructing it needs
    #      `_merge_outcome` re-implemented on this side -- a second copy of the merge semantics,
    #      which is precisely the drift the five-engine parity contract exists to stop.
    #   2. Take the Rust branch only when no trace is required. On this path a trace is ALWAYS
    #      required: `fast_decide` either logs the decision (the trace is the record) or is
    #      serving a simulation (the trace is the output). There is no caller that does not
    #      need it, so the condition is never true.
    #   3. Do not take it. Which is 2, stated honestly.
    #
    # And the cost of dropping it is the part worth writing down, because it is not what the
    # README implies. The Rust core replaces RULE-TREE EVALUATION -- under 100 microseconds of
    # a ~50 ms decision (`simulation/LOAD_TEST_RESULTS.md:37-41` said rule evaluation was never
    # the bottleneck). Ceiling of the swap: 1-2%. And it requires a policy with no scorecard
    # and no decision table on top of the fast path's own exclusions: queried against the live
    # API, ZERO of the four seeded policies could ever reach it. So this deletes a path that
    # served no shipped policy, could not explain the decisions it did serve, and was worth
    # one or two percent if it had.
    #
    # The Rust core is not dead: `rulemind-decide-service` and the WASM entry point both call
    # it, and there the CALLER chooses traced or untraced explicitly rather than having the
    # answer silently emptied. `app/variable_reachability.py` + the PyO3 `decide_from_payload`
    # spike (plan item 4.5) is the version of this that would attack the real cost -- the
    # subprocess sandbox -- and that is measured before it is built, not after.

    # Which variables this policy can actually reach -- the SAME analysis the executor runs
    # (app/variable_reachability.py owns it; a second copy here is precisely the drift that
    # makes a performance flag change the outcome).
    #
    # Computed at BUILD time and cached, applied PER REQUEST. The flag is read per request
    # and the parity tests flip it mid-process with mock.patch.dict, so baking the flag's
    # answer into a cached bundle would make it sticky per cache entry -- the first request
    # after a rebuild would decide the mode for every later one. The SET does not depend on
    # the flag, only on the policy, so caching the set and gating on the flag at use time is
    # both correct and free.
    #
    # Cache-safe because _serving_bundle is keyed per policy (f"{tenant_id}:{policy['id']}"),
    # so this set can never be read by a different policy.
    #
    # get_policy is wired: the serving bundle holds only THIS policy, so a sub-workflow step
    # would otherwise be opaque here while the executor walked into it -- a divergence in the
    # analysis meant to prevent divergence.
    needed = variable_reachability.needed_variable_ids(
        policy, rules, scorecards, decision_tables,
        {v["id"]: v for v in variables},
        lambda ref: storage.get_policy(ref, tenant_id=tenant_id),
    )

    return {
        "policy": policy,
        "variables": variables,
        "connectors": connectors,
        "schema_paths": schema_paths,
        "core_bundle": core_bundle,
        "needed_variable_ids": needed,
        "timeout_ms": int(settings.get("timeout_ms", 2000)),
        "memory_mb": int(settings.get("memory_mb", 128)),
        "budget_ms": _budget_ms(settings),
    }


def _serving_bundle(storage: Any, tenant_id: str, policy: Dict[str, Any]) -> Dict[str, Any]:
    """The cached serving bundle. Single-flighted since the epoch scheme was added: on
    expiry, every concurrent request used to run the full 6-query rebuild (plus a Rust
    Bundle compile) simultaneously — a stampede whose size is proportional to load."""
    key = f"{tenant_id}:{policy['id']}"
    result: Dict[str, Any] = _epoch_cached(
        _SERVING, key, tenant_id, lambda: _build_serving_bundle(storage, tenant_id, policy))
    return result


def _compute_variables(bundle: Dict[str, Any], payload: Dict[str, Any],
                       budget: Optional["_Budget"] = None,
                       seed_samples: bool = False) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, str]]:
    connectors = bundle["connectors"]
    # SEEDING IS OFF BY DEFAULT, and that default is the whole point.
    #
    # This used to seed every source with its sample_payload unconditionally, which made
    # FAST_DECIDE=1 decide differently from the executor on production traffic. The executor
    # gates it -- `seed_samples = self._source in _SAMPLE_SEEDING_SOURCES` at
    # executor._payloads_by_source -- with the comment saying exactly why: on production traffic
    # an unsupplied field must STAY unsupplied, so the variable that reads it fails and the
    # decision fails closed, rather than quietly evaluating against demo data.
    #
    # `api` is not in that set, so /decide through the executor left the source empty. /decide
    # through the fast path filled it with an approving sample. Same policy, same payload,
    # different decision, chosen by a performance flag -- the third instance of this exact shape
    # after input validation and the evaluation budget, and the reason all three are worth
    # calling a correctness bug rather than a gap.
    #
    # The author-driven surfaces still seed, and still match: _run_decision_batch passes True
    # here and `source="simulation"` on its executor branch, which IS in _SAMPLE_SEEDING_SOURCES.
    payloads: Dict[str, Any] = (
        {cid: dict(sample) for cid, sample in connectors.items()} if seed_samples
        else {cid: {} for cid in connectors})
    flat_fields: Dict[str, Any] = {}
    for key, value in payload.items():
        if key in connectors and isinstance(value, dict):
            payloads[key] = dict(value)
        else:
            flat_fields[key] = value
    # Field-level override so a flat input (bureau_score=590) drives the source's
    # variables instead of the source silently keeping its approving sample.
    # (A connector's sample keys are its field set here.) Mirrors the executor.
    if flat_fields:
        # Field-level override, driven by each source's DECLARED schema_paths -- the same source
        # of truth executor._payloads_by_source uses. Asking `if field in sample` instead read the
        # field set off the sample, which silently made this depend on seeding being on.
        for connector_id, fields in (bundle.get("schema_paths") or {}).items():
            target = payloads.get(connector_id)
            if not isinstance(target, dict):
                continue
            for field in fields:
                if field in flat_fields:
                    target[field] = flat_fields[field]
    payloads["custom"] = dict(flat_fields) if flat_fields else dict(payload)
    values: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    timeout_ms = bundle["timeout_ms"]
    # Per-request, never baked into the cached bundle -- see the note in _build_serving_bundle.
    # `.get`, not `[...]`, because "needed_variable_ids" is an OPTIONAL bundle key: tests and
    # callers hand-roll serving bundles (test_simulation_accuracy builds one with five keys),
    # and a missing key has to mean "compute everything", which is the unfiltered default.
    needed_ids = bundle.get("needed_variable_ids") if variable_reachability.compute_only_used_vars() else None

    for variable in bundle["variables"]:
        # Skip BEFORE the budget check, exactly as PolicyExecutor._compute_variables does --
        # a variable that is never computed must not consume budget either, or the two paths
        # would abandon the run at different points for the same policy.
        if needed_ids is not None and variable["id"] not in needed_ids:
            continue
        # Two-part enforcement, mirroring PolicyExecutor._compute_variables. The check abandons
        # the run between variables; the clamp stops the NEXT variable from overshooting the
        # budget on its own -- without it a 2000ms variable started with 5ms of budget left
        # still runs the full 2000ms, and across a 55-variable catalog that error compounds
        # until the effective bound is timeout_ms x variable_count again.
        effective_timeout = timeout_ms
        if budget is not None:
            budget.check("variable computation")
            remaining = budget.remaining_ms()
            if remaining is not None:
                effective_timeout = max(1, min(timeout_ms, remaining))
        source_payload = payloads.get(variable.get("source_id"), {})
        execution = execute_variable(
            variable["code"], source_payload, values,
            timeout_ms=effective_timeout, memory_mb=bundle["memory_mb"],
        )
        values[variable["id"]] = execution.get("value")
        # A variable that errors becomes None here; surface the error to the caller so it is
        # never silently swallowed (a None fed to a gate can flip a decision).
        if execution.get("error"):
            errors[variable["id"]] = str(execution.get("error"))
    return values, payloads, errors


def fast_decide(storage: Any, policy: Dict[str, Any], payload: Dict[str, Any], tenant_id: str,
                log: bool = True, user_id: Optional[str] = None,
                seed_samples: bool = False) -> Dict[str, Any]:
    started = time.perf_counter()
    bundle = _serving_bundle(storage, tenant_id, policy)
    budget = _Budget(bundle["budget_ms"], started)
    # BEFORE any variable is computed, exactly like PolicyExecutor.execute. Validation that runs
    # after the work is not a guard, it is a report -- and the whole point is that the unchecked
    # keys were becoming None and quietly re-phrasing gates.
    validation_trace = _validate_input(storage, tenant_id, policy["id"], payload or {})
    values, resolved_payload, variable_errors = _compute_variables(
        bundle, payload or {}, budget, seed_samples=seed_samples)
    # Variable computation errors on the fast path are recorded as observable error events (not
    # silently dropped), so a decision made on a None-defaulted variable is alertable.
    if variable_errors:
        for variable_id, message in variable_errors.items():
            try:
                storage.add_error_event(
                    {
                        "tenant_id": tenant_id, "scope": "decision", "stage": "variable_error",
                        "entity_type": "variable", "entity_id": variable_id,
                        "message": "Variable errored on the fast path (value defaulted to null): {0}".format(message),
                        "details": {"policy_id": policy["id"]},
                    },
                    tenant_id=tenant_id,
                )
            except Exception:  # pragma: no cover - observability must not break the decision
                pass

    # The executor checks the deadline before every step; the compiled core runs the whole step
    # list as one unit, so this is the equivalent boundary. Without it a run whose variables ate
    # the entire budget would still go on to evaluate.
    budget.check("policy evaluation")
    # ONE branch. There used to be two, and the Rust one returned score/rule_results/trace
    # hardcoded empty -- see the long note in _build_serving_bundle for why it is gone.
    result = core_decide(bundle["core_bundle"], payload or {}, {"variables": values, "strict_validation": False})
    # Lineage, on the path that actually serves from a compiled bundle. Policies mutate in
    # place, so policy_id alone cannot say what was live when this decision was made.
    result.setdefault("policy_version", policy.get("version"))

    latency_ms = round((time.perf_counter() - started) * 1000, 3)
    # Warn-mode issues lead the trace, as they do on the slow path: the executor appends its
    # input_validation entry before any step runs, and an operator diffing the two traces must
    # not see the same decision described in a different order.
    trace = list(result.get("trace", []))
    if validation_trace is not None:
        trace.insert(0, validation_trace)
    decision = {
        "policy_id": policy["id"],
        # THE RESPONSE SHAPE MUST NOT DEPEND ON THE FLAG. This dict is returned verbatim to the
        # caller of POST /api/v1/decide when FAST_DECIDE=1, and it had no `decision_id` at all
        # -- the slow path's response has carried one since it existed. So an integrator who
        # deep-links /decisions/{id}/reason-codes broke silently the moment an operator flipped
        # a flag documented as a performance toggle. That is the same divergence class this
        # file warns about three times for fail-closed semantics; the response shape was missed.
        #
        # None when log=False (simulation, shadow, what-if): no Decision row is written, so
        # there is no id to name, and inventing one would be a lineage fabrication. The KEY is
        # always present, because a key that appears and disappears is its own divergence.
        "decision_id": str(uuid.uuid4()) if log else None,
        # Same reasoning: the slow path reports which version of the policy decided, so this
        # one must too. (And `policy_version` here is what makes the logged row attributable.)
        "policy_version": policy.get("version"),
        "outcome": result["outcome"] if result["outcome"] != "pending" else policy.get("defaultOutcome", "review"),
        "score": result.get("score"),
        "variables": values,
        "variable_errors": dict(variable_errors),
        "rule_results": result.get("rule_results", []),
        "scorecard_result": result.get("scorecard_result"),
        "trace": trace,
        "latency_ms": latency_ms,
    }
    # Recording the error above is observability; it does not make the decision correct. This
    # path was still RETURNING the fail-open outcome, so enabling FAST_DECIDE silently undid the
    # slow path's guard. The two paths must agree on semantics or the flag is a correctness
    # switch rather than a performance one.
    _fail_closed_on_variable_errors(storage, tenant_id, decision, variable_errors)
    if log:
        _log_decision(storage, tenant_id, decision, resolved_payload, values, user_id)
        # Dark launch: if a shadow candidate is registered for this policy, evaluate it on the same
        # payload and log what it WOULD have decided — never affects this returned decision.
        try:
            from . import shadow

            shadow.run_shadow(storage, policy["id"], resolved_payload, decision["outcome"], tenant_id)
        except Exception:  # pragma: no cover - dark launch must never break the live path
            pass
    return decision


def _validate_input(storage: Any, tenant_id: str, policy_id: str,
                    payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Fast-path twin of PolicyExecutor._validate_input (P06).

    This module imported input_schema nowhere, so a tenant that had switched nested validation on
    got it enforced on the slow path and NOT on the fast path -- FAST_DECIDE=1 silently unwired an
    opt-in safety check, which is the third time this flag has changed correctness rather than
    speed. An unchecked nested key resolves to None, logic.py compares False against None, and the
    direction a typo breaks is then decided by how the rule author happened to word the gate.

    Reads the SAME connectors and the SAME engine_config the executor reads (tenant_catalog is
    literally what PolicyExecutor._catalog_for returns for a live decision), so the two paths
    cannot resolve different settings or validate against a different schema.

    Returns the warn-mode trace entry to prepend, or None. Raises input_schema.InputValidationError
    -- the executor's exception, not a copy -- so /decide answers the same 422 with the same body
    whichever path served it. Fails CLOSED: nothing downstream of this runs when it raises.
    """
    catalog = tenant_catalog(storage, tenant_id)
    settings = input_schema.resolve_settings(
        catalog["settings"].get("engine_config", {}) or {}, policy_id)
    if not settings.enabled:
        return None
    issues = input_schema.validate_payload(payload, catalog["connectors"], settings)
    if not issues:
        return None
    blocking = [issue for issue in issues if settings.blocks(issue)]
    _record_input_issues(storage, tenant_id, policy_id, issues, blocked=bool(blocking))
    entry = {
        "type": "input_validation",
        "status": "rejected" if blocking else "warned",
        "mode": settings.mode,
        "issues": [issue.to_dict() for issue in issues],
        "detail": "{0} input problem(s) found; {1} of them block the decision.".format(
            len(issues), len(blocking)),
    }
    if blocking:
        raise input_schema.InputValidationError(issues, policy_id=policy_id, blocking=blocking)
    return entry


def _record_input_issues(storage: Any, tenant_id: str, policy_id: str,
                         issues: Any, blocked: bool) -> None:
    """Make a bad payload observable whether or not it was rejected — see the executor's twin.

    Carries no execution_id because the fast path writes no workflow_execution row; every other
    field matches, so an operator's error-event query finds both paths' issues together instead
    of seeing warn mode go quiet the moment FAST_DECIDE was turned on.
    """
    try:
        storage.add_error_event(
            {
                "tenant_id": tenant_id,
                "scope": "decision",
                "stage": "input_validation",
                "entity_type": "policy",
                "entity_id": policy_id,
                "message": "Input payload {0}: {1} problem(s) at {2}".format(
                    "rejected" if blocked else "flagged (warn mode)",
                    len(issues),
                    ", ".join(issue.path for issue in issues[:10])),
                "details": {
                    "policy_id": policy_id,
                    "blocked": blocked,
                    "issues": [issue.to_dict() for issue in issues],
                },
            },
            tenant_id=tenant_id,
        )
    except Exception:  # pragma: no cover - observability must not break the decision
        pass


def _fail_closed_on_variable_errors(
    storage: Any, tenant_id: str, decision: Dict[str, Any], variable_errors: Dict[str, str]
) -> None:
    """Fast-path twin of PolicyExecutor._fail_closed_on_variable_errors.

    Kept as a separate function rather than shared with the executor because the fast path has
    no ExecutionContext -- but the RULE it applies is identical, and the parity tests assert the
    two paths return the same outcome for the same failure. See the executor's docstring for why
    a failed variable cannot be allowed to inherit the rule author's phrasing.
    """
    if not variable_errors:
        return
    try:
        settings = storage.get_settings(tenant_id=tenant_id) or {}
        if (settings.get("engine_config") or {}).get("fail_open_on_variable_error"):
            return
    except Exception:  # pragma: no cover - a settings read must not decide the decision
        pass
    if decision["outcome"] == "review":
        return
    original = decision["outcome"]
    decision["outcome"] = "review"
    decision["trace"] = list(decision.get("trace") or []) + [{
        "type": "variable_error_guard",
        "status": "failed_closed",
        "outcome": "review",
        "original_outcome": original,
        "failed_variables": sorted(variable_errors),
        "detail": "Sent to review: {0} variable(s) could not be computed, and a null input "
                  "silently changes gate outcomes.".format(len(variable_errors)),
    }]


def _log_decision(storage: Any, tenant_id: str, decision: Dict[str, Any], payload: Dict[str, Any], values: Dict[str, Any], user_id: Optional[str] = None) -> None:
    record = {
        # The SAME id the caller was handed. It used to be minted here, so the response and the
        # row it describes could never be connected -- the fast path returned no id at all and
        # this one existed only in the database.
        "id": decision.get("decision_id") or str(uuid.uuid4()),
        "tenant_id": tenant_id,
        "policy_id": decision["policy_id"],
        "policy_version": decision.get("policy_version"),
        # Match the slow path exactly: carry the case id, and honour the tenant's
        # configured PII redaction keys. Passing no extra_keys here meant enabling
        # FAST_DECIDE silently dropped tenant-configured redaction.
        # NOT user_id. client_id is the device idempotency key under
        # UNIQUE(tenant_id, client_id); writing the subject here made a customer's second
        # decision collide with their first and never persist. Same bug the executor had.
        "client_id": (payload.get("client_id") or payload.get("clientId")) if isinstance(payload, dict) else None,
        "user_id": user_id,
        "payload": redact_payload(payload, extra_keys=storage.tenant_pii_redact_keys(tenant_id)),
        "computed_variables": values,
        "rule_results": decision["rule_results"],
        "scorecard_result": decision["scorecard_result"],
        "trace": decision["trace"],
        "outcome": decision["outcome"],
        "latency_ms": int(decision["latency_ms"]),
        "source": "api_fast",
        "created_at": now_iso(),
    }
    from . import decision_log

    # Durable-by-default when DECISION_WAL=1: WAL append (fsync) before the async DB write, so a
    # hard kill in that window is recovered by startup replay. Same async submit() otherwise.
    decision_log.persist_decision(storage, record, tenant_id)


def _safe_add_decision(storage: Any, record: Dict[str, Any], tenant_id: str) -> None:
    try:
        storage.add_decision(record, tenant_id=tenant_id)
    except Exception as exc:  # logging must never fail a decision — but must NOT be silent
        from . import decision_log

        decision_log.record_write_failure(
            storage, tenant_id, {"source": record.get("source"), "policy_id": record.get("policy_id")}, exc
        )
