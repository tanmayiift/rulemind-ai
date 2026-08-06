"""Governance & change-management endpoints — data-protection posture, per-workspace SLOs,
decision replay, bundle versions, and policy backtesting. Extracted verbatim from app/main.py.

Shared app state is read live off the ``app.main`` module (``main.storage`` etc.) at call time —
see the package docstring for why this is required for the test harness's storage swapping. The
tenant is resolved from ``request.state`` (set by TenantContextMiddleware), which is the
request-scoped source of truth."""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from .. import main
from ..logic import now_iso

router = APIRouter()


# ── Data protection ────────────────────────────────────────────────────────
class DataProtectionRequest(BaseModel):
    retention_days: Optional[int] = None
    pii_redact_keys: Optional[List[str]] = None


def _data_protection_view(tenant_id: str) -> Dict[str, Any]:
    from ..logic import REDACTED_KEYS
    from ..storage import decision_encryption_enabled

    settings = main.storage.get_settings(tenant_id=tenant_id)
    custom = (settings.get("engine_config", {}) or {}).get("pii_redact_keys", []) or []
    env_keys = [k.strip() for k in (os.getenv("RULEMIND_PII_REDACT_KEYS", "") or "").split(",") if k.strip()]
    return {
        "retention_days": int(settings.get("audit_retention_days", 90) or 90),
        "encryption_at_rest": decision_encryption_enabled(),
        "archive_sink": (os.getenv("DECISION_ARCHIVE_SINK", "none") or "none"),
        "pii_redact_keys": custom,
        "builtin_redact_keys": sorted(REDACTED_KEYS),
        "env_redact_keys": env_keys,
    }


@router.get("/api/v1/settings/data-protection")
def get_data_protection(request: Request) -> Dict[str, Any]:
    """Per-workspace data-protection posture: decision-log retention window, at-rest encryption
    status, the OLAP archive sink, and the PII fields redacted from stored payloads."""
    return _data_protection_view(main.active_tenant_id(request))


@router.put("/api/v1/settings/data-protection")
def update_data_protection(request: Request, body: DataProtectionRequest) -> Dict[str, Any]:
    """Update the retention window and the workspace's custom PII redaction fields. Encryption at
    rest and the archive sink are deployment-level (env) and shown read-only."""
    tenant_id = main.active_tenant_id(request)
    settings = main.storage.get_settings(tenant_id=tenant_id)
    patch: Dict[str, Any] = {}
    if body.retention_days is not None:
        patch["audit_retention_days"] = max(1, int(body.retention_days))
    if body.pii_redact_keys is not None:
        engine = dict(settings.get("engine_config", {}) or {})
        engine["pii_redact_keys"] = [str(k).strip() for k in body.pii_redact_keys if str(k).strip()]
        patch["engine_config"] = engine
    if patch:
        main.storage.update_settings(patch, tenant_id=tenant_id)
    return _data_protection_view(tenant_id)


# ── Service-level objectives + drift ───────────────────────────────────────
class SloConfigRequest(BaseModel):
    enabled: Optional[bool] = None
    latency_p95_ms: Optional[float] = None
    error_rate_pct: Optional[float] = None
    drift_threshold: Optional[float] = None
    min_approval_rate_pct: Optional[float] = None
    max_approval_rate_pct: Optional[float] = None
    min_sample: Optional[int] = None
    recent_hours: Optional[int] = None
    baseline_days: Optional[int] = None


@router.get("/api/v1/settings/slo")
def get_slo_config(request: Request) -> Dict[str, Any]:
    """The workspace's effective SLO objective (stored overrides merged over platform defaults)."""
    from .. import slo
    return slo.tenant_slo_config(main.storage, tenant_id=main.active_tenant_id(request))


@router.put("/api/v1/settings/slo")
def update_slo_config(request: Request, body: SloConfigRequest) -> Dict[str, Any]:
    """Update this workspace's SLO objective — latency/error ceilings, optional approval-rate
    bounds, the outcome-drift limit, and the evaluation windows."""
    from .. import slo
    tenant_id = main.active_tenant_id(request)
    settings = main.storage.get_settings(tenant_id=tenant_id)
    engine = dict(settings.get("engine_config", {}) or {})
    stored = dict(engine.get("slo", {}) or {})
    for key, value in body.model_dump(exclude_unset=True).items():
        stored[key] = value
    engine["slo"] = stored
    main.storage.update_settings({"engine_config": engine}, tenant_id=tenant_id)
    return slo.tenant_slo_config(main.storage, tenant_id=tenant_id)


@router.get("/api/v1/slo/status")
def get_slo_status(request: Request) -> Dict[str, Any]:
    """Live SLO scorecard for the workspace — current-window metrics, outcome-drift vs the
    trailing baseline, active breaches, and the recent breach/recovery audit trail."""
    from .. import slo
    tenant_id = main.active_tenant_id(request)
    report = slo.evaluate_slo(main.storage, tenant_id=tenant_id)
    try:
        events = main.storage.list_audit_events(tenant_id=tenant_id, event_type="slo_breach")[:20]
    except Exception:
        events = []
    report["recent_events"] = [
        {"detail": e.get("detail"), "created_at": e.get("created_at"),
         "breach_types": (e.get("metadata", {}) or {}).get("breach_types", [])}
        for e in events
    ]
    return report


# ── Decision replay + bundle versions + policy backtest ────────────────────
@router.post("/api/v1/decisions/{decision_id}/replay")
def replay_decision(request: Request, decision_id: str, bundle_version: Optional[int] = Query(default=None, alias="bundleVersion")) -> Dict[str, Any]:
    """Re-run a past decision against the current policy — or a historical compiled bundle version
    (`?bundleVersion=N`) — and report whether the outcome would change. Answers "did my policy
    change flip decisions that already went out?" using the decision's stored inputs. Uses the
    recorded computed variables, so it isolates rule/policy-logic changes and is unaffected by
    payload redaction."""
    from ..core.engine import decide as core_decide

    tenant_id = main.active_tenant_id(request)
    decision = main.ensure_exists(main.storage.get_decision(decision_id, tenant_id=tenant_id), "decision", decision_id)
    if bundle_version is not None:
        bundle = main.storage.get_bundle(bundle_version, tenant_id=tenant_id)
        if not bundle:
            raise HTTPException(status_code=404, detail="Bundle version {0} not found.".format(bundle_version))
    else:
        bundle = main.storage.latest_bundle(tenant_id=tenant_id)
        if not bundle:
            raise HTTPException(status_code=404, detail="No compiled bundle to replay against.")
    replayed = core_decide(
        bundle["content"],
        decision.get("payload") or {},
        {
            "policy_id": decision.get("policy_id"),
            "variables": decision.get("computed_variables") or {},
            "strict_validation": False,
        },
    )
    original_outcome = decision.get("outcome")
    replayed_outcome = replayed.get("outcome")
    return {
        "decision_id": decision_id,
        "policy_id": decision.get("policy_id"),
        "bundle_version": bundle.get("version"),
        "original_outcome": original_outcome,
        "replayed_outcome": replayed_outcome,
        "changed": original_outcome != replayed_outcome,
        "replayed_at": now_iso(),
    }


@router.get("/api/v1/bundles/versions")
def list_bundle_versions(request: Request) -> List[Dict[str, Any]]:
    """The retained compiled bundle versions (for choosing a historical version to replay against)."""
    return [
        {"version": b.get("version"), "checksum": b.get("checksum"), "compiled_at": b.get("compiled_at"),
         "superseded": b.get("superseded")}
        for b in main.storage.list_bundles(main.active_tenant_id(request))
    ]


@router.post("/api/v1/policies/{policy_id}/backtest")
def backtest_policy_endpoint(
    request: Request,
    policy_id: str,
    bundle_version: Optional[int] = Query(default=None, alias="bundleVersion"),
    sample: int = Query(default=200, ge=1, le=2000),
) -> Dict[str, Any]:
    """Replay a sample of this policy's recent real decisions through a compiled bundle (latest,
    or `?bundleVersion=N`) and report the aggregate outcome impact — how many decisions would
    change and the full from→to transition matrix. The batch sibling of decision replay: answers
    "if I ship this, how much of my live traffic flips?" before you promote."""
    from .. import backtest as backtest_mod

    tenant_id = main.active_tenant_id(request)
    main.ensure_exists(main.storage.get_policy(policy_id, tenant_id=tenant_id), "policy", policy_id)
    try:
        return backtest_mod.backtest_policy(
            main.storage, policy_id, tenant_id=tenant_id,
            bundle_version=bundle_version, sample=sample,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
