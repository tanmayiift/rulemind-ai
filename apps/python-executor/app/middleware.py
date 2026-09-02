from __future__ import annotations

import os
from typing import Callable

from fastapi import Request
from fastapi.responses import JSONResponse, Response
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
from . import idempotency, rate_limit
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

        # /scim/v2 is guarded here too. It is a USER-PROVISIONING api: left outside this
        # check it would be fully unauthenticated, letting anyone enumerate and create
        # accounts. Adding the prefix to the router without this line is the whole exploit.
        if not (path.startswith("/api/v1") or path.startswith("/sdk/v1") or path.startswith("/scim/v2")):
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

        # Limits come from app/rate_limit.py: per-tenant config, then per-plan env,
        # then a global env, then a built-in default. RATE_LIMIT_ENABLED=0 turns the
        # whole thing off, which is how load tests and benchmarks should run.
        limit_decision = (
            rate_limit.ALLOW_ALL if rate_limit.is_exempt(path)
            else rate_limit.check(tenant)
        )
        if not limit_decision.allowed:
            return JSONResponse(
                status_code=429,
                headers=limit_decision.headers(),
                content={
                    "error": "Rate limit exceeded",
                    "detail": "Limit is {0} requests/minute for this tenant. Retry in {1}s.".format(
                        limit_decision.limit, max(1, limit_decision.retry_after)),
                },
            )

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
            # Advertise the budget on every response, not just on rejection — a client
            # that can see Remaining can slow down before it gets a 429.
            if limit_decision.enforced:
                for header, value in limit_decision.headers().items():
                    response.headers[header] = value
            return response
        finally:
            reset_current_role(role_token)
            reset_current_api_key_id(api_key_token)
            reset_current_tenant_id(tenant_token)


def admin_cookie_secure() -> bool:
    return os.getenv("NODE_ENV", "development") != "development"


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """Replay the original response when a write is retried with the same Idempotency-Key.

    Registered INSIDE TenantContextMiddleware (i.e. added before it, since Starlette makes the
    last-added middleware outermost) for two reasons: auth must run first, so an unauthenticated
    request never gets to claim a key; and request.state.tenant_id must already be set, because
    keys are scoped per tenant.
    """

    def __init__(self, app, storage) -> None:
        super().__init__(app)
        self.storage = storage

    async def dispatch(self, request: Request, call_next: Callable):
        key = (request.headers.get(idempotency.HEADER) or "").strip()
        path = request.url.path
        if (
            not key
            or request.method not in idempotency.IDEMPOTENT_METHODS
            or len(key) > idempotency.MAX_KEY_LENGTH
            or not (path.startswith("/api/v1") or path.startswith("/sdk/v1"))
        ):
            return await call_next(request)

        # Safe to consume: BaseHTTPMiddleware wraps the request in starlette's _CachedRequest,
        # which caches the body precisely when dispatch() calls .body() and replays it to the
        # downstream app (starlette/middleware/base.py, verified against 0.41.3). Hand-rolling
        # a receive-channel replay here is therefore redundant -- but the caching IS a
        # dependency on that behaviour, so test_the_request_body_still_reaches_the_handler
        # guards it against a future starlette upgrade rather than trusting it silently.
        body = await request.body()

        # `storage` is a live accessor, not an instance -- the test harness swaps the object it
        # returns, so resolving it per request (as TenantContextMiddleware does at :104) is
        # required, not stylistic. Binding the instance once would write claims to a database
        # the handler is no longer using.
        storage = self.storage() if callable(self.storage) else self.storage
        tenant_id = getattr(request.state, "tenant_id", None)
        fingerprint = idempotency.request_fingerprint(request.method, path, body)
        try:
            claim = storage.claim_idempotency_key(key, fingerprint, tenant_id=tenant_id)
        except Exception:  # noqa: BLE001 - a broken key store must not take writes offline
            return await call_next(request)

        if claim["action"] == "replay":
            return Response(content=claim["body"], status_code=claim["status"],
                            media_type="application/json",
                            headers={"Idempotent-Replay": "true"})
        if claim["action"] == "in_flight":
            return JSONResponse(status_code=409, content={
                "detail": {
                    "error": "idempotency_key_in_flight",
                    "message": "A request with this Idempotency-Key is still being processed.",
                }})
        if claim["action"] == "mismatch":
            return JSONResponse(status_code=422, content={
                "detail": {
                    "error": "idempotency_key_reused",
                    "message": ("This Idempotency-Key was already used for a different request. "
                                "Generate a new key per operation."),
                }})

        try:
            response = await call_next(request)
        except Exception:
            # Release rather than leave the key claimed: a crashed handler must not make the
            # key answer 409 forever, which would be worse than the original failure.
            storage.release_idempotency_key(key, tenant_id=tenant_id)
            raise

        chunks = [chunk async for chunk in response.body_iterator]
        payload = b"".join(chunks)
        try:
            storage.complete_idempotency_key(key, response.status_code,
                                                  payload.decode("utf-8", "replace"),
                                                  tenant_id=tenant_id)
        except Exception:  # noqa: BLE001 - the response is already correct; remembering is best-effort
            pass
        return Response(content=payload, status_code=response.status_code,
                        headers=dict(response.headers), media_type=response.media_type)
