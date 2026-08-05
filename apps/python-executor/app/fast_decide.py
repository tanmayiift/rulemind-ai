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
_SERVING: Dict[str, Dict[str, Any]] = {}


def is_fast_servable(policy: Dict[str, Any]) -> bool:
    steps = policy.get("steps", []) or []
    return not any(step.get("type") in _IO_STEP_TYPES for step in steps)


def invalidate(tenant_id: Optional[str] = None) -> None:
    """Drop cached bundles (all, or one tenant) — call on any publish/update."""
    with _CACHE_LOCK:
        if tenant_id is None:
            _SERVING.clear()
        else:
            for key in [k for k in _SERVING if k.startswith(f"{tenant_id}:")]:
                _SERVING.pop(key, None)


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
    with _CACHE_LOCK:
        cached = _SERVING.get(key)
    if cached is not None:
        return cached
    built = _build_serving_bundle(storage, tenant_id, policy)
    with _CACHE_LOCK:
        _SERVING[key] = built
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
