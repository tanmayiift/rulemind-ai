"""Journeys — the screen flow that collects a payload and runs a policy (parity P15).

WHAT ALREADY WORKED, and why the row's estimate was wrong. `app/experience_studio.py` has carried
the journey shape since the mobile studio shipped -- id, title, category, policyId, and an ordered
list of typed screens -- `/sdk/v1/experience-manifest` serves it, and
`packages/sdk-flutter/example` renders and RUNS it against the named policy. The parity row said a
journey "exists only as fixtures in three places that do not share a definition, so there is not
even one shape to build a table from", and estimated 3-6 months. There was one shape, it was
served, and "run it from an SDK" -- one of the row's four e2e clauses -- already worked.

WHAT DID NOT EXIST is a per-tenant record. `build_experience_manifest` returned a deep copy of one
static template, so every workspace saw the same three demo journeys and none could author its
own. This router is the missing half: create, lock, duplicate, and have the manifest serve what
the workspace authored.

LOCK IS NOT THE LIFECYCLE. `status` is the dev/uat/prod promotion every authoring entity has.
`is_locked` means "this is in the field; the screens must not move under the people filling them
in". So a locked journey still promotes, still duplicates -- duplication is the intended way to
change one -- but refuses content edits. Conflating the two would mean either that a prod journey
can be rewritten under live respondents, or that a draft cannot be edited.
"""
from __future__ import annotations

import copy
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import main
from ..context import get_current_api_key_id
from ..logic import slugify
from ..main import active_role, active_tenant_id

router = APIRouter()

MAX_SCREENS = 60
MAX_FIELDS_PER_SCREEN = 60


class JourneyRequest(BaseModel):
    title: str
    category: Optional[str] = None
    description: Optional[str] = None
    policy_id: Optional[str] = None
    primary_color: Optional[str] = None
    screens: List[Dict[str, Any]] = []
    is_active: bool = True
    status: str = "dev"


class JourneyPatch(BaseModel):
    title: Optional[str] = None
    category: Optional[str] = None
    description: Optional[str] = None
    policy_id: Optional[str] = None
    primary_color: Optional[str] = None
    screens: Optional[List[Dict[str, Any]]] = None
    is_active: Optional[bool] = None
    status: Optional[str] = None


class DuplicateRequest(BaseModel):
    title: Optional[str] = None


def _audit(event_type: str, tenant_id: str, journey_id: str, detail: str,
           extra: Optional[Dict[str, Any]] = None) -> None:
    """The actor, which `_audit_entity_change` does not carry.

    Storage already writes `journey_created` / `journey_updated` for the CRUD pair, but with only
    a version in its metadata. Lock and duplicate are the two operations where WHO matters: a lock
    is someone declaring a flow live, and a duplicate is where a second copy of a journey in the
    field comes from.
    """
    metadata = {"actor_api_key_id": get_current_api_key_id(), "actor_role": active_role()}
    metadata.update(extra or {})
    try:
        main.storage.add_audit_event(
            {"tenant_id": tenant_id, "event_type": event_type, "entity_type": "journey",
             "entity_id": journey_id, "detail": detail, "metadata": metadata},
            tenant_id=tenant_id)
    except Exception:  # pragma: no cover - audit is best effort
        pass


def _validate(screens: List[Dict[str, Any]]) -> None:
    """Bounds, and that a screen is a screen.

    Unbounded `screens` is a JSON column an authenticated author can grow without limit; every
    read of the manifest then carries it to every device. The caps are generous enough that no
    real flow meets them and small enough that the column cannot become a payload.
    """
    if len(screens) > MAX_SCREENS:
        raise HTTPException(status_code=422,
                            detail="A journey may have at most {0} screens.".format(MAX_SCREENS))
    seen: set = set()
    for index, screen in enumerate(screens):
        if not isinstance(screen, dict):
            raise HTTPException(status_code=422,
                                detail="Screen {0} is not an object.".format(index + 1))
        screen_id = str(screen.get("id") or "").strip()
        if not screen_id:
            raise HTTPException(status_code=422,
                                detail="Screen {0} has no id.".format(index + 1))
        if screen_id in seen:
            # Duplicate ids would make "which screen did they answer" ambiguous in the manifest
            # the device renders, and the SDK looks screens up by id.
            raise HTTPException(status_code=422,
                                detail="Duplicate screen id '{0}'.".format(screen_id))
        seen.add(screen_id)
        fields = screen.get("fields")
        if fields is not None and (not isinstance(fields, list) or len(fields) > MAX_FIELDS_PER_SCREEN):
            raise HTTPException(
                status_code=422,
                detail="Screen '{0}' must have a list of at most {1} fields.".format(
                    screen_id, MAX_FIELDS_PER_SCREEN))


def _journey_or_404(journey_id: str, tenant_id: str) -> Dict[str, Any]:
    journey = main.storage.get_journey(journey_id, tenant_id=tenant_id)
    if not journey:
        raise HTTPException(status_code=404, detail="Journey not found.")
    return journey


def _unique_id(base: str, tenant_id: str) -> str:
    candidate = slugify(base) or "journey"
    existing = {j["id"] for j in main.storage.list_journeys(tenant_id=tenant_id)}
    if candidate not in existing:
        return candidate
    return "{0}_{1}".format(candidate, uuid.uuid4().hex[:6])


@router.get("/api/v1/journeys")
def list_journeys(request: Request) -> List[Dict[str, Any]]:
    return main.storage.list_journeys(tenant_id=active_tenant_id(request))


@router.get("/api/v1/journeys/{journey_id}")
def get_journey(journey_id: str, request: Request) -> Dict[str, Any]:
    return _journey_or_404(journey_id, active_tenant_id(request))


@router.post("/api/v1/journeys")
def create_journey(body: JourneyRequest, request: Request) -> Dict[str, Any]:
    tenant_id = active_tenant_id(request)
    _validate(body.screens)
    payload = body.model_dump()
    payload["id"] = _unique_id(body.title, tenant_id)
    return main.storage.create_journey(payload, tenant_id=tenant_id)


@router.put("/api/v1/journeys/{journey_id}")
def update_journey(journey_id: str, body: JourneyPatch, request: Request) -> Dict[str, Any]:
    tenant_id = active_tenant_id(request)
    journey = _journey_or_404(journey_id, tenant_id)

    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    content_keys = {"title", "category", "description", "policy_id", "primary_color", "screens"}
    if journey["is_locked"] and content_keys & set(patch):
        # 409, not 403: the caller has the right to edit journeys, this journey is in a state that
        # refuses the edit. The message says the way forward, because "locked" with no exit is a
        # dead end -- and duplicating is the intended way to change a live flow.
        raise HTTPException(
            status_code=409,
            detail="This journey is locked because it is in the field. Duplicate it to make "
                   "changes, or unlock it first if nobody is filling it in.")
    if "screens" in patch:
        _validate(patch["screens"])
    return main.storage.update_journey(journey_id, patch, tenant_id=tenant_id)


@router.delete("/api/v1/journeys/{journey_id}")
def delete_journey(journey_id: str, request: Request) -> Dict[str, Any]:
    tenant_id = active_tenant_id(request)
    journey = _journey_or_404(journey_id, tenant_id)
    if journey["is_locked"]:
        raise HTTPException(
            status_code=409,
            detail="This journey is locked because it is in the field. Unlock it before deleting.")
    main.storage.delete_journey(journey_id, tenant_id=tenant_id)
    return {"deleted": True, "id": journey_id}


@router.post("/api/v1/journeys/{journey_id}/lock")
def lock_journey(journey_id: str, request: Request) -> Dict[str, Any]:
    tenant_id = active_tenant_id(request)
    _journey_or_404(journey_id, tenant_id)
    updated = main.storage.update_journey(journey_id, {"is_locked": True}, tenant_id=tenant_id)
    _audit("journey_locked", tenant_id, journey_id,
           "Journey {0} locked at version {1}.".format(journey_id, updated["version"]),
           {"version": updated["version"]})
    return updated


@router.post("/api/v1/journeys/{journey_id}/unlock")
def unlock_journey(journey_id: str, request: Request) -> Dict[str, Any]:
    tenant_id = active_tenant_id(request)
    _journey_or_404(journey_id, tenant_id)
    updated = main.storage.update_journey(journey_id, {"is_locked": False}, tenant_id=tenant_id)
    _audit("journey_unlocked", tenant_id, journey_id,
           "Journey {0} unlocked.".format(journey_id), {"version": updated["version"]})
    return updated


@router.post("/api/v1/journeys/{journey_id}/duplicate")
def duplicate_journey(journey_id: str, body: DuplicateRequest, request: Request) -> Dict[str, Any]:
    """Copy a journey, unlocked and back in dev.

    The copy deliberately does NOT inherit `is_locked` or a prod `status`. Duplication exists to
    make a live flow changeable; a copy that arrived locked, or already in prod, would defeat both
    halves of that in one step -- the second more quietly, since it would put an unreviewed journey
    in front of real people.
    """
    tenant_id = active_tenant_id(request)
    source = _journey_or_404(journey_id, tenant_id)

    title = (body.title or "").strip() or "{0} (copy)".format(source["title"])
    payload = {
        "id": _unique_id(title, tenant_id),
        "title": title,
        "category": source["category"],
        "description": source["description"],
        "policy_id": source["policy_id"],
        "primary_color": source["primary_color"],
        "screens": copy.deepcopy(source["screens"]),
        "is_locked": False,
        "is_active": source["is_active"],
        "status": "dev",
    }
    created = main.storage.create_journey(payload, tenant_id=tenant_id)
    _audit("journey_duplicated", tenant_id, created["id"],
           "Journey {0} duplicated from {1}.".format(created["id"], journey_id),
           {"source_journey_id": journey_id, "screens": len(payload["screens"])})
    return created
