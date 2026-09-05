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
@router.get("/api/v1/audit")
def audit_events(request: Request, limit: int = 200, offset: int = 0,
                 event_type: Optional[str] = None, entity_type: Optional[str] = None,
                 entity_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """The authoring audit trail: who changed which rule/variable/scorecard/policy, when.

    This route did not exist. Events were written -- and after the entity-CRUD wiring, written
    for every authoring entity -- but nothing read them back over HTTP, so the compliance
    artifact this product exists to produce could only be reached by opening the database. An
    auditor asking "show me every change to this rule" had no answer.

    Tenant is passed explicitly rather than relying on the storage layer's fallback chain
    (`tenant_id or contextvar or default_tenant_id`). On an authenticated request the contextvar
    is set by the middleware and resolves correctly either way -- but the last link in that
    chain is the FIRST tenant, and an audit read reaching it would serve one tenant's change
    history to another. Cheap to not depend on.
    """
    return main.storage.list_audit_events(
        tenant_id=main.active_tenant_id(request), event_type=event_type,
        entity_type=entity_type, entity_id=entity_id, limit=limit, offset=offset)


@router.get("/api/v1/audit/decisions")
def audit_decisions(limit: int = 200, offset: int = 0, client_id: Optional[str] = None,
                    policy_id: Optional[str] = None, outcome: Optional[str] = None,
                    user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    # Paginated — the decisions table is unbounded in production; an unpaged fetch
    # of tens of thousands of full-context rows would time out and exhaust memory.
    # client_id/policy_id/outcome filter in SQL so "show me this customer's history"
    # is one query rather than a full-table page-and-scan by the caller.
    # user_id is the SUBJECT filter -- "this customer's history". client_id stays the device
    # idempotency key, which is what an SDK-sync caller looks a decision up by.
    return main.storage.list_decisions(limit=limit, offset=offset, client_id=client_id,
                                       policy_id=policy_id, outcome=outcome, user_id=user_id)


@router.get("/api/v1/audit/decisions/count")
def audit_decisions_count() -> Dict[str, int]:
    return {"total": main.storage.count_decisions()}


@router.get("/api/v1/audit/decisions/{decision_id}")
def audit_decision(decision_id: str, request: Request) -> Dict[str, Any]:
    """One decision by id.

    Declared AFTER /count deliberately: FastAPI matches in declaration order, so a
    `{decision_id}` route above it would swallow `/count` and answer it with a 404 for a
    decision literally named "count".

    Added for the step-through debugger, whose entry point from the Decision Explorer is
    "replay this execution" -- fetching the whole page of decisions to find one by id works
    only until a workspace has more than a page of them.
    """
    decision = main.storage.get_decision(decision_id, tenant_id=main.active_tenant_id(request))
    if not decision:
        raise HTTPException(status_code=404, detail="Decision not found.")
    return decision


@router.get("/api/v1/audit/promotions")
def audit_promotions() -> List[Dict[str, Any]]:
    return main.storage.list_promotions()


@router.get("/api/v1/audit/errors")
def audit_errors(limit: int = 200, offset: int = 0) -> List[Dict[str, Any]]:
    return main.storage.list_error_events(limit=limit, offset=offset)
