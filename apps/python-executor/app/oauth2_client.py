"""OAuth2 client credentials for connectors -- the real thing, replacing a bearer token in disguise.

WHAT THIS REPLACES, AND WHY IT WAS WORSE THAN "NOT IMPLEMENTED". `connector_probe._auth_headers`
treated `auth_type: oauth2` exactly like `bearer`: it took the first of
`access_token`, `token`, `api_key`, `client_secret` and sent it as `Authorization: Bearer ...`.
No token endpoint, no grant, no refresh -- a repo-wide grep for `client_credentials`,
`token_url`, `refresh_token` or `grant_type` found only the SSO module. So:

  * a connector configured the normal OAuth2 way -- `client_id` + `client_secret`, no pre-issued
    token -- sent **`Authorization: Bearer <client_secret>` to the data provider's API**. The
    client secret is a credential for the TOKEN ENDPOINT only; this handed it to every resource
    server the connector called, and to anything logging its request headers;
  * a connector given a pre-issued `access_token` worked until that token expired and then failed
    every decision, forever, because nothing could renew it. The seeded GST connector is
    configured `oauth2`, and most bureau and bank-data APIs require it.

WHAT IT DOES NOW (RFC 6749 section 4.4, client credentials):

  config.token_url       the provider's token endpoint. Its presence is what makes this OAuth2 --
                         without it, `oauth2` means "a static access_token", the legacy mode, and
                         NEVER falls back to sending the client secret.
  config.client_id / client_secret
  config.scope           optional, space-separated
  config.audience        optional; Auth0 and several bureaus require it
  config.token_auth_method  `client_secret_basic` (default, section 2.3.1) or `client_secret_post`

The token endpoint is author-supplied and receives the client secret, so it goes through the same
SSRF-guarded egress as a connector fetch: resolve, range-check, pin to the vetted IP, no
redirects. A 3xx from a token endpoint would hand the secret to a host that never passed
`vet_url`.

Tokens are cached per (tenant, connector, endpoint, client, scope, audience) until `expires_in`
minus a safety margin, single-flighted so N concurrent decisions trigger one token request, and
invalidated on a 401 from the resource so a token revoked early is replaced rather than retried
into the ground.
"""
from __future__ import annotations

import base64
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Tuple
from urllib.parse import urlencode

import httpx

from .egress import EgressBlocked, build_pinned_request

# Refresh this many seconds BEFORE the provider says the token expires. A token that expires in
# flight turns into a 401 on a live decision; clocks drift and requests take time.
EXPIRY_MARGIN_SECONDS = 60
# When a provider returns no `expires_in`, assume this. Short on purpose: guessing long means a
# stale token fails decisions until the guess runs out.
DEFAULT_TTL_SECONDS = 300
TOKEN_TIMEOUT_SECONDS = 10.0


class OAuth2Error(Exception):
    """A token could not be obtained. `code` is stable and actionable; `reason` is for a human."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


@dataclass
class _Cached:
    token: str
    expires_at: float


_CACHE: Dict[Tuple[str, ...], _Cached] = {}
_LOCKS: Dict[Tuple[str, ...], threading.Lock] = {}
_GUARD = threading.Lock()


def _str(config: Dict[str, Any], key: str) -> str:
    value = config.get(key)
    return value.strip() if isinstance(value, str) else ""


def uses_token_endpoint(config: Dict[str, Any]) -> bool:
    """Is this a real client-credentials connector (as opposed to a static bearer token)?"""
    return str(config.get("auth_type") or "").lower() == "oauth2" and bool(_str(config, "token_url"))


def cache_key(config: Dict[str, Any], tenant_id: str, connector_id: str) -> Tuple[str, ...]:
    return (tenant_id or "", connector_id or "", _str(config, "token_url"), _str(config, "client_id"),
            _str(config, "scope"), _str(config, "audience"))


def _lock_for(key: Tuple[str, ...]) -> threading.Lock:
    with _GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.Lock()
        return lock


def invalidate(key: Tuple[str, ...]) -> None:
    """Drop a cached token -- called when the resource answers 401, i.e. the provider revoked or
    rotated it before its stated expiry."""
    with _GUARD:
        _CACHE.pop(key, None)


def reset() -> None:
    """Test hook."""
    with _GUARD:
        _CACHE.clear()
        _LOCKS.clear()


def _request_token(config: Dict[str, Any]) -> Tuple[str, int]:
    token_url = _str(config, "token_url")
    client_id = _str(config, "client_id")
    client_secret = _str(config, "client_secret")
    if not client_id or not client_secret:
        raise OAuth2Error("oauth2_client_credentials_missing",
                          "An OAuth2 connector with a token_url needs both client_id and client_secret.")
    method = (_str(config, "token_auth_method") or "client_secret_basic").lower()
    if method not in ("client_secret_basic", "client_secret_post"):
        raise OAuth2Error("oauth2_bad_auth_method",
                          "token_auth_method must be client_secret_basic or client_secret_post, "
                          "got {0!r}.".format(method))

    form: Dict[str, str] = {"grant_type": "client_credentials"}
    if _str(config, "scope"):
        form["scope"] = _str(config, "scope")
    if _str(config, "audience"):
        form["audience"] = _str(config, "audience")
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
               "User-Agent": "RuleMind-OAuth2/1"}
    if method == "client_secret_basic":
        # RFC 6749 section 2.3.1: id and secret are form-urlencoded BEFORE the base64, which
        # matters the moment either contains a ':' or a non-ASCII character.
        from urllib.parse import quote

        raw = "{0}:{1}".format(quote(client_id, safe=""), quote(client_secret, safe=""))
        headers["Authorization"] = "Basic " + base64.b64encode(raw.encode("utf-8")).decode("ascii")
    else:
        form["client_id"] = client_id
        form["client_secret"] = client_secret

    try:
        pinned = build_pinned_request(token_url)
    except EgressBlocked as blocked:
        raise OAuth2Error("oauth2_token_url_blocked",
                          "The token endpoint is blocked by egress policy: {0}".format(blocked)) from blocked
    headers["Host"] = pinned.host_header
    try:
        with httpx.Client(timeout=TOKEN_TIMEOUT_SECONDS, follow_redirects=False) as client:
            response = client.post(pinned.url, content=urlencode(form), headers=headers,
                                   extensions={"sni_hostname": pinned.sni_hostname})
    except httpx.HTTPError as exc:
        raise OAuth2Error("oauth2_token_unreachable",
                          "Could not reach the token endpoint: {0}".format(str(exc)[:200])) from exc

    if response.status_code in (401, 403):
        raise OAuth2Error("oauth2_client_rejected",
                          "The token endpoint rejected the client credentials (HTTP {0}).".format(
                              response.status_code))
    if not (200 <= response.status_code < 300):
        raise OAuth2Error("oauth2_token_failed",
                          "The token endpoint answered HTTP {0}.".format(response.status_code))
    try:
        body = response.json()
    except ValueError as exc:
        raise OAuth2Error("oauth2_token_malformed", "The token endpoint did not return JSON.") from exc
    token = body.get("access_token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise OAuth2Error("oauth2_token_malformed", "The token response has no access_token.")
    token_type = str(body.get("token_type") or "bearer").lower()
    if token_type != "bearer":
        raise OAuth2Error("oauth2_unsupported_token_type",
                          "Only bearer tokens are supported, got {0!r}.".format(token_type))
    try:
        ttl = int(body.get("expires_in") or DEFAULT_TTL_SECONDS)
    except (TypeError, ValueError):
        ttl = DEFAULT_TTL_SECONDS
    return token, max(1, ttl)


def access_token(config: Dict[str, Any], key: Tuple[str, ...]) -> str:
    """A valid access token for this connector, fetching one only when needed.

    Single-flighted per key: concurrent decisions against a cold cache produce ONE token request,
    not one each -- providers rate-limit token endpoints hard, and a stampede at the top of every
    hour would lock the connector out.
    """
    now = time.monotonic()
    cached = _CACHE.get(key)
    if cached and cached.expires_at > now:
        return cached.token
    with _lock_for(key):
        cached = _CACHE.get(key)
        now = time.monotonic()
        if cached and cached.expires_at > now:
            return cached.token
        token, ttl = _request_token(config)
        # Never cache a token for less than a second, and never for longer than it lives.
        lifetime = max(1.0, ttl - EXPIRY_MARGIN_SECONDS) if ttl > EXPIRY_MARGIN_SECONDS else max(1.0, ttl / 2)
        _CACHE[key] = _Cached(token=token, expires_at=time.monotonic() + lifetime)
        return token
