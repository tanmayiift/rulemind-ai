"""Authentication for WebSocket endpoints.

WebSockets cannot be covered by the HTTP auth middleware, for two independent reasons:

  1. Starlette's ``BaseHTTPMiddleware`` returns early for any non-HTTP ASGI scope -- confirmed in
     this venv at ``starlette/middleware/base.py`` -- so a WebSocket connection never reaches
     ``TenantContextMiddleware.dispatch`` at all;
  2. even if it did, the middleware's prefix guard only covers ``/api/v1``, ``/sdk/v1`` and
     ``/scim/v2``. ``/ws/v1`` is not in it.

So the check has to live inside each WebSocket endpoint, and it has to run BEFORE ``accept()`` --
after accept, the endpoint has already agreed to talk, and the first frame it sends is typically
a full document snapshot.

Credentials are read from the same places the HTTP middleware reads them, in the same order, so
the two paths cannot disagree about who a caller is:

  * ``x-api-key`` header, then ``Authorization: Bearer <session>``, then the ``rm_session``
    httpOnly cookie -- which is the dashboard's primary credential and IS sent by the browser on
    a same-origin WebSocket handshake, so the web client needs no change.
  * Browsers cannot set headers on a WebSocket handshake, so the query-string forms ``?api_key=``
    and ``?token=`` are accepted for non-cookie clients. Query strings land in access logs, which
    is a real downside; it is offered as the fallback, after the cookie, rather than as the way in.
  * ``AUTH_MODE=none`` bypasses the check exactly as it does for HTTP, resolving to the default
    tenant. Without this parity, disabling auth for local development would break the editor.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

from fastapi import WebSocket

from .rbac import AUTHOR, capabilities_for
from .session_cookie import SESSION_COOKIE

__all__ = ["WebSocketAuthError", "WebSocketIdentity", "authenticate_websocket"]


class WebSocketAuthError(Exception):
    """The handshake carried no usable credential, or the credential was rejected."""


@dataclass
class WebSocketIdentity:
    tenant_id: str
    role: str
    actor: Optional[str] = None

    @property
    def can_edit(self) -> bool:
        """Whether this identity may mutate the document.

        A viewer or reviewer can watch a collaborative session but must not be able to send
        ``edit``/``restore`` -- the WebSocket is a write path, and it previously accepted writes
        from anyone at all.
        """
        return AUTHOR in capabilities_for(self.role)


def _credential(websocket: WebSocket, header: str, query: str) -> Optional[str]:
    value = websocket.headers.get(header)
    if value:
        return value
    return websocket.query_params.get(query)


async def authenticate_websocket(websocket: WebSocket, storage: Any) -> WebSocketIdentity:
    """Resolve the tenant + role for a WebSocket handshake, or raise WebSocketAuthError."""
    if os.getenv("AUTH_MODE") == "none":
        return WebSocketIdentity(tenant_id=str(storage.default_tenant_id or ""), role="owner")

    api_key = _credential(websocket, "x-api-key", "api_key")
    if api_key:
        resolved = storage.get_tenant_by_api_key(api_key)
        if not resolved:
            raise WebSocketAuthError("Invalid API key")
        tenant = resolved["tenant"]
        if not tenant.get("is_active", True):
            raise WebSocketAuthError("Tenant is inactive")
        return WebSocketIdentity(
            tenant_id=tenant["id"],
            role=resolved["api_key"].get("role", "owner"),
            actor=resolved["api_key"].get("label"),
        )

    bearer = _credential(websocket, "authorization", "token") or ""
    if bearer.lower().startswith("bearer "):
        bearer = bearer[7:]
    # The dashboard's real credential: an httpOnly session cookie. Browsers send cookies on a
    # same-origin WS handshake, so this is what a logged-in editor actually authenticates with --
    # checked after the explicit forms so a deliberate header/param still wins.
    bearer = bearer.strip() or websocket.cookies.get(SESSION_COOKIE, "")
    if bearer:
        resolved = storage.resolve_member_session(bearer)
        if not resolved:
            raise WebSocketAuthError("Invalid or expired session")
        tenant = resolved["tenant"]
        if not tenant.get("is_active", True):
            raise WebSocketAuthError("Tenant is inactive")
        member = resolved.get("member") or {}
        return WebSocketIdentity(
            tenant_id=tenant["id"],
            role=resolved.get("role", "viewer"),
            actor=member.get("email") or member.get("name"),
        )

    raise WebSocketAuthError("Missing API key")
