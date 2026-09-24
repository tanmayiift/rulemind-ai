"""Insights & audit endpoints — decision/latency/SDK analytics, experiment results, rejection
drivers, and the audit trail (decisions, promotions, errors). Extracted verbatim from app/main.py.

Shared app state is read live off the ``app.main`` module at call time (see the routers package
docstring). Tenant is resolved from ``request.state`` via ``main.active_tenant_id(request)``."""
from __future__ import annotations

from typing import Annotated, Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from .. import main
from ..api_models import DecisionRecord
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
def analytics_rejection_drivers(request: Request, body: RejectionDriversRequest) -> Dict[str, Any]:
    """Which predictor/condition drives the most rejections — pure compute over the
    decision log (no LLM, no key). Optionally scoped to one policy.

    Tenant passed explicitly. See `audit_events` below: without it this resolved through
    `Storage._tenant_id`'s fallback chain to `default_tenant_id`, the FIRST tenant in the
    table — so on a multi-tenant install this analytic was computed over someone else's
    decisions and returned as the caller's."""
    from ..analytics import rejection_drivers

    decisions = main.storage.list_decisions(tenant_id=main.active_tenant_id(request),
                                            limit=max(1, min(body.limit, 1000)))
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
def audit_events(request: Request,
                 limit: Annotated[int, Query(ge=1, le=1000)] = 200,
                 offset: Annotated[int, Query(ge=0)] = 0,
                 event_type: Optional[str] = None, entity_type: Optional[str] = None,
                 entity_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """The authoring audit trail: who changed which rule/variable/scorecard/policy, when.

    This route did not exist. Events were written -- and after the entity-CRUD wiring, written
    for every authoring entity -- but nothing read them back over HTTP, so the compliance
    artifact this product exists to produce could only be reached by opening the database. An
    auditor asking "show me every change to this rule" had no answer.

    limit/offset use the Annotated form so the PYTHON default is a real int. Written as
    `limit: int = Query(default=200, ...)` the default value is a `Query` object, which FastAPI
    unwraps but a direct call does not -- so any test or internal caller invoking the handler as
    a plain function got a `Query` where an int was promised and failed inside SQLAlchemy with
    `'<' not supported between instances of 'int' and 'Query'`. Same validation, no trap.

    limit/offset are BOUNDED HERE, not in storage. `list_audit_events` deliberately applies them
    raw -- its internal callers pass limit=None to mean "every matching event" -- so this route is
    the only place that can reject a nonsense page. Untyped, `?limit=-1` reached Postgres as
    `LIMIT -1` and answered 500 (`InvalidRowCountInLimitClause`) on the one endpoint an auditor's
    tooling drives; on SQLite the same request was quieter and worse, because `LIMIT -1` means NO
    limit there and the unbounded fetch this signature exists to prevent came back silently.

    Tenant is passed explicitly rather than relying on the storage layer's fallback chain
    (`tenant_id or contextvar or default_tenant_id`). On an authenticated request the contextvar
    is set by the middleware and resolves correctly either way -- but the last link in that
    chain is the FIRST tenant, and an audit read reaching it would serve one tenant's change
    history to another. Cheap to not depend on.
    """
    return main.storage.list_audit_events(
        tenant_id=main.active_tenant_id(request), event_type=event_type,
        entity_type=entity_type, entity_id=entity_id, limit=limit, offset=offset)


@router.get("/api/v1/audit/decisions", responses={200: {"model": List[DecisionRecord]}})
def audit_decisions(request: Request,
                    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
                    offset: Annotated[int, Query(ge=0)] = 0,
                    client_id: Optional[str] = None,
                    policy_id: Optional[str] = None, outcome: Optional[str] = None,
                    user_id: Optional[str] = None,
                    created_from: Optional[str] = None,
                    created_to: Optional[str] = None) -> List[Dict[str, Any]]:
    """Decision history, filterable by case, subject, policy, outcome AND DATE.

    `created_from` / `created_to` are ISO-8601 and half-open, [from, to). Without them "show me
    everything in Q1" was not expressible at any layer: the only ordering is `created_at DESC`,
    so an eight-month-old decision was reachable only by paging every row written since. The
    Decision Explorer did not even do that — it fetched the default 200 and filtered in the
    browser, so the screen a regulator question lands on could see the most recent 200 decisions
    and nothing older.

    Tenant is passed explicitly, for the reason spelled out on `audit_events` above: the storage
    fallback chain ends at the FIRST tenant, and a decision read reaching it would serve one
    customer's decisions to another. This route and `audit_errors` below were the two immediate
    neighbours of that comment and neither passed it.

    limit/offset are bounded here rather than in storage, matching `audit_events` — untyped,
    `?limit=-1` reaches Postgres as `LIMIT -1` and 500s, and on SQLite means NO limit, which is
    the unbounded fetch pagination exists to prevent.
    """
    return main.storage.list_decisions(tenant_id=main.active_tenant_id(request),
                                       limit=limit, offset=offset, client_id=client_id,
                                       policy_id=policy_id, outcome=outcome, user_id=user_id,
                                       created_from=created_from, created_to=created_to)


@router.get("/api/v1/audit/decisions/count")
def audit_decisions_count(request: Request) -> Dict[str, int]:
    """Tenant passed explicitly — the count the Decision Explorer paginates against. Without
    one it counted the FIRST tenant's decisions, so a tenant with no traffic could be shown a
    non-zero total (and one with traffic, the wrong one)."""
    return {"total": main.storage.count_decisions(tenant_id=main.active_tenant_id(request))}


@router.get("/api/v1/audit/decisions/{decision_id}", responses={200: {"model": DecisionRecord}})
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
def audit_promotions(request: Request) -> List[Dict[str, Any]]:
    """Tenant passed explicitly. The promotion ledger is the maker/checker record — who moved
    what to prod — so a fallback to the first tenant here shows one customer's release history
    to another."""
    return main.storage.list_promotions(tenant_id=main.active_tenant_id(request))


@router.get("/api/v1/audit/errors")
def audit_errors(request: Request,
                 limit: Annotated[int, Query(ge=1, le=1000)] = 200,
                 offset: Annotated[int, Query(ge=0)] = 0) -> List[Dict[str, Any]]:
    """Tenant passed explicitly — see `audit_events` above. This route sat directly beneath a
    comment explaining that the storage fallback chain ends at the FIRST tenant, and did not
    pass one."""
    return main.storage.list_error_events(tenant_id=main.active_tenant_id(request),
                                          limit=limit, offset=offset)
