"""Insights & audit endpoints — decision/latency/SDK analytics, experiment results, rejection
drivers, and the audit trail (decisions, promotions, errors). Extracted verbatim from app/main.py.

Shared app state is read live off the ``app.main`` module at call time (see the routers package
docstring). Tenant is resolved from ``request.state`` via ``main.active_tenant_id(request)``."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import main
from ..analytics import (
    decision_analytics,
    experiment_analytics,
    latency_analytics,
    sdk_analytics,
)

router = APIRouter()


class RejectionDriversRequest(BaseModel):
    policy_id: Optional[str] = None
    limit: int = 500


# ── Analytics ──────────────────────────────────────────────────────────────
@router.post("/api/v1/analytics/rejection-drivers")
def analytics_rejection_drivers(body: RejectionDriversRequest) -> Dict[str, Any]:
    """Which predictor/condition drives the most rejections — pure compute over the
    decision log (no LLM, no key). Optionally scoped to one policy."""
    from ..analytics import rejection_drivers

    decisions = main.storage.list_decisions(limit=max(1, min(body.limit, 1000)))
    if body.policy_id:
        decisions = [d for d in decisions if d.get("policy_id") == body.policy_id]
    return rejection_drivers(decisions)


@router.get("/api/v1/experiments/{experiment_id}/results")
@router.get("/api/v1/analytics/experiments/{experiment_id}")
def experiment_results(request: Request, experiment_id: str) -> Dict[str, Any]:
    try:
        return experiment_analytics(main.storage, main.active_tenant_id(request), experiment_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/api/v1/analytics/decisions")
def analytics_decisions(request: Request) -> Dict[str, Any]:
    return decision_analytics(main.storage, main.active_tenant_id(request))


@router.get("/api/v1/analytics/latency")
def analytics_latency(request: Request) -> Dict[str, Any]:
    return latency_analytics(main.storage, main.active_tenant_id(request))


@router.get("/api/v1/analytics/sdk")
def analytics_sdk(request: Request) -> Dict[str, Any]:
    return sdk_analytics(main.storage, main.active_tenant_id(request))


# ── Audit trail ────────────────────────────────────────────────────────────
@router.get("/api/v1/audit/decisions")
def audit_decisions(limit: int = 200, offset: int = 0) -> List[Dict[str, Any]]:
    # Paginated — the decisions table is unbounded in production; an unpaged fetch
    # of tens of thousands of full-context rows would time out and exhaust memory.
    return main.storage.list_decisions(limit=limit, offset=offset)


@router.get("/api/v1/audit/decisions/count")
def audit_decisions_count() -> Dict[str, int]:
    return {"total": main.storage.count_decisions()}


@router.get("/api/v1/audit/promotions")
def audit_promotions() -> List[Dict[str, Any]]:
    return main.storage.list_promotions()


@router.get("/api/v1/audit/errors")
def audit_errors(limit: int = 200, offset: int = 0) -> List[Dict[str, Any]]:
    return main.storage.list_error_events(limit=limit, offset=offset)
