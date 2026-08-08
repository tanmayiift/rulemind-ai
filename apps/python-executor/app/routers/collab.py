"""Collaborative editor transport: a WebSocket for live multi-author editing + presence, and REST
endpoints for the time-travel history. Sits on top of ``collab_hub`` (state + CRDT merge) and
``crdt`` (conflict-free merge). See the collaborative editor page in the web app.

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

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from ..collab_hub import hub

router = APIRouter()


class SeedRequest(BaseModel):
    initial: Dict[str, Any] = {}


@router.post("/api/v1/collab/{doc_id}/seed")
def seed_document(doc_id: str, body: SeedRequest) -> Dict[str, Any]:
    """Seed a document's initial fields (no-op if it already has history). Handy for opening an
    editor on an existing policy's fields."""
    hub.seed(doc_id, body.initial)
    return hub.state(doc_id)


@router.get("/api/v1/collab/{doc_id}/state")
def get_state(doc_id: str) -> Dict[str, Any]:
    return {**hub.state(doc_id), "presence": hub.presence(doc_id)}


@router.get("/api/v1/collab/{doc_id}/history")
def get_history(doc_id: str) -> Dict[str, Any]:
    """The version timeline for time-travel: every committed edit as an immutable version."""
    return {"doc_id": doc_id, "history": hub.history(doc_id)}


@router.get("/api/v1/collab/{doc_id}/as-of/{version}")
def get_as_of(doc_id: str, version: int) -> Dict[str, Any]:
    """The document state as of a past version (time-travel read)."""
    return {"doc_id": doc_id, "version": version, "doc": hub.doc_as_of(doc_id, version)}


@router.websocket("/ws/v1/collab/{doc_id}")
async def collab_ws(websocket: WebSocket, doc_id: str, actor: Optional[str] = None) -> None:
    """Live collaborative editing channel. Query param ``actor`` names the editor (defaults to a
    random anon id). Each client gets an ``init`` snapshot on join, then a stream of ``edit`` and
    ``presence`` messages; it sends ``edit`` / ``cursor`` / ``restore``."""
    await websocket.accept()
    actor = actor or "anon-{0}".format(uuid.uuid4().hex[:6])
    client_id = uuid.uuid4().hex
    init = await hub.join(doc_id, client_id, actor, websocket)
    try:
        await websocket.send_json(init)
        while True:
            msg = await websocket.receive_json()
            kind = msg.get("type")
            if kind == "edit":
                changes = msg.get("changes") or {}
                if isinstance(changes, dict) and changes:
                    await hub.edit(doc_id, actor, changes)
            elif kind == "cursor":
                await hub.cursor(doc_id, client_id, msg.get("field"))
            elif kind == "restore":
                version = msg.get("version")
                if isinstance(version, int):
                    await hub.restore(doc_id, version, actor)
            elif kind == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await hub.leave(doc_id, client_id)
