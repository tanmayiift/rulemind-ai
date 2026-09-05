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

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, Optional, Tuple

from . import env, input_schema
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

try:  # optional native accelerator
    import rulemind_core_rs  # type: ignore

    _HAVE_RUST = True
except ImportError:  # pragma: no cover - Rust extension optional
    _HAVE_RUST = False

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
    policy_id = policy.get("id")
    for experiment in storage.list_experiments(tenant_id=tenant_id):
        if experiment.get("status") == "running" and experiment.get("target_policy_id") == policy_id:
            return False
    return not tenant_fetches_sources(storage, tenant_id)


def tenant_fetches_sources(storage: Any, tenant_id: str) -> bool:
    """Has this tenant switched decision-time source fetching on for any connector?

    If so the fast path must DECLINE the decision, exactly as it declines a policy containing an
    IO step: this module retrieves nothing, and `_compute_variables` below builds every source
    payload from `sample_payload`. Serving such a policy here would mean FAST_DECIDE=1 quietly
    reverting the fail-closed contract the executor now enforces — the connector would be
    configured to pull a real bureau report, and the decision would be made on the demo sample
    instead, at HTTP 200. That is a performance flag acting as a correctness switch, which is
    the precise shape of the bug `_IO_STEP_TYPES` was widened to fix.

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
# — the fast path computes the tenant's whole variable catalog with no COMPUTE_ONLY_USED_VARS
# filter, and the seeded demo tenant is 55 variables at the 2000ms per-variable timeout, i.e.
# 110 seconds of one Starlette threadpool thread for a single request.
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
    settings = storage.get_settings(tenant_id=tenant_id).get("engine_config", {})
    core_bundle = {"policy": policy, "rules": rules, "scorecards": scorecards,
                   "decision_tables": decision_tables, "variables": variables}

    rust_bundle = None
    # The Rust core covers rule/outcome policies only; scorecards and decision tables
    # stay on the Python core (which now serves both on the fast path).
    rules_only = not any(step.get("type") in ("scorecard", "decision_table") for step in policy.get("steps", []))
    if _HAVE_RUST and rules_only:
        try:
            rust_bundle = rulemind_core_rs.Bundle(json.dumps({"policy": policy, "rules": rules}))
        except Exception:  # pragma: no cover - defensive
            rust_bundle = None

    return {
        "policy": policy,
        "variables": variables,
        "connectors": connectors,
        "core_bundle": core_bundle,
        "rust_bundle": rust_bundle,
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
                       budget: Optional["_Budget"] = None) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, str]]:
    connectors = bundle["connectors"]
    payloads: Dict[str, Any] = {cid: dict(sample) for cid, sample in connectors.items()}
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
        for sample in payloads.values():
            if isinstance(sample, dict):
                for field, value in flat_fields.items():
                    if field in sample:
                        sample[field] = value
    payloads["custom"] = dict(flat_fields) if flat_fields else dict(payload)
    values: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    timeout_ms = bundle["timeout_ms"]
    for variable in bundle["variables"]:
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


def fast_decide(storage: Any, policy: Dict[str, Any], payload: Dict[str, Any], tenant_id: str, log: bool = True, user_id: Optional[str] = None) -> Dict[str, Any]:
    started = time.perf_counter()
    bundle = _serving_bundle(storage, tenant_id, policy)
    budget = _Budget(bundle["budget_ms"], started)
    # BEFORE any variable is computed, exactly like PolicyExecutor.execute. Validation that runs
    # after the work is not a guard, it is a report -- and the whole point is that the unchecked
    # keys were becoming None and quietly re-phrasing gates.
    validation_trace = _validate_input(storage, tenant_id, policy["id"], payload or {})
    values, resolved_payload, variable_errors = _compute_variables(bundle, payload or {}, budget)
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
    if bundle["rust_bundle"] is not None:
        outcome = bundle["rust_bundle"].decide(values)
        result = {
            "policy_id": policy["id"],
            "outcome": outcome,
            "score": None,
            "variables": values,
            "rule_results": [],
            "scorecard_result": None,
            "trace": [],
        }
    else:
        result = core_decide(bundle["core_bundle"], payload or {}, {"variables": values, "strict_validation": False})

    latency_ms = round((time.perf_counter() - started) * 1000, 3)
    # Warn-mode issues lead the trace, as they do on the slow path: the executor appends its
    # input_validation entry before any step runs, and an operator diffing the two traces must
    # not see the same decision described in a different order.
    trace = list(result.get("trace", []))
    if validation_trace is not None:
        trace.insert(0, validation_trace)
    decision = {
        "policy_id": policy["id"],
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
        "id": str(uuid.uuid4()),
        "tenant_id": tenant_id,
        "policy_id": decision["policy_id"],
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
