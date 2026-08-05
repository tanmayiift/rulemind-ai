from __future__ import annotations

import os
from typing import Callable

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .context import (
    reset_current_api_key_id,
    reset_current_role,
    reset_current_tenant_id,
    set_current_api_key_id,
    set_current_role,
    set_current_tenant_id,
)
from .mtls import MtlsSettings, evaluate_client_cert
from .rbac import is_allowed
from .runtime import rate_limit_allow
from .session_cookie import (
    CSRF_PROTECTED_METHODS,
    SESSION_COOKIE,
    authenticated_via_cookie,
    csrf_valid,
)


EXEMPT_PATHS = {
    "/health",
    "/api/v1/health",
    "/api/v1/onboarding/signup",
    "/ready",
    "/metrics",
    "/docs",
    "/redoc",
    "/openapi.json",
}

# The human-auth endpoints manage their own identity: login/OTP establish it,
# session/logout validate the bearer token themselves. None should be gated by the
# API-key check (login runs before any key/session exists, and a viewer must still
# be able to read their own session and log out).
UNAUTH_AUTH_PREFIXES = ("/api/v1/auth/",)


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    # Browser sessions carry the token in an httpOnly cookie instead of the header.
    return request.cookies.get(SESSION_COOKIE, "") or ""


class TenantContextMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, storage) -> None:
        super().__init__(app)
        self.storage = storage

    async def dispatch(self, request: Request, call_next: Callable):
        if request.method == "OPTIONS":
            return await call_next(request)

        path = request.url.path
        if path in EXEMPT_PATHS or path.startswith("/api/admin/v1/auth"):
            return await call_next(request)

        if path.startswith("/api/admin/v1"):
            return await call_next(request)

        if not (path.startswith("/api/v1") or path.startswith("/sdk/v1")):
            return await call_next(request)

        # mTLS is transport-layer auth, orthogonal to the API-key / session check below, so it
        # runs first and applies even in AUTH_MODE=none. Config-gated (default off) — no effect
        # until an operator opts in. A guarded path without a valid client cert is rejected here.
        mtls = MtlsSettings()
        if mtls.guards(path):
            ok, reason, identity = evaluate_client_cert(request.headers, mtls)
            if not ok:
                return JSONResponse(
                    status_code=401,
                    content={"error": "Client certificate required", "detail": reason},
                )
            request.state.mtls_fingerprint = identity.get("fingerprint")
            request.state.mtls_subject = identity.get("subject")

        # Login / OTP endpoints establish identity — no key or session yet.
        if any(path.startswith(prefix) for prefix in UNAUTH_AUTH_PREFIXES):
            return await call_next(request)

        if os.getenv("AUTH_MODE") == "none":
            return await call_next(request)

        if request.method == "POST" and path.startswith("/api/v1/webhooks/") and not path.endswith("/test"):
            return await call_next(request)

        # CSRF: a browser cookie session performing a state-changing request must present a
        # matching double-submit token. Header/API-key auth (machines, mobile SDK) is exempt —
        # it isn't cookie-driven, so it isn't CSRF-exposed.
        if request.method in CSRF_PROTECTED_METHODS and authenticated_via_cookie(request) and not csrf_valid(request):
            return JSONResponse(status_code=403, content={"error": "CSRF token missing or invalid"})

        storage = self.storage() if callable(self.storage) else self.storage

        # Two ways to authenticate: a machine API key (x-api-key) or a human login
        # session (Authorization: Bearer). Both resolve to a tenant + an RBAC role
        # and are enforced by the same capability check below.
        api_key = request.headers.get("x-api-key")
        session_token = "" if api_key else _bearer_token(request)
        actor_kind = "api_key"
        session_id = None
        if api_key:
            resolved = storage.get_tenant_by_api_key(api_key)
            if not resolved:
                return JSONResponse(status_code=401, content={"error": "Invalid API key"})
            tenant = resolved["tenant"]
            role = resolved["api_key"].get("role", "owner")
            api_key_id = resolved["api_key"]["id"]
        elif session_token:
            resolved = storage.resolve_member_session(session_token)
            if not resolved:
                return JSONResponse(status_code=401, content={"error": "Invalid or expired session"})
            tenant = resolved["tenant"]
            role = resolved.get("role", "viewer")
            actor_kind = "member"
            session_id = resolved.get("session_id")
            api_key_id = "member:{0}".format(resolved.get("member", {}).get("id", ""))
        else:
            return JSONResponse(status_code=401, content={"error": "Missing API key"})

        if not tenant.get("is_active", True):
            return JSONResponse(status_code=403, content={"error": "Tenant is inactive"})

        limit = 5000 if tenant.get("plan") == "enterprise" else 1000
        allowed, retry_after = rate_limit_allow(tenant["id"], limit)
        if not allowed:
            return JSONResponse(status_code=429, headers={"Retry-After": str(retry_after)}, content={"error": "Rate limit exceeded"})

        # RBAC: the caller's role must hold the capability this request needs.
        if not is_allowed(role, request.method, path):
            return JSONResponse(status_code=403, content={
                "error": "Forbidden",
                "detail": "Role '{0}' is not permitted to {1} {2}.".format(role, request.method, path),
            })

        request.state.tenant_id = tenant["id"]
        request.state.api_key_id = api_key_id
        request.state.role = role
        request.state.actor_kind = actor_kind
        request.state.session_id = session_id
        tenant_token = set_current_tenant_id(tenant["id"])
        api_key_token = set_current_api_key_id(api_key_id)
        role_token = set_current_role(role)
        try:
            response = await call_next(request)
            return response
        finally:
            reset_current_role(role_token)
            reset_current_api_key_id(api_key_token)
            reset_current_tenant_id(tenant_token)


def admin_cookie_secure() -> bool:
    return os.getenv("NODE_ENV", "development") != "development"
