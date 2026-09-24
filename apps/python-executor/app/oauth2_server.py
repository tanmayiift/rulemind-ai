"""OAuth2 client credentials for RuleMind's OWN API (RFC 6749 section 4.4).

THE GAP. The only machine credential was a long-lived `x-api-key`, sent on every request. That is
the thing an integration team's security review objects to first: a secret that never expires,
transmitted constantly, and that standard OAuth2 client libraries -- which most enterprise
integration platforms are built on -- cannot use at all.

WHAT THIS ADDS. `POST /api/v1/oauth/token` exchanges an API key for a short-lived bearer token:

    client_id      the key's id (`kid`, shown in GET /api/v1/access/keys)
    client_secret  the key itself
    grant_type     client_credentials

The token is then sent as `Authorization: Bearer <token>`. It carries NO more authority than the
key it came from -- same tenant, same role -- and it dies with the key: every request re-resolves
the key by id, so revoking a key revokes its tokens within the same cache window that revoking
the key itself takes effect in.

WHY THE KEY IS DOMAIN-SEPARATED. Admin sessions are already HS256 JWTs signed with
RULEMIND_ADMIN_JWT_SECRET. Signing access tokens with the SAME key would let a platform-admin
token be replayed as a tenant API token (and the reverse), because the verifier could not tell
which one it had been handed. The access-token key is derived from that secret with a fixed label,
and every token carries `typ: rm_access`, which is checked -- so no new secret to provision, and no
way for one kind of token to be accepted as the other.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Any, Dict, Optional

import jwt

from . import env

TOKEN_TYPE = "rm_access"
ISSUER = "rulemind"
_DOMAIN_LABEL = b"rulemind-api-access-token-v1"
DEFAULT_TTL_SECONDS = 3600
MAX_TTL_SECONDS = 86400


def _signing_key() -> bytes:
    base = env.env_str("RULEMIND_ADMIN_JWT_SECRET", "rulemind-admin-dev-secret").encode("utf-8")
    return hmac.new(base, _DOMAIN_LABEL, hashlib.sha256).digest()


def ttl_seconds() -> int:
    return max(60, min(env.env_int("OAUTH2_ACCESS_TOKEN_TTL_SECONDS", DEFAULT_TTL_SECONDS), MAX_TTL_SECONDS))


def issue(resolved: Dict[str, Any]) -> Dict[str, Any]:
    """An access token for a resolved API key (the shape `get_tenant_by_api_key` returns)."""
    now = int(time.time())
    ttl = ttl_seconds()
    claims = {
        "iss": ISSUER,
        "typ": TOKEN_TYPE,
        "sub": resolved["api_key"]["kid"],
        "tid": resolved["tenant"]["id"],
        "iat": now,
        "exp": now + ttl,
        "jti": secrets.token_urlsafe(12),
    }
    token = jwt.encode(claims, _signing_key(), algorithm="HS256")
    return {"access_token": token, "token_type": "Bearer", "expires_in": ttl}


def looks_like_access_token(token: str) -> bool:
    """Cheap shape test so the middleware only attempts verification on JWTs. Session tokens are
    opaque `rms_...` strings and never have two dots."""
    return bool(token) and token.count(".") == 2 and not token.startswith("rms_")


def verify(token: str) -> Optional[Dict[str, Any]]:
    """The claims of a valid access token, or None. Never raises."""
    try:
        claims = jwt.decode(token, _signing_key(), algorithms=["HS256"], issuer=ISSUER,
                            options={"require": ["exp", "iat", "sub", "tid", "typ"]})
    except jwt.PyJWTError:
        return None
    if claims.get("typ") != TOKEN_TYPE:
        return None
    return claims
