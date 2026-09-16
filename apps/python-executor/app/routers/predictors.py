"""Predictors and their drift report (parity P20).

`GET /api/v1/predictors/{id}/drift` is the endpoint the row's e2eFlow is about. Everything else
here is the CRUD that has to exist for it to have something to report on.

RBAC: AUTHOR, unlike webhook subscriptions next door which are manage_access. The distinction is
deliberate and is about what the entity DOES rather than how important it feels. A subscription
sends this workspace's data to a third party; a predictor names a number that already exists and
records it in this workspace's own database. That is authoring, in the same class as a scorecard
or a variable -- and `/api/v1/predictors` must be listed in rbac's author prefixes, because the
unknown-write default is fail-closed and would otherwise make a policy_maker unable to author
one.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request
from pydantic import BaseModel

from .. import drift, main, predictors
from ..context import get_current_api_key_id
from ..main import active_role, active_tenant_id

router = APIRouter()

# A window bound, for the same reason every other author-supplied number here has one: the
# baseline query is indexed but unbounded windows still scan proportionally, and a decision-volume
# table makes "the last 3650 days" an expensive way to ask a cheap question.
MAX_WINDOW_DAYS = 400


class PredictorRequest(BaseModel):
    name: str
    source_type: str
    source_id: Optional[str] = None
    description: Optional[str] = None
    is_active: bool = True
    baseline_days: int = 30
    current_days: int = 7
    bins: int = 10


class PredictorPatch(BaseModel):
    name: Optional[str] = None
    source_type: Optional[str] = None
    source_id: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    baseline_days: Optional[int] = None
    current_days: Optional[int] = None
    bins: Optional[int] = None


def _audit(event_type: str, tenant_id: str, entity_id: str, detail: str) -> None:
    try:
        main.storage.add_audit_event(
            {"tenant_id": tenant_id, "event_type": event_type, "entity_type": "predictor",
             "entity_id": entity_id, "detail": detail,
             "metadata": {"actor_api_key_id": get_current_api_key_id(), "actor_role": active_role()}},
            tenant_id=tenant_id)
    except Exception:  # pragma: no cover - audit is best effort
        pass


def _validated(payload: Dict[str, Any]) -> Dict[str, Any]:
    if payload.get("source_type") is not None:
        try:
            predictors.validate(payload)
        except predictors.PredictorInvalid as invalid:
            raise HTTPException(status_code=422, detail=str(invalid)) from invalid
    for field, ceiling in (("baseline_days", MAX_WINDOW_DAYS), ("current_days", MAX_WINDOW_DAYS),
                           ("bins", 50)):
        if payload.get(field) is not None:
            value = int(payload[field])
            if value < 1 or value > ceiling:
                raise HTTPException(
                    status_code=422,
                    detail="{0} must be between 1 and {1}.".format(field, ceiling))
    return payload


@router.get("/api/v1/predictors/source-types")
def list_source_types() -> Dict[str, Any]:
    return {"source_types": list(predictors.SOURCE_TYPES)}


@router.get("/api/v1/predictors")
def list_predictors(request: Request) -> List[Dict[str, Any]]:
    return main.storage.list_predictors(tenant_id=active_tenant_id(request))


@router.post("/api/v1/predictors")
def create_predictor(body: PredictorRequest, request: Request) -> Dict[str, Any]:
    tenant_id = active_tenant_id(request)
    created = main.storage.create_predictor(_validated(body.model_dump()), tenant_id=tenant_id)
    _audit("predictor_created", tenant_id, created["id"],
           "Created predictor '{0}' over {1} {2}".format(
               body.name, body.source_type, body.source_id or ""))
    return created


@router.get("/api/v1/predictors/{predictor_id}")
def get_predictor(predictor_id: str, request: Request) -> Dict[str, Any]:
    found = main.storage.get_predictor(predictor_id, tenant_id=active_tenant_id(request))
    if not found:
        # 404, not 403, across the tenant boundary -- a 403 confirms the object exists.
        raise HTTPException(status_code=404, detail="Predictor not found.")
    return found


@router.patch("/api/v1/predictors/{predictor_id}")
def update_predictor(predictor_id: str, body: PredictorPatch, request: Request) -> Dict[str, Any]:
    tenant_id = active_tenant_id(request)
    patch = _validated({k: v for k, v in body.model_dump().items() if v is not None})
    updated = main.storage.update_predictor(predictor_id, patch, tenant_id=tenant_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Predictor not found.")
    _audit("predictor_updated", tenant_id, predictor_id,
           "Updated predictor '{0}'".format(updated.get("name") or predictor_id))
    return updated


@router.delete("/api/v1/predictors/{predictor_id}")
def delete_predictor(predictor_id: str, request: Request) -> Dict[str, Any]:
    tenant_id = active_tenant_id(request)
    if not main.storage.delete_predictor(predictor_id, tenant_id=tenant_id):
        raise HTTPException(status_code=404, detail="Predictor not found.")
    _audit("predictor_deleted", tenant_id, predictor_id,
           "Deleted predictor {0} and its recorded values".format(predictor_id))
    return {"deleted": True, "id": predictor_id}


class BackfillRequest(BaseModel):
    # Bounded per call. `predictors.MAX_BACKFILL_DECISIONS` caps it regardless of what is asked
    # for, so one request cannot turn into an unbounded scan plus an unbounded insert.
    limit: int = 1000


@router.post("/api/v1/predictors/{predictor_id}/backfill")
def backfill_predictor(predictor_id: str, request: Request,
                       # default_factory for the same reason as model-sets/compare: one shared
                       # instance per process is a latent cross-request leak the moment a field
                       # stops being immutable. `limit` is an int today; that is not a guarantee.
                       body: BackfillRequest = Body(default_factory=BackfillRequest)) -> Dict[str, Any]:
    """Replay historical decisions through this predictor (parity P12's "backfill it").

    `predictors.observe` runs on the decision path, so it is forward-only: a predictor created
    today has no history, and with the default 30-day baseline the drift report has nothing to
    compare against for a month -- on a feature whose entire job is comparison. Everything needed
    to fix that already existed: decisions store their computed_variables, scorecard_result and
    outcome, and `extract` already maps one decision to one value.

    Idempotent by decision id, so running it twice does not double the series, and each value is
    stamped with the DECISION's timestamp rather than now. See `predictors.backfill` for why that
    second point is the whole difficulty.
    """
    tenant_id = active_tenant_id(request)
    predictor = main.storage.get_predictor(predictor_id, tenant_id=tenant_id)
    if not predictor:
        raise HTTPException(status_code=404, detail="Predictor not found.")

    result = predictors.backfill(main.storage, predictor, tenant_id, limit=body.limit)
    _audit("predictor_backfilled", tenant_id, predictor_id,
           "Backfilled {0} values for predictor {1} from {2} historical decisions.".format(
               result["written"], predictor_id, result["scanned"]))
    return result


@router.get("/api/v1/predictors/{predictor_id}/drift")
def predictor_drift(predictor_id: str, request: Request,
                    baseline_days: Optional[int] = Query(default=None),
                    current_days: Optional[int] = Query(default=None)) -> Dict[str, Any]:
    """PSI of the recent window against the baseline window.

    The windows default to the predictor's OWN stored settings rather than to a constant, so two
    people asking "is it drifting?" get the same answer -- a drift number is only comparable
    against its own history when the windows are fixed. Overriding them per request is allowed
    for exploration, and the response echoes which were used so a screenshot is self-describing.

    The two windows are ADJACENT, not overlapping: the baseline ends where the current window
    begins. An overlapping baseline contains the very observations it is being compared against,
    which damps exactly the signal the report exists to show.
    """
    tenant_id = active_tenant_id(request)
    predictor = main.storage.get_predictor(predictor_id, tenant_id=tenant_id)
    if not predictor:
        raise HTTPException(status_code=404, detail="Predictor not found.")

    current_span = int(current_days or predictor.get("current_days") or 7)
    baseline_span = int(baseline_days or predictor.get("baseline_days") or 30)
    for label, value in (("current_days", current_span), ("baseline_days", baseline_span)):
        if value < 1 or value > MAX_WINDOW_DAYS:
            raise HTTPException(
                status_code=422,
                detail="{0} must be between 1 and {1}.".format(label, MAX_WINDOW_DAYS))

    now = datetime.utcnow()
    current_start = now - timedelta(days=current_span)
    baseline_start = current_start - timedelta(days=baseline_span)

    baseline = main.storage.predictor_values_between(
        tenant_id, predictor_id, baseline_start, current_start)
    current = main.storage.predictor_values_between(
        tenant_id, predictor_id, current_start, now)

    report = drift.psi(baseline, current, bins=int(predictor.get("bins") or 10))
    return {
        "predictor_id": predictor_id,
        "name": predictor.get("name"),
        "baseline_days": baseline_span,
        "current_days": current_span,
        "baseline_window": [baseline_start.isoformat() + "Z", current_start.isoformat() + "Z"],
        "current_window": [current_start.isoformat() + "Z", now.isoformat() + "Z"],
        **report,
    }
