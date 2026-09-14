"""httpOnly session cookie + double-submit CSRF for the human dashboard.

The web app used to keep the member session token in `localStorage` and send it as
`Authorization: Bearer` — readable by any XSS. This moves the browser session into an **httpOnly,
SameSite cookie** the JS can't read, plus a **double-submit CSRF token** for state-changing
requests. Machine callers (API keys) and the mobile SDK keep using the `Authorization` header —
they aren't browsers, so they aren't CSRF-exposed.

- `rm_session`  httpOnly, SameSite=Lax, Secure in prod — the session token; sent automatically.
- `rm_csrf`     readable by JS (not httpOnly) — echoed back in `X-CSRF-Token` on mutations.
  A cross-site attacker can neither read the victim's `rm_csrf` cookie nor set the header to
  match, so header==cookie proves same-origin intent (defense-in-depth over SameSite).
"""
from __future__ import annotations

import secrets

from fastapi import Request, Response

from . import runtime

SESSION_COOKIE = "rm_session"
CSRF_COOKIE = "rm_csrf"
CSRF_HEADER = "x-csrf-token"

# Mutating methods that require a CSRF token when the request is authenticated via the cookie.
CSRF_PROTECTED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _secure() -> bool:
    """Whether to set `Secure` on the session/CSRF cookies — i.e. "is this a real deployment?".

    This used to be `os.getenv("NODE_ENV", "development") != "development"`, which asks the
    WRONG variable. RULEMIND_ENV is the signal a RuleMind deployment sets (see
    security_config._is_local_dev); a manifest that sets RULEMIND_ENV=production and leaves
    NODE_ENV alone -- which is what the Helm chart does -- got `Secure=False`, so the httpOnly
    session cookie and its CSRF partner shipped over TLS without the Secure attribute. Any
    plaintext http:// request to the same host (a stray link, a downgrade, a captive portal)
    then leaks a live session token that JS was deliberately prevented from reading.

    Delegating to runtime.is_local_dev() rather than re-deriving it: this file previously held
    one of THREE byte-identical copies of the NODE_ENV test (middleware.admin_cookie_secure and
    db._warn_if_sqlite_in_production were the others), so fixing it in one place left the others
    behind.
    """
    return not runtime.is_local_dev()


def set_session_cookies(response: Response, token: str, max_age_seconds: int = 60 * 60 * 24 * 7) -> str:
    """Set the httpOnly session cookie + a fresh CSRF cookie. Returns the CSRF token."""
    csrf = secrets.token_urlsafe(32)
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, secure=_secure(), samesite="lax",
        max_age=max_age_seconds, path="/",
    )
    response.set_cookie(
        CSRF_COOKIE, csrf, httponly=False, secure=_secure(), samesite="lax",
        max_age=max_age_seconds, path="/",
    )
    return csrf


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


def session_token_from_request(request: Request) -> str:
    """The member session token from the Authorization header (machine/mobile) or, failing that,
    the httpOnly session cookie (browser)."""
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.cookies.get(SESSION_COOKIE, "") or ""


def csrf_valid(request: Request) -> bool:
    """Double-submit check for cookie-authenticated mutations: the X-CSRF-Token header must match
    the rm_csrf cookie. Only meaningful when the session came from the cookie (see middleware)."""
    header = request.headers.get(CSRF_HEADER, "")
    cookie = request.cookies.get(CSRF_COOKIE, "")
    return bool(header) and bool(cookie) and secrets.compare_digest(header, cookie)


def authenticated_via_cookie(request: Request) -> bool:
    """True when there is no Authorization header but a session cookie is present — i.e. this is a
    browser cookie session that must satisfy CSRF on mutations."""
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer ") or request.headers.get("x-api-key"):
        return False
    return bool(request.cookies.get(SESSION_COOKIE))
