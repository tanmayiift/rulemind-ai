"""Platform-admin + mobile-app endpoints — platform-admin auth & tenant/key management, and the
mobile admin app's demo/login/session flows. Extracted verbatim from app/main.py. Stable
helpers/models imported by value from app.main; direct storage calls use main.storage live."""
from __future__ import annotations

import logging
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
from ..middleware import (
    admin_cookie_secure,
    clear_login_failures,
    client_ip,
    login_guard_status,
    login_max_attempts,
    record_login_failure,
)

router = APIRouter()

LOGGER = logging.getLogger("rulemind.auth")

# Both platform-admin logins hold the keys to every tenant, and both sat behind the two early
# returns in TenantContextMiddleware.dispatch() where the rate limiter is unreachable. Neither
# counted failures, so bcrypt's cost factor was the only thing between an attacker and an
# unlimited online guessing attack. The middleware now throttles the surface per IP; this
# counts failures per ACCOUNT, which is what stops a slow distributed spray that stays under
# any single IP's budget.
LOGIN_SCOPE_ADMIN = "platform_admin"


def _login_locked_response(guard: Any) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail="Too many failed sign-in attempts. Try again in {0}s.".format(max(1, guard.retry_after)),
        headers={"Retry-After": str(max(1, guard.retry_after))},
    )


def _audit_login_failure(scope: str, email: str, source_ip: str, guard: Any) -> None:
    """Make a credential-stuffing run alertable rather than merely blocked.

    Blocking without a signal means the first anyone hears of a campaign against the
    superadmin account is a support ticket. Best-effort: an audit-store failure must never
    turn into a login outage.

    Filed against Storage.PLATFORM_TENANT_ID, not a workspace. The platform-admin account has
    no tenant, so this used to resolve onto whatever tenant was in context (or the default one)
    and a brute-force campaign against the superadmin landed in an unrelated customer's
    tamper-evident chain.
    """
    LOGGER.warning("failed login: scope=%s email=%s ip=%s failures=%s locked=%s",
                   scope, email, source_ip, guard.failures, guard.locked)
    if not guard.should_alert:
        return
    try:
        main.storage.add_audit_event({
            "event_type": "login_failed_repeatedly",
            "entity_type": "auth",
            "entity_id": scope,
            "detail": "{0} consecutive failed sign-in attempts for '{1}'{2}".format(
                guard.failures, email, " — account locked" if guard.locked else ""),
            "metadata": {
                "scope": scope, "email": email, "source_ip": source_ip,
                "failures": guard.failures, "locked": guard.locked,
                "max_attempts": login_max_attempts(),
            },
        }, tenant_id=main.storage.PLATFORM_TENANT_ID)
    except Exception:  # pragma: no cover - audit is best effort
        pass


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
    email = (request.email or "").strip().lower()
    source_ip = client_ip(http_request)
    guard = login_guard_status(LOGIN_SCOPE_ADMIN, email)
    if guard.locked:
        raise _login_locked_response(guard)
    user = main.storage.get_platform_admin_user_by_email(request.email)
    if not user or not user.get("is_active") or not bcrypt_verify(request.password, user["password_hash"]):
        # One counter shared with the web admin login (same credential, same account): an
        # attacker must not get a fresh budget just by switching to the mobile endpoint.
        _audit_login_failure(LOGIN_SCOPE_ADMIN, email, source_ip,
                             record_login_failure(LOGIN_SCOPE_ADMIN, email))
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    clear_login_failures(LOGIN_SCOPE_ADMIN, email)
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
def admin_login(request: AdminLoginRequest, response: Response, http_request: Request) -> Dict[str, Any]:
    email = (request.email or "").strip().lower()
    source_ip = client_ip(http_request)
    guard = login_guard_status(LOGIN_SCOPE_ADMIN, email)
    if guard.locked:
        raise _login_locked_response(guard)
    user = main.storage.get_platform_admin_user_by_email(request.email)
    if not user or not user.get("is_active") or not bcrypt_verify(request.password, user["password_hash"]):
        # Counted for a nonexistent email too: skipping the count for unknown accounts would
        # answer "is this address a superadmin?" through the lockout's timing and status code.
        _audit_login_failure(LOGIN_SCOPE_ADMIN, email, source_ip,
                             record_login_failure(LOGIN_SCOPE_ADMIN, email))
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    clear_login_failures(LOGIN_SCOPE_ADMIN, email)
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
