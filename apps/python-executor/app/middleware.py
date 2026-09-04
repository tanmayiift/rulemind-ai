from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Tuple

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from .context import (
    REQUEST_ID_HEADER,
    request_id_scope,
    reset_current_api_key_id,
    reset_current_role,
    reset_current_tenant_id,
    set_current_api_key_id,
    set_current_role,
    set_current_tenant_id,
)
from .env import env_int, env_str
from .mtls import MtlsSettings, evaluate_client_cert
from .rbac import is_allowed
from .runtime import is_local_dev
from . import idempotency, rate_limit
from .session_cookie import (
    CSRF_PROTECTED_METHODS,
    SESSION_COOKIE,
    authenticated_via_cookie,
    csrf_valid,
)


LOGGER = logging.getLogger("rulemind.middleware")

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


# ── Pre-auth throttling ────────────────────────────────────────────────────────
#
# Two paths reach a handler before any tenant exists, so the per-tenant limiter below is
# structurally unable to see them: /api/v1/onboarding/signup returns at the EXEMPT_PATHS check
# and the whole /api/admin/v1 surface returns one branch later, both ~76 lines before
# rate_limit.check() runs. Signup is four unauthenticated writes that mint a LIVE API key, so
# unthrottled it is a tenant/key faucet and a free front door to every authenticated route
# behind that key. These limits are keyed by CLIENT IP because that is the only identity that
# exists at this point in the request.
# Per-IP budgets, requests/minute. 20 for signup is six times tighter than the most restrictive
# authenticated plan (DEFAULT_PLAN_RPM["free"] == 120) while still absorbing a shared-NAT office
# or a CI run, which a single-digit ceiling would not.
#
# A rate limit alone does not "fix" signup abuse and is not claimed to — it bounds and surfaces
# it. The endpoint's own docstring asks for email verification / CAPTCHA, and a deployment that
# provisions tenants out of band should set RULEMIND_SELF_SERVE_SIGNUP=0 rather than rely on
# this number.
DEFAULT_SIGNUP_RPM = 20
DEFAULT_ADMIN_RPM = 60


def trusted_proxy_hops() -> int:
    """How many reverse proxies sit in front of this process (RULEMIND_TRUSTED_PROXY_HOPS).

    Defaults to 0 — the socket peer — because X-Forwarded-For is caller-supplied. Trusting it
    blindly would make every IP-keyed limit here trivially bypassable: send a random XFF per
    request and each one gets its own fresh bucket, which is worse than having no limit,
    because it also poisons the audit trail with attacker-chosen addresses.
    """
    return max(0, env_int("RULEMIND_TRUSTED_PROXY_HOPS", 0))


def client_ip(request: Request) -> str:
    """The client address to key pre-auth limits on.

    With N trusted hops configured, the trustworthy entry is the Nth from the RIGHT: each of
    our own proxies appends the peer it received from, so the last N entries are the ones we
    wrote and everything to their left is attacker-controlled. A short header means the
    request did not traverse the expected chain, so it falls back to the socket peer rather
    than accepting whatever was supplied.
    """
    peer = getattr(request.client, "host", "") or "unknown"
    hops = trusted_proxy_hops()
    if hops <= 0:
        return peer
    forwarded = [part.strip() for part in (request.headers.get("x-forwarded-for") or "").split(",") if part.strip()]
    if len(forwarded) < hops:
        return peer
    return forwarded[-hops]


DEFAULT_LOGIN_RPM = 20

# The credential endpoints only. Deliberately not the whole "/api/v1/auth/" prefix: /session
# is polled by the dashboard on every navigation, and throttling it at login rates would log
# real users out. These four are the ones that accept a guess.
CREDENTIAL_PATHS = (
    "/api/v1/auth/login",
    "/api/v1/auth/otp/request",
    "/api/v1/auth/otp/verify",
    "/api/mobile/v1/auth/login",
)


def _pre_auth_limit(path: str) -> Optional[Tuple[str, int]]:
    """(bucket prefix, requests/minute) for a path that authenticates itself, else None."""
    if path == "/api/v1/onboarding/signup":
        return "signup", env_int("RULEMIND_SIGNUP_RATE_LIMIT_PER_MIN", DEFAULT_SIGNUP_RPM)
    if path in CREDENTIAL_PATHS:
        return "login", env_int("RULEMIND_LOGIN_RATE_LIMIT_PER_MIN", DEFAULT_LOGIN_RPM)
    if path.startswith("/api/admin/v1"):
        return "admin", env_int("RULEMIND_ADMIN_RATE_LIMIT_PER_MIN", DEFAULT_ADMIN_RPM)
    return None


def check_pre_auth_rate_limit(request: Request, path: str) -> Optional[JSONResponse]:
    """429 when an unauthenticated caller exceeds the per-IP budget for this surface.

    Reuses rate_limit.check's token bucket via its `key=` argument — the same Redis-backed,
    cross-replica limiter the authenticated path uses. The synthetic tenant carries only a
    `config.rate_limit_rpm`, which resolve_limit() reads first, so these surfaces get their own
    tight ceiling instead of the 1000/min a tenantless caller would otherwise inherit.
    """
    configured = _pre_auth_limit(path)
    if configured is None:
        return None
    prefix, rpm = configured
    if rpm <= 0:  # explicitly disabled by an operator
        return None
    address = client_ip(request)
    decision = rate_limit.check(
        {"config": {"rate_limit_rpm": rpm}},
        key="preauth:{0}:{1}".format(prefix, address),
        # These are IP-keyed, not tenant-keyed, so the RateLimit-* headers must not claim to
        # describe a tenant budget the caller does not have yet.
        scope="preauth:{0}".format(prefix),
    )
    if decision.allowed:
        return None
    LOGGER.warning("pre-auth rate limit hit: surface=%s ip=%s limit=%s path=%s", prefix, address, rpm, path)
    return JSONResponse(
        status_code=429,
        headers=decision.headers(),
        content={
            "error": "Rate limit exceeded",
            "detail": "Too many requests to {0} from this address. Retry in {1}s.".format(
                path, max(1, decision.retry_after)),
        },
    )


# ── Credential-stuffing lockout ────────────────────────────────────────────────
#
# Before this, NONE of the three login endpoints counted failures: the platform superadmin
# login, its mobile twin and the workspace member login each verified a bcrypt hash and
# returned 401, forever, at whatever rate the caller could manage. Only bcrypt's cost factor
# stood between an attacker and an offline-speed online guessing attack against the account
# that can read and mutate every tenant.
#
# The shape is copied from storage.verify_member_otp(..., max_attempts=5), which already got
# this right for OTP codes. The counter lives here rather than in storage.py because this
# agent does not own that file; see the report for the persistent-column version that should
# replace it. Redis-backed when configured, because a per-process counter gives an attacker
# max_attempts x replica_count guesses per window.
DEFAULT_LOGIN_MAX_ATTEMPTS = 5
DEFAULT_LOGIN_LOCKOUT_SECONDS = 900
# Audit-event threshold. Lower than max_attempts on purpose: the point is to make a campaign
# visible while it is still being attempted, not only once it has been stopped.
LOGIN_ALERT_AFTER = 3


class LoginGuard:
    """Outcome of one lockout check."""

    __slots__ = ("locked", "failures", "retry_after", "newly_locked")

    def __init__(self, locked: bool, failures: int, retry_after: int, newly_locked: bool = False) -> None:
        self.locked = locked
        self.failures = failures
        self.retry_after = retry_after
        self.newly_locked = newly_locked

    @property
    def should_alert(self) -> bool:
        return self.failures >= login_alert_after()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "LoginGuard(locked={0}, failures={1}, retry_after={2})".format(
            self.locked, self.failures, self.retry_after)


def login_max_attempts() -> int:
    return max(1, env_int("RULEMIND_LOGIN_MAX_ATTEMPTS", DEFAULT_LOGIN_MAX_ATTEMPTS))


def login_lockout_seconds() -> int:
    return max(1, env_int("RULEMIND_LOGIN_LOCKOUT_SECONDS", DEFAULT_LOGIN_LOCKOUT_SECONDS))


def login_alert_after() -> int:
    return max(1, env_int("RULEMIND_LOGIN_ALERT_AFTER", LOGIN_ALERT_AFTER))


def _login_bucket(scope: str, identity: str) -> str:
    # Hashed: an email address is PII and these keys land in Redis, in `KEYS` output and in
    # anything that dumps the keyspace.
    digest = hashlib.sha256("{0}\x00{1}".format(scope, (identity or "").strip().lower()).encode("utf-8"))
    return "loginfail:{0}:{1}".format(scope, digest.hexdigest()[:32])


class _LocalLoginAttempts:
    """Per-process fallback for when Redis is not configured."""

    def __init__(self) -> None:
        self._entries: Dict[str, Tuple[int, float]] = {}

    def read(self, key: str) -> Tuple[int, float]:
        count, expires = self._entries.get(key, (0, 0.0))
        if expires and expires <= time.time():
            self._entries.pop(key, None)
            return 0, 0.0
        return count, expires

    def bump(self, key: str, ttl: int) -> Tuple[int, float]:
        count, expires = self.read(key)
        if not expires:
            expires = time.time() + ttl
        self._entries[key] = (count + 1, expires)
        return count + 1, expires

    def clear(self, key: str) -> None:
        self._entries.pop(key, None)

    def reset(self) -> None:
        self._entries.clear()


_LOCAL_LOGIN_ATTEMPTS = _LocalLoginAttempts()


def reset_login_attempts_for_tests() -> None:
    """Drop the in-process counters. Named for tests so nothing calls it from a request path."""
    _LOCAL_LOGIN_ATTEMPTS.reset()


def _redis() -> Any:
    """Resolved per call, not bound at import, so a test can swap `runtime.redis_client` and
    exercise the documented in-process fallback deterministically."""
    try:
        from .runtime import redis_client

        return redis_client()
    except Exception:  # pragma: no cover - a broken cache must never break login
        return None


def _durable_store() -> Any:
    """The app's own Storage, resolved per call like `_redis` above.

    Bound at import this would be circular (main imports middleware), and a test that swaps
    `main.storage` for a temp database would keep writing to the old one.
    """
    try:
        from . import main

        return getattr(main, "storage", None)
    except Exception:  # pragma: no cover - a broken store must never break login
        return None


def login_guard_status(scope: str, identity: str) -> LoginGuard:
    """Whether this identity is currently locked out — call BEFORE verifying the credential.

    Checking first is the point: verifying anyway and only refusing afterwards would still
    burn a bcrypt comparison per attempt, which is the CPU-exhaustion half of the same attack.
    """
    key = _login_bucket(scope, identity)
    maximum = login_max_attempts()
    client = _redis()
    if client is not None:
        try:
            raw = client.get(key)
            count = int(raw) if raw else 0
            ttl = int(client.ttl(key) or 0)
            return LoginGuard(locked=count >= maximum, failures=count, retry_after=max(1, ttl) if ttl > 0 else 1)
        except Exception:  # noqa: BLE001 - fall through to the durable counter
            pass
    store = _durable_store()
    if store is not None:
        try:
            count, expires_at = store.read_login_attempts(key)
            remaining = int((expires_at - datetime.utcnow()).total_seconds()) if expires_at else 0
            return LoginGuard(locked=count >= maximum, failures=count,
                              retry_after=max(1, remaining))
        except Exception:  # noqa: BLE001 - fall through to the in-process counter
            pass
    count, expires = _LOCAL_LOGIN_ATTEMPTS.read(key)
    remaining = int(max(0.0, expires - time.time())) if expires else 0
    return LoginGuard(locked=count >= maximum, failures=count, retry_after=max(1, remaining))


def record_login_failure(scope: str, identity: str) -> LoginGuard:
    """Count one failed credential check and report the resulting state."""
    key = _login_bucket(scope, identity)
    maximum = login_max_attempts()
    ttl = login_lockout_seconds()
    count = 0
    remaining = ttl
    client = _redis()
    if client is not None:
        try:
            count = int(client.incr(key))
            if count == 1:
                # Set the window once, on the first failure, and never refresh it. Extending
                # it on every attempt would let an attacker hold a victim's account locked
                # indefinitely just by continuing to guess.
                client.expire(key, ttl)
            remaining = int(client.ttl(key) or ttl)
        except Exception:  # noqa: BLE001 - fall through to the durable counter
            count = 0
    if not count:
        # The database, not the per-process dict. Without Redis the dict was the whole lockout,
        # and it is neither durable nor shared: a restart resets it, and each replica keeps its
        # own count so N replicas multiply the attempts an attacker gets. See models.LoginAttempt.
        store = _durable_store()
        if store is not None:
            try:
                count, expires_at = store.bump_login_attempts(key, ttl)
                remaining = int(max(1.0, (expires_at - datetime.utcnow()).total_seconds()))
            except Exception:  # noqa: BLE001 - last resort below
                count = 0
    if not count:
        count, expires = _LOCAL_LOGIN_ATTEMPTS.bump(key, ttl)
        remaining = int(max(1.0, expires - time.time()))
    return LoginGuard(
        locked=count >= maximum,
        failures=count,
        retry_after=max(1, remaining),
        newly_locked=count == maximum,
    )


def clear_login_failures(scope: str, identity: str) -> None:
    """Reset the counter after a successful authentication."""
    key = _login_bucket(scope, identity)
    client = _redis()
    if client is not None:
        try:
            client.delete(key)
        except Exception:  # noqa: BLE001 - best effort
            pass
    store = _durable_store()
    if store is not None:
        try:
            store.clear_login_attempts(key)
        except Exception:  # noqa: BLE001 - best effort
            pass
    _LOCAL_LOGIN_ATTEMPTS.clear(key)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Stamp every request with a correlation id, and echo it back.

    Deliberately a SEPARATE, outermost middleware rather than a few lines inside
    TenantContextMiddleware.dispatch. That method has ~10 early returns -- OPTIONS, exempt
    paths, 401, 403, 429 -- and those error responses are exactly the ones worth correlating,
    so wiring the id inside it would miss them precisely when it matters.

    request_id_scope accepts an inbound id or generates one. It also SANITISES: an inbound
    header containing a newline is a log-forgery vector, since the id is written into every
    structured log line for the request.
    """

    async def dispatch(self, request: Request, call_next: Callable):
        with request_id_scope(request.headers.get(REQUEST_ID_HEADER)) as request_id:
            request.state.request_id = request_id
            response = await call_next(request)
            response.headers[REQUEST_ID_HEADER] = request_id
            return response


class TenantContextMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, storage) -> None:
        super().__init__(app)
        self.storage = storage

    async def dispatch(self, request: Request, call_next: Callable):
        if request.method == "OPTIONS":
            return await call_next(request)

        path = request.url.path
        # Runs BEFORE the two early returns below. Both of them exit dispatch() long before
        # the per-tenant rate-limit check further down, which is why signup and the admin
        # surface were completely unthrottled: the limiter was not bypassed, it was
        # unreachable.
        throttled = check_pre_auth_rate_limit(request, path)
        if throttled is not None:
            return throttled

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

        if env_str("AUTH_MODE") == "none":
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

        # Limits come from app/rate_limit.py. check_request draws from TWO buckets: the one
        # for this request's resource class (decision / read / write / ai / export / admin,
        # per rate_limit.classify) and the tenant-wide aggregate that bounds their sum. It
        # handles the exempt-path and kill-switch checks itself, so there is one call and no
        # second place for the two to disagree.
        #
        # `cost` is derived from Content-Length rather than the body: a /decide/batch of N
        # payloads is N decisions, and reading the body HERE — outermost, before the handler
        # that streams it — would buffer every bulk upload. See rate_limit.request_cost.
        limit_decision = rate_limit.check_request(
            tenant, request.method, path,
            cost=rate_limit.request_cost(request.method, path,
                                         request.headers.get("content-length")),
        )
        if not limit_decision.allowed:
            return JSONResponse(
                status_code=429,
                headers=limit_decision.headers(),
                content={
                    "error": "Rate limit exceeded",
                    # Naming the budget is the difference between an actionable 429 and a
                    # mystery: "you are out of AI calls" tells a client to back off on ONE
                    # surface, where "you are rate limited" reads as a total outage.
                    "detail": "Limit is {0} requests/minute on the '{1}' budget for this tenant. Retry in {2}s.".format(
                        limit_decision.limit, limit_decision.scope or rate_limit.TENANT,
                        max(1, limit_decision.retry_after)),
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
            # that can see Remaining can slow down before it gets a 429. With two buckets in
            # play the numbers describe the BINDING one (fewest requests left), and
            # RateLimit-Scope names it; advertising the aggregate unconditionally would tell
            # a caller it has 900 requests left while its class bucket rejects the next one.
            if limit_decision.enforced:
                for header, value in limit_decision.headers().items():
                    response.headers[header] = value
            return response
        finally:
            reset_current_role(role_token)
            reset_current_api_key_id(api_key_token)
            reset_current_tenant_id(tenant_token)


def admin_cookie_secure() -> bool:
    """Whether the admin-JWT cookie carries `Secure`.

    This was a byte-identical copy of session_cookie._secure(): `NODE_ENV != "development"`.
    Two problems, both live once a deployment sets RULEMIND_ENV=production. It keyed off
    NODE_ENV, which the API container has no reason to set, and it DEFAULTED to development —
    so the admin session cookie shipped over plain HTTP with no Secure attribute on exactly
    the deployments that needed it. Delegating to runtime.is_local_dev() (itself a delegate to
    security_config._is_local_dev) means RULEMIND_ENV decides, and the duplicate that let the
    two copies drift is gone.
    """
    return not is_local_dev()


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
