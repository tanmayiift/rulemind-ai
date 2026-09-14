"""SCIM 2.0 provisioning endpoints (RFC 7644) — Track B6.

Lets an enterprise IdP (Okta, Entra ID, OneLogin) push joiners, movers and leavers into a
workspace automatically. The leaver path is the one that gets audited: a termination in the
IdP must end access here without anyone remembering to do it.

Mounted under /scim/v2 rather than /api/v1 because that prefix is what IdP connectors expect
to be given as the "SCIM base URL". TenantContextMiddleware was extended to guard it -- it
previously covered only /api/v1 and /sdk/v1, so a new prefix would have been entirely
unauthenticated, which for a user-provisioning API means anyone could enumerate and create
accounts.

Responses use application/scim+json and SCIM's own envelopes throughout, including for errors:
an IdP parses the error body, and FastAPI's default {"detail": ...} is not SCIM.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import JSONResponse

from .. import main, scim

router = APIRouter()


def _scim(body: Dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=body, status_code=status_code, media_type=scim.CONTENT_TYPE)


def _error(status: int, detail: str, scim_type: Optional[str] = None) -> JSONResponse:
    return _scim(scim.error(status, detail, scim_type), status_code=status)


# ── discovery ──────────────────────────────────────────────────────────────
@router.get("/scim/v2/ServiceProviderConfig")
def service_provider_config() -> JSONResponse:
    return _scim(scim.service_provider_config())


@router.get("/scim/v2/ResourceTypes")
def resource_types() -> JSONResponse:
    return _scim(scim.list_response([{
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
        "id": "User",
        "name": "User",
        "endpoint": "/Users",
        "schema": scim.USER_SCHEMA,
        "meta": {"resourceType": "ResourceType", "location": "/scim/v2/ResourceTypes/User"},
    }], total=1, start_index=1, per_page=1))


@router.get("/scim/v2/Schemas")
def schemas() -> JSONResponse:
    return _scim(scim.list_response([{
        "id": scim.USER_SCHEMA,
        "name": "User",
        "description": "Workspace member",
        "meta": {"resourceType": "Schema", "location": f"/scim/v2/Schemas/{scim.USER_SCHEMA}"},
    }], total=1, start_index=1, per_page=1))


# ── Users ──────────────────────────────────────────────────────────────────
@router.get("/scim/v2/Users")
def list_users(request: Request,
               filter: Optional[str] = Query(None),
               startIndex: int = Query(1, ge=1),
               count: int = Query(100, ge=0, le=200)) -> JSONResponse:
    """List or search members.

    `startIndex` is 1-BASED per RFC 7644 s3.4.2.4. Treating it as a 0-based offset drops the
    first user of every page, and the symptom -- "one user never provisions" -- is very hard
    to trace back here.
    """
    tenant_id = main.active_tenant_id(request)
    members = main.storage.list_members(tenant_id)

    try:
        parsed = scim.parse_filter(filter)
    except ValueError as exc:
        # 400 rather than ignoring the filter: an IdP reads an unfiltered list as "no match"
        # and creates a duplicate user.
        return _error(400, str(exc), scim_type="invalidFilter")

    if parsed:
        attr, op, value = parsed
        def matches(member: Dict[str, Any]) -> bool:
            if attr == "username":
                actual = (member.get("email") or "").lower()
                target = value.lower()
            elif attr == "externalid":
                actual, target = (member.get("external_id") or ""), value
            else:
                actual, target = str(bool(member.get("is_active"))).lower(), value.lower()
            return actual != target if op == "ne" else actual == target
        members = [m for m in members if matches(m)]

    total = len(members)
    page = members[startIndex - 1: startIndex - 1 + count] if count else []
    return _scim(scim.list_response([scim.to_scim_user(m) for m in page],
                                    total=total, start_index=startIndex, per_page=len(page)))


@router.get("/scim/v2/Users/{user_id}")
def get_user(request: Request, user_id: str) -> JSONResponse:
    tenant_id = main.active_tenant_id(request)
    member = main.storage.get_member(tenant_id, user_id)
    if not member:
        return _error(404, f"User {user_id} not found")
    return _scim(scim.to_scim_user(member))


@router.post("/scim/v2/Users")
def create_user(request: Request, payload: Dict[str, Any] = Body(...)) -> JSONResponse:
    tenant_id = main.active_tenant_id(request)
    fields = scim.from_scim_user(payload)
    if not fields.get("email"):
        return _error(400, "userName is required", scim_type="invalidValue")

    # 409 on a duplicate userName is required by RFC 7644 s3.3 -- an IdP retrying a create
    # after a timeout depends on it to distinguish "already provisioned" from a real error.
    if main.storage.get_member_by_email(fields["email"], tenant_id=tenant_id):
        return _error(409, f"User {fields['email']} already exists", scim_type="uniqueness")

    member = main.storage.create_member(
        tenant_id, fields["email"], fields["name"],
        # Least privilege: SCIM carries no RBAC role, so an IdP must never be able to mint an
        # admin by omission. Elevation stays a deliberate act inside RuleMind.
        role=scim.DEFAULT_ROLE,
        auth_provider="oidc",
        external_id=fields.get("external_id"),
    )
    if not fields.get("is_active", True):
        main.storage.update_member(tenant_id, member["id"], {"is_active": False})
        member = main.storage.get_member(tenant_id, member["id"]) or member
    return _scim(scim.to_scim_user(member), status_code=201)


@router.put("/scim/v2/Users/{user_id}")
def replace_user(request: Request, user_id: str, payload: Dict[str, Any] = Body(...)) -> JSONResponse:
    tenant_id = main.active_tenant_id(request)
    if not main.storage.get_member(tenant_id, user_id):
        return _error(404, f"User {user_id} not found")
    fields = scim.from_scim_user(payload)
    updated = main.storage.update_member(tenant_id, user_id, {
        k: v for k, v in fields.items() if v is not None})
    if updated is None:  # deleted between the existence check and the write
        return _error(404, f"User {user_id} not found")
    return _scim(scim.to_scim_user(updated))


@router.patch("/scim/v2/Users/{user_id}")
def patch_user(request: Request, user_id: str, payload: Dict[str, Any] = Body(...)) -> JSONResponse:
    """The deprovisioning path.

    A termination arrives as a PATCH setting `active` false. update_member invalidates the
    member's cached sessions, so revocation is immediate rather than waiting for a session to
    expire -- which is the difference between a real offboarding control and a flag.
    """
    tenant_id = main.active_tenant_id(request)
    if not main.storage.get_member(tenant_id, user_id):
        return _error(404, f"User {user_id} not found")
    patch = scim.apply_patch({}, payload.get("Operations") or payload.get("operations") or [])
    if not patch:
        return _error(400, "No supported operations in PatchOp", scim_type="invalidValue")
    updated = main.storage.update_member(tenant_id, user_id, patch)
    if updated is None:  # deleted between the existence check and the write
        return _error(404, f"User {user_id} not found")
    return _scim(scim.to_scim_user(updated))


@router.delete("/scim/v2/Users/{user_id}")
def delete_user(request: Request, user_id: str) -> JSONResponse:
    """DEACTIVATES rather than deleting the row.

    Deliberate: audit events, decisions and promotions reference the member, and hard-deleting
    would either orphan that history or cascade it away -- destroying the audit trail of what
    a person did is the opposite of what an offboarding control is for. The member stops being
    able to authenticate, which is what the IdP is asking for.
    """
    tenant_id = main.active_tenant_id(request)
    if not main.storage.get_member(tenant_id, user_id):
        return _error(404, f"User {user_id} not found")
    main.storage.update_member(tenant_id, user_id, {"is_active": False})
    return JSONResponse(content=None, status_code=204)
