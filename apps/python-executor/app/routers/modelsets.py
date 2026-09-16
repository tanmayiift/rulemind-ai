"""Model sets: CRUD, drift, and side-by-side comparison (parity P05).

`POST /api/v1/model-sets/{id}/compare` is what the row's e2eFlow is about -- run every member on
the SAME input and return the predictions together. Without it, comparing two candidate models
means wiring two policies and diffing decision logs, which is enough friction that nobody does it.

RBAC is AUTHOR, like models and every other authoring entity. A set names models that already
exist in this workspace and routes between them; it grants no reach the author did not have.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel

from .. import main, modelsets
from ..context import get_current_api_key_id
from ..main import active_role, active_tenant_id

router = APIRouter()


class ModelSetRequest(BaseModel):
    name: str
    members: List[Dict[str, Any]]
    id: Optional[str] = None
    description: Optional[str] = None
    is_active: bool = True


class ModelSetPatch(BaseModel):
    name: Optional[str] = None
    members: Optional[List[Dict[str, Any]]] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class CompareRequest(BaseModel):
    features: Dict[str, Any] = {}


def _audit(event_type: str, tenant_id: str, entity_id: str, detail: str) -> None:
    try:
        main.storage.add_audit_event(
            {"tenant_id": tenant_id, "event_type": event_type, "entity_type": "model_set",
             "entity_id": entity_id, "detail": detail,
             "metadata": {"actor_api_key_id": get_current_api_key_id(), "actor_role": active_role()}},
            tenant_id=tenant_id)
    except Exception:  # pragma: no cover - audit is best effort
        pass


def _validated(payload: Dict[str, Any], tenant_id: str) -> Dict[str, Any]:
    if payload.get("members") is not None:
        try:
            members = modelsets.validate_members(payload["members"])
            # Pinned at WRITE time: a set that silently contains a non-existent model routes to an
            # error at decision time, which is the worst place to find out.
            payload["members"] = modelsets.pin_members(members, main.storage, tenant_id)
        except modelsets.ModelSetInvalid as invalid:
            raise HTTPException(status_code=422, detail=str(invalid)) from invalid
    return payload


@router.get("/api/v1/model-sets")
def list_model_sets(request: Request) -> List[Dict[str, Any]]:
    return main.storage.list_model_sets(tenant_id=active_tenant_id(request))


@router.post("/api/v1/model-sets")
def create_model_set(body: ModelSetRequest, request: Request) -> Dict[str, Any]:
    tenant_id = active_tenant_id(request)
    created = main.storage.create_model_set(
        _validated(body.model_dump(), tenant_id), tenant_id=tenant_id)
    _audit("model_set_created", tenant_id, created["id"],
           "Created model set '{0}' with {1} member(s)".format(body.name, len(body.members)))
    return created


@router.get("/api/v1/model-sets/{set_id}")
def get_model_set(set_id: str, request: Request) -> Dict[str, Any]:
    found = main.storage.get_model_set(set_id, tenant_id=active_tenant_id(request))
    if not found:
        raise HTTPException(status_code=404, detail="Model set not found.")
    return found


@router.patch("/api/v1/model-sets/{set_id}")
def update_model_set(set_id: str, body: ModelSetPatch, request: Request) -> Dict[str, Any]:
    tenant_id = active_tenant_id(request)
    patch = _validated({k: v for k, v in body.model_dump().items() if v is not None}, tenant_id)
    updated = main.storage.update_model_set(set_id, patch, tenant_id=tenant_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Model set not found.")
    _audit("model_set_updated", tenant_id, set_id,
           "Updated model set '{0}'".format(updated.get("name") or set_id))
    return updated


@router.delete("/api/v1/model-sets/{set_id}")
def delete_model_set(set_id: str, request: Request) -> Dict[str, Any]:
    tenant_id = active_tenant_id(request)
    if not main.storage.delete_model_set(set_id, tenant_id=tenant_id):
        raise HTTPException(status_code=404, detail="Model set not found.")
    _audit("model_set_deleted", tenant_id, set_id, "Deleted model set {0}".format(set_id))
    return {"deleted": True, "id": set_id}


@router.get("/api/v1/model-sets/{set_id}/drift")
def model_set_drift(set_id: str, request: Request) -> Dict[str, Any]:
    """Which members have moved since they were pinned.

    Reported, never enforced. Retraining a model is a legitimate thing to do, and blocking a
    decision because of it would make this feature a liability -- what the set owes the operator
    is that the change is VISIBLE, not that it is prevented.
    """
    tenant_id = active_tenant_id(request)
    model_set = main.storage.get_model_set(set_id, tenant_id=tenant_id)
    if not model_set:
        raise HTTPException(status_code=404, detail="Model set not found.")
    drifted = modelsets.member_drift(model_set.get("members") or [], main.storage, tenant_id)
    return {"id": set_id, "drifted": drifted, "in_sync": not drifted}


@router.post("/api/v1/model-sets/{set_id}/compare")
def compare_model_set(set_id: str, request: Request,
                      # default_factory, not default=CompareRequest(): a bare default builds ONE
                      # instance at import and shares it across every request that omits a body.
                      # CompareRequest.features is a mutable dict, so anything that mutated it
                      # would leak into later requests. A factory hands each request its own.
                      body: CompareRequest = Body(default_factory=CompareRequest)) -> Dict[str, Any]:
    """Run EVERY member on the same input and return the predictions side by side.

    Routing is deliberately bypassed: the point of a comparison is to see what each candidate
    would have said, including the ones this input would not have routed to. The response still
    reports which member routing WOULD have chosen, so the two readings are available together.

    One member failing does not fail the comparison -- a candidate that errors is exactly what an
    operator is looking for, and hiding the others behind it would defeat the endpoint.
    """
    from ..model_executor import execute_model

    tenant_id = active_tenant_id(request)
    model_set = main.storage.get_model_set(set_id, tenant_id=tenant_id)
    if not model_set:
        raise HTTPException(status_code=404, detail="Model set not found.")

    routed, reason = modelsets.resolve_member(model_set, {"features": body.features, **body.features})
    results = []
    for member in model_set.get("members") or []:
        model = main.storage.get_model(member["model_id"], include_blob=True, tenant_id=tenant_id)
        if not model:
            results.append({"label": member["label"], "model_id": member["model_id"],
                            "error": "model no longer exists"})
            continue
        try:
            prediction = execute_model(model["model_blob"], body.features)
        except Exception as exc:  # noqa: BLE001 - a failing candidate is a RESULT, not an outage
            results.append({"label": member["label"], "model_id": member["model_id"],
                            "error": str(exc)[:300]})
            continue
        results.append({
            "label": member["label"],
            "model_id": member["model_id"],
            "pinned_version": member.get("pinned_version"),
            "current_version": model.get("version"),
            "result": prediction,
        })
    return {
        "id": set_id,
        "routed_to": routed["label"] if routed else None,
        "routing_reason": reason,
        "results": results,
    }
