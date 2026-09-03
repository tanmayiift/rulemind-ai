"""Real-time collaborative-editing hub — the server side of the collaborative policy editor.

Holds, per document, the merged CRDT state, an append-only version history (for time-travel), and
the set of connected clients (for presence). Edits from any client are merged with a server-assigned
Lamport timestamp (guaranteeing a total order across concurrent edits) and broadcast to everyone —
so all clients converge to the same document (the ``crdt`` module provides the conflict-free merge).

Every entry point is keyed by ``(tenant_id, doc_id)``, not by ``doc_id`` alone. The hub had no
notion of a tenant at all, so two tenants opening a document with the same id -- and ids here are
caller-supplied, typically a policy id, so a collision is routine rather than exotic -- shared one
document: tenant A saw tenant B's policy draft in its editor and could edit it.

In-process per replica, like ``decision_bus``: presence + live fan-out is a per-connection concern
and connections are pinned to one replica. The durable artifact is the version history, which can be
persisted; cross-replica fan-out would layer Redis pub/sub on top (same pattern as the SSE feed).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

from . import crdt


class _Doc:
    def __init__(self, tenant_id: str, doc_id: str):
        self.tenant_id = tenant_id
        self.doc_id = doc_id
        self.state: crdt.CRDTDoc = {}
        self.history: List[Dict[str, Any]] = []   # [{version, doc(plain), actor, ts, label}]
        self.clients: Dict[str, Dict[str, Any]] = {}  # client_id -> {actor, ws, cursor}
        self.clock = 0

    def _next_ts(self) -> float:
        # Lamport-ish: monotonic server counter blended with wall clock for human-readable ordering.
        self.clock += 1
        return self.clock + time.time() / 1e12


class CollabHub:
    def __init__(self) -> None:
        # Keyed by (tenant_id, doc_id). A doc_id alone is NOT a key: ids are caller-supplied
        # (usually a policy id), so two tenants naming a document the same way is the normal
        # case, and sharing one _Doc means one tenant's draft appears in another's editor.
        self._docs: Dict[Tuple[str, str], _Doc] = {}
        self._lock = asyncio.Lock()

    def _doc(self, tenant_id: str, doc_id: str) -> _Doc:
        key = (tenant_id, doc_id)
        if key not in self._docs:
            self._docs[key] = _Doc(tenant_id, doc_id)
        return self._docs[key]

    def seed(self, tenant_id: str, doc_id: str, initial: Dict[str, Any]) -> None:
        """Seed a document's initial state (v0) if it doesn't exist yet — idempotent."""
        doc = self._doc(tenant_id, doc_id)
        if not doc.history:
            doc.state = crdt.to_crdt(initial or {}, ts=doc._next_ts(), actor="__seed__")
            doc.history.append({"version": 0, "doc": crdt.to_plain(doc.state), "actor": "__seed__",
                                "ts": time.time(), "label": "seed"})

    def presence(self, tenant_id: str, doc_id: str) -> List[Dict[str, Any]]:
        doc = self._doc(tenant_id, doc_id)
        return [{"client_id": cid, "actor": c["actor"], "cursor": c.get("cursor")}
                for cid, c in doc.clients.items()]

    def state(self, tenant_id: str, doc_id: str) -> Dict[str, Any]:
        doc = self._doc(tenant_id, doc_id)
        return {"doc": crdt.to_plain(doc.state), "version": doc.history[-1]["version"] if doc.history else 0}

    def history(self, tenant_id: str, doc_id: str) -> List[Dict[str, Any]]:
        return list(self._doc(tenant_id, doc_id).history)

    def doc_as_of(self, tenant_id: str, doc_id: str, version: int) -> Optional[Dict[str, Any]]:
        return crdt.doc_as_of(self._doc(tenant_id, doc_id).history, version)

    async def join(self, tenant_id: str, doc_id: str, client_id: str, actor: str, ws: Any) -> Dict[str, Any]:
        async with self._lock:
            doc = self._doc(tenant_id, doc_id)
            if not doc.history:
                self.seed(tenant_id, doc_id, {})
            doc.clients[client_id] = {"actor": actor, "ws": ws, "cursor": None}
            init = {"type": "init", **self.state(tenant_id, doc_id), "presence": self.presence(tenant_id, doc_id)}
        await self._broadcast(tenant_id, doc_id, {"type": "presence", "presence": self.presence(tenant_id, doc_id)}, exclude=client_id)
        return init

    async def leave(self, tenant_id: str, doc_id: str, client_id: str) -> None:
        async with self._lock:
            doc = self._doc(tenant_id, doc_id)
            doc.clients.pop(client_id, None)
        await self._broadcast(tenant_id, doc_id, {"type": "presence", "presence": self.presence(tenant_id, doc_id)})

    async def edit(self, tenant_id: str, doc_id: str, actor: str, changes: Dict[str, Any], label: Optional[str] = None) -> Dict[str, Any]:
        """Apply a client's field changes as a new stamped version, then broadcast. Returns the
        server-authoritative merged state so the caller (WS handler) can ack."""
        async with self._lock:
            doc = self._doc(tenant_id, doc_id)
            ts = doc._next_ts()
            doc.state = crdt.apply_edit(doc.state, changes, ts=ts, actor=actor)
            version = (doc.history[-1]["version"] + 1) if doc.history else 1
            doc.history.append({"version": version, "doc": crdt.to_plain(doc.state), "actor": actor,
                                "ts": time.time(), "label": label or "edit"})
            payload = {"type": "edit", "changes": changes, "actor": actor, "version": version,
                       "doc": crdt.to_plain(doc.state)}
        await self._broadcast(tenant_id, doc_id, payload)
        return payload

    async def restore(self, tenant_id: str, doc_id: str, version: int, actor: str) -> Optional[Dict[str, Any]]:
        """Time-travel restore: re-apply a past version's fields as a NEW forward version (history is
        append-only — you can always travel back again)."""
        past = self.doc_as_of(tenant_id, doc_id, version)
        if past is None:
            return None
        return await self.edit(tenant_id, doc_id, actor, past, label="restore v{0}".format(version))

    async def cursor(self, tenant_id: str, doc_id: str, client_id: str, field: Optional[str]) -> None:
        async with self._lock:
            doc = self._doc(tenant_id, doc_id)
            if client_id in doc.clients:
                doc.clients[client_id]["cursor"] = field
        await self._broadcast(tenant_id, doc_id, {"type": "presence", "presence": self.presence(tenant_id, doc_id)})

    async def _broadcast(self, tenant_id: str, doc_id: str, message: Dict[str, Any], exclude: Optional[str] = None) -> None:
        doc = self._doc(tenant_id, doc_id)
        dead: List[str] = []
        for cid, client in list(doc.clients.items()):
            if cid == exclude:
                continue
            try:
                await client["ws"].send_json(message)
            except Exception:
                dead.append(cid)
        for cid in dead:
            doc.clients.pop(cid, None)


# Process-wide singleton (mirrors decision_bus / the fast-cache singletons).
hub = CollabHub()
