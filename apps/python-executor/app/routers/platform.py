"""Platform-admin + mobile-app endpoints — platform-admin auth & tenant/key management, and the
mobile admin app's demo/login/session flows. Extracted verbatim from app/main.py. Stable
helpers/models imported by value from app.main; direct storage calls use main.storage live."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Cookie, HTTPException, Request, Response

from .. import main
from ..auth import JWT_COOKIE_NAME, bcrypt_verify, create_admin_jwt
from ..main import (
    AdminLoginRequest,
    MobileAdminLoginRequest,
    TenantCreateRequest,
    TenantUpdateRequest,
    bearer_token,
    mobile_session_payload,
    public_api_base_url,
    require_platform_admin,
    require_platform_admin_request,
    sanitize_admin_user,
)
from ..middleware import admin_cookie_secure

router = APIRouter()


# ── Mobile admin app ───────────────────────────────────────────────────────
@router.post("/api/mobile/v1/auth/demo")
def mobile_demo_access(request: Request) -> Dict[str, Any]:
    tenant_id = str(main.storage.default_tenant_id or "")
    if not tenant_id:
        raise HTTPException(status_code=500, detail="Default tenant is unavailable.")
    return mobile_session_payload(
        base_url=public_api_base_url(request),
        user=None,
        tenant_id=tenant_id,
        mode="demo",
    )


@router.post("/api/mobile/v1/auth/login")
def mobile_admin_login(request: MobileAdminLoginRequest, http_request: Request) -> Dict[str, Any]:
    user = main.storage.get_platform_admin_user_by_email(request.email)
    if not user or not user.get("is_active") or not bcrypt_verify(request.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    tenant_id = request.tenantId or str(main.storage.default_tenant_id or "")
    token = create_admin_jwt(user["id"], user["email"])
    return mobile_session_payload(
        base_url=public_api_base_url(http_request),
        user=user,
        tenant_id=tenant_id,
        mode="admin",
        access_token=token,
    )


@router.get("/api/mobile/v1/auth/me")
def mobile_admin_me(request: Request) -> Dict[str, Any]:
    user = require_platform_admin_request(request)
    return {"user": sanitize_admin_user(user), "availableTenants": main.storage.list_tenants()}


@router.post("/api/mobile/v1/tenants/{tenant_id}/session")
def mobile_switch_tenant(tenant_id: str, request: Request) -> Dict[str, Any]:
    user = require_platform_admin_request(request)
    return mobile_session_payload(
        base_url=public_api_base_url(request),
        user=user,
        tenant_id=tenant_id,
        mode="admin",
        access_token=bearer_token(request),
    )


# ── Platform admin (tenant + key management) ───────────────────────────────
@router.post("/api/admin/v1/auth/login")
def admin_login(request: AdminLoginRequest, response: Response) -> Dict[str, Any]:
    user = main.storage.get_platform_admin_user_by_email(request.email)
    if not user or not user.get("is_active") or not bcrypt_verify(request.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    token = create_admin_jwt(user["id"], user["email"])
    response.set_cookie(
        JWT_COOKIE_NAME,
        token,
        httponly=True,
        secure=admin_cookie_secure(),
        samesite="lax",
        path="/",
        max_age=60 * 60 * 12,
    )
    return {"user": {key: value for key, value in user.items() if key != "password_hash"}}


@router.get("/api/admin/v1/auth/me")
def admin_me(admin_token: Optional[str] = Cookie(default=None, alias=JWT_COOKIE_NAME)) -> Dict[str, Any]:
    return {"user": sanitize_admin_user(require_platform_admin(admin_token))}


@router.post("/api/admin/v1/auth/logout")
def admin_logout(response: Response) -> Dict[str, bool]:
    response.delete_cookie(JWT_COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/api/admin/v1/tenants")
def admin_list_tenants(admin_token: Optional[str] = Cookie(default=None, alias=JWT_COOKIE_NAME)) -> List[Dict[str, Any]]:
    require_platform_admin(admin_token)
    return main.storage.list_tenants()


@router.post("/api/admin/v1/tenants")
def admin_create_tenant(request: TenantCreateRequest, admin_token: Optional[str] = Cookie(default=None, alias=JWT_COOKIE_NAME)) -> Dict[str, Any]:
    require_platform_admin(admin_token)
    return main.storage.create_tenant(request.name, plan=request.plan, config=request.config, is_active=request.is_active)


@router.get("/api/admin/v1/tenants/{tenant_id}")
def admin_get_tenant(tenant_id: str, admin_token: Optional[str] = Cookie(default=None, alias=JWT_COOKIE_NAME)) -> Dict[str, Any]:
    require_platform_admin(admin_token)
    tenant = main.storage.get_tenant(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found.")
    return tenant


@router.patch("/api/admin/v1/tenants/{tenant_id}")
def admin_update_tenant(tenant_id: str, request: TenantUpdateRequest, admin_token: Optional[str] = Cookie(default=None, alias=JWT_COOKIE_NAME)) -> Dict[str, Any]:
    require_platform_admin(admin_token)
    tenant = main.storage.update_tenant(tenant_id, request.model_dump(exclude_none=True))
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found.")
    return tenant


@router.post("/api/admin/v1/tenants/{tenant_id}/keys")
def admin_create_tenant_key(tenant_id: str, admin_token: Optional[str] = Cookie(default=None, alias=JWT_COOKIE_NAME)) -> Dict[str, Any]:
    require_platform_admin(admin_token)
    try:
        return main.storage.generate_api_key_for_tenant(tenant_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/api/admin/v1/tenants/{tenant_id}/keys")
def admin_list_tenant_keys(tenant_id: str, admin_token: Optional[str] = Cookie(default=None, alias=JWT_COOKIE_NAME)) -> List[Dict[str, Any]]:
    require_platform_admin(admin_token)
    return main.storage.list_api_keys(tenant_id)


@router.delete("/api/admin/v1/tenants/{tenant_id}/keys/{kid}")
def admin_revoke_tenant_key(tenant_id: str, kid: str, admin_token: Optional[str] = Cookie(default=None, alias=JWT_COOKIE_NAME)) -> Dict[str, bool]:
    require_platform_admin(admin_token)
    if not main.storage.revoke_api_key(tenant_id, kid):
        raise HTTPException(status_code=404, detail="API key not found.")
    return {"revoked": True}
