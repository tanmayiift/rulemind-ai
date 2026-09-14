"""Collaborative editor transport: a WebSocket for live multi-author editing + presence, and REST
endpoints for the time-travel history. Sits on top of ``collab_hub`` (state + CRDT merge) and
``crdt`` (conflict-free merge). See the collaborative editor page in the web app.

SECURITY NOTE (T3.1). Every endpoint here used to be reachable with no credentials at all, and
the hub was keyed by ``doc_id`` alone with no notion of a tenant. The WebSocket called
``accept()`` unconditionally, so anyone on the internet could open ``/ws/v1/collab/<policy id>``,
receive a full document snapshot in the ``init`` frame, and send ``edit``/``restore``. Two
independent reasons the auth middleware could not cover it: ``BaseHTTPMiddleware`` skips
non-HTTP ASGI scopes entirely, so a WebSocket never reaches it, and ``/ws/v1`` was not in its
path prefix guard anyway. Authentication therefore has to happen INSIDE the endpoint, which is
what ``authenticate_websocket`` does, and the hub is now keyed by ``(tenant_id, doc_id)``.

Message protocol (JSON, both directions):
  server -> client:  {type:"init", doc, version, presence}
                     {type:"edit", changes, actor, version, doc}
                     {type:"presence", presence}
  client -> server:  {type:"edit", changes:{field:value}}
                     {type:"cursor", field:<name|null>}
                     {type:"restore", version:N}
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from pydantic import BaseModel

from .. import main
from ..collab_hub import hub
from ..ws_auth import WebSocketAuthError, authenticate_websocket

router = APIRouter()


class SeedRequest(BaseModel):
    initial: Dict[str, Any] = {}


@router.post("/api/v1/collab/{doc_id}/seed")
def seed_document(doc_id: str, body: SeedRequest, request: Request) -> Dict[str, Any]:
    """Seed a document's initial fields (no-op if it already has history). Handy for opening an
    editor on an existing policy's fields."""
    tenant_id = main.active_tenant_id(request)
    hub.seed(tenant_id, doc_id, body.initial)
    return hub.state(tenant_id, doc_id)


@router.get("/api/v1/collab/{doc_id}/state")
def get_state(doc_id: str, request: Request) -> Dict[str, Any]:
    tenant_id = main.active_tenant_id(request)
    return {**hub.state(tenant_id, doc_id), "presence": hub.presence(tenant_id, doc_id)}


@router.get("/api/v1/collab/{doc_id}/history")
def get_history(doc_id: str, request: Request) -> Dict[str, Any]:
    """The version timeline for time-travel: every committed edit as an immutable version."""
    tenant_id = main.active_tenant_id(request)
    return {"doc_id": doc_id, "history": hub.history(tenant_id, doc_id)}


@router.get("/api/v1/collab/{doc_id}/as-of/{version}")
def get_as_of(doc_id: str, version: int, request: Request) -> Dict[str, Any]:
    """The document state as of a past version (time-travel read)."""
    tenant_id = main.active_tenant_id(request)
    return {"doc_id": doc_id, "version": version,
            "doc": hub.doc_as_of(tenant_id, doc_id, version)}


@router.websocket("/ws/v1/collab/{doc_id}")
async def collab_ws(websocket: WebSocket, doc_id: str, actor: Optional[str] = None) -> None:
    """Live collaborative editing channel. Query param ``actor`` names the editor (defaults to a
    random anon id). Each client gets an ``init`` snapshot on join, then a stream of ``edit`` and
    ``presence`` messages; it sends ``edit`` / ``cursor`` / ``restore``.

    Authentication happens BEFORE accept(), so an unauthenticated client is rejected at the
    handshake and never receives a document frame.
    """
    try:
        identity = await authenticate_websocket(websocket, main.storage)
    except WebSocketAuthError as error:
        # 1008 = policy violation. Closing before accept() means the handshake itself fails, so
        # no document snapshot is ever sent.
        await websocket.close(code=1008, reason=str(error))
        return

    tenant_id = identity.tenant_id
    await websocket.accept()
    # The actor label is cosmetic (presence). The AUTHORITATIVE identity is `identity`, which the
    # client cannot influence -- so a client cannot claim to be someone else for auditing by
    # passing ?actor=.
    actor = actor or identity.actor or "anon-{0}".format(uuid.uuid4().hex[:6])
    client_id = uuid.uuid4().hex
    init = await hub.join(tenant_id, doc_id, client_id, actor, websocket)
    try:
        await websocket.send_json(init)
        while True:
            msg = await websocket.receive_json()
            kind = msg.get("type")
            if kind == "edit":
                if not identity.can_edit:
                    await websocket.send_json({"type": "error", "detail": "read-only session"})
                    continue
                changes = msg.get("changes") or {}
                if isinstance(changes, dict) and changes:
                    await hub.edit(tenant_id, doc_id, actor, changes)
            elif kind == "cursor":
                await hub.cursor(tenant_id, doc_id, client_id, msg.get("field"))
            elif kind == "restore":
                if not identity.can_edit:
                    await websocket.send_json({"type": "error", "detail": "read-only session"})
                    continue
                version = msg.get("version")
                if isinstance(version, int):
                    await hub.restore(tenant_id, doc_id, version, actor)
            elif kind == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await hub.leave(tenant_id, doc_id, client_id)
