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
from typing import Any, Dict, Optional, Tuple

from .core import decide as core_decide
from .logic import now_iso, redact_payload
from .sandbox import execute_variable

try:  # optional native accelerator
    import rulemind_core_rs  # type: ignore

    _HAVE_RUST = True
except ImportError:  # pragma: no cover - Rust extension optional
    _HAVE_RUST = False

# step types that need live I/O or human interaction -> not fast-servable
_IO_STEP_TYPES = {"action", "review_gate", "transform", "model"}

_CACHE_LOCK = threading.Lock()
# key -> (epoch_built_at, bundle). The epoch stamp lets a replica that never received the
# in-process invalidate() call still detect a stale bundle and rebuild — see _current_epoch.
_SERVING: Dict[str, Tuple[int, Dict[str, Any]]] = {}

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
_EPOCH_TTL = float(os.getenv("FAST_CACHE_EPOCH_TTL", "1.0"))
_LOCAL_EPOCH: Dict[str, int] = {}
_EPOCH_CACHE: Dict[str, Tuple[float, int]] = {}  # tenant_id -> (fetched_at_monotonic, epoch)


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
    return True


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
            _EPOCH_CACHE.clear()
            _LOCAL_EPOCH.clear()
            return
        for key in [k for k in _SERVING if k.startswith(f"{tenant_id}:")]:
            _SERVING.pop(key, None)
        _LOCAL_EPOCH[tenant_id] = _LOCAL_EPOCH.get(tenant_id, 0) + 1
        # Force this process to re-read the epoch next time (don't serve from its TTL cache).
        _EPOCH_CACHE.pop(tenant_id, None)
    client = _redis()
    if client is not None:
        try:
            client.incr(_EPOCH_KEY.format(tenant_id))
        except Exception:  # pragma: no cover - best-effort; local clear already happened
            pass


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
    }


def _serving_bundle(storage: Any, tenant_id: str, policy: Dict[str, Any]) -> Dict[str, Any]:
    key = f"{tenant_id}:{policy['id']}"
    epoch = _current_epoch(tenant_id)
    with _CACHE_LOCK:
        cached = _SERVING.get(key)
    if cached is not None and cached[0] == epoch:
        return cached[1]
    built = _build_serving_bundle(storage, tenant_id, policy)
    with _CACHE_LOCK:
        _SERVING[key] = (epoch, built)
    return built


def _compute_variables(bundle: Dict[str, Any], payload: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
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
        for cid, sample in payloads.items():
            if isinstance(sample, dict):
                for field, value in flat_fields.items():
                    if field in sample:
                        sample[field] = value
    payloads["custom"] = dict(flat_fields) if flat_fields else dict(payload)
    values: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    for variable in bundle["variables"]:
        source_payload = payloads.get(variable.get("source_id"), {})
        execution = execute_variable(
            variable["code"], source_payload, values,
            timeout_ms=bundle["timeout_ms"], memory_mb=bundle["memory_mb"],
        )
        values[variable["id"]] = execution.get("value")
        # A variable that errors becomes None here; surface the error to the caller so it is
        # never silently swallowed (a None fed to a gate can flip a decision).
        if execution.get("error"):
            errors[variable["id"]] = str(execution.get("error"))
    return values, payloads, errors


def fast_decide(storage: Any, policy: Dict[str, Any], payload: Dict[str, Any], tenant_id: str, log: bool = True) -> Dict[str, Any]:
    started = time.perf_counter()
    bundle = _serving_bundle(storage, tenant_id, policy)
    values, resolved_payload, variable_errors = _compute_variables(bundle, payload or {})
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
    decision = {
        "policy_id": policy["id"],
        "outcome": result["outcome"] if result["outcome"] != "pending" else policy.get("defaultOutcome", "review"),
        "score": result.get("score"),
        "variables": values,
        "rule_results": result.get("rule_results", []),
        "scorecard_result": result.get("scorecard_result"),
        "trace": result.get("trace", []),
        "latency_ms": latency_ms,
    }
    if log:
        _log_decision(storage, tenant_id, decision, resolved_payload, values)
    return decision


def _log_decision(storage: Any, tenant_id: str, decision: Dict[str, Any], payload: Dict[str, Any], values: Dict[str, Any]) -> None:
    record = {
        "id": str(uuid.uuid4()),
        "tenant_id": tenant_id,
        "policy_id": decision["policy_id"],
        "payload": redact_payload(payload),
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

    decision_log.submit(_safe_add_decision, storage, record, tenant_id)


def _safe_add_decision(storage: Any, record: Dict[str, Any], tenant_id: str) -> None:
    try:
        storage.add_decision(record, tenant_id=tenant_id)
    except Exception:  # pragma: no cover - logging must never fail a decision
        pass
