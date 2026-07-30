from __future__ import annotations

import os
from typing import Callable

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .context import reset_current_api_key_id, reset_current_tenant_id, set_current_api_key_id, set_current_tenant_id
from .runtime import rate_limit_allow


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

        if os.getenv("AUTH_MODE") == "none":
            return await call_next(request)

        if request.method == "POST" and path.startswith("/api/v1/webhooks/") and not path.endswith("/test"):
            return await call_next(request)

        api_key = request.headers.get("x-api-key")
        if not api_key:
            return JSONResponse(status_code=401, content={"error": "Missing API key"})

        storage = self.storage() if callable(self.storage) else self.storage
        resolved = storage.get_tenant_by_api_key(api_key)
        if not resolved:
            return JSONResponse(status_code=401, content={"error": "Invalid API key"})

        tenant = resolved["tenant"]
        if not tenant.get("is_active", True):
            return JSONResponse(status_code=403, content={"error": "Tenant is inactive"})

        limit = 5000 if tenant.get("plan") == "enterprise" else 1000
        allowed, retry_after = rate_limit_allow(tenant["id"], limit)
        if not allowed:
            return JSONResponse(status_code=429, headers={"Retry-After": str(retry_after)}, content={"error": "Rate limit exceeded"})

        request.state.tenant_id = tenant["id"]
        request.state.api_key_id = resolved["api_key"]["id"]
        tenant_token = set_current_tenant_id(tenant["id"])
        api_key_token = set_current_api_key_id(resolved["api_key"]["id"])
        try:
            response = await call_next(request)
            return response
        finally:
            reset_current_api_key_id(api_key_token)
            reset_current_tenant_id(tenant_token)


def admin_cookie_secure() -> bool:
    return os.getenv("NODE_ENV", "development") != "development"
