"""Real-time collaborative-editing hub — the server side of the collaborative policy editor.

Holds, per document, the merged CRDT state, an append-only version history (for time-travel), and
the set of connected clients (for presence). Edits from any client are merged with a server-assigned
Lamport timestamp (guaranteeing a total order across concurrent edits) and broadcast to everyone —
so all clients converge to the same document (the ``crdt`` module provides the conflict-free merge).

In-process per replica, like ``decision_bus``: presence + live fan-out is a per-connection concern
and connections are pinned to one replica. The durable artifact is the version history, which can be
persisted; cross-replica fan-out would layer Redis pub/sub on top (same pattern as the SSE feed).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

from . import crdt


class _Doc:
    def __init__(self, doc_id: str):
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
        self._docs: Dict[str, _Doc] = {}
        self._lock = asyncio.Lock()

    def _doc(self, doc_id: str) -> _Doc:
        if doc_id not in self._docs:
            self._docs[doc_id] = _Doc(doc_id)
        return self._docs[doc_id]

    def seed(self, doc_id: str, initial: Dict[str, Any]) -> None:
        """Seed a document's initial state (v0) if it doesn't exist yet — idempotent."""
        doc = self._doc(doc_id)
        if not doc.history:
            doc.state = crdt.to_crdt(initial or {}, ts=doc._next_ts(), actor="__seed__")
            doc.history.append({"version": 0, "doc": crdt.to_plain(doc.state), "actor": "__seed__",
                                "ts": time.time(), "label": "seed"})

    def presence(self, doc_id: str) -> List[Dict[str, Any]]:
        doc = self._doc(doc_id)
        return [{"client_id": cid, "actor": c["actor"], "cursor": c.get("cursor")}
                for cid, c in doc.clients.items()]

    def state(self, doc_id: str) -> Dict[str, Any]:
        doc = self._doc(doc_id)
        return {"doc": crdt.to_plain(doc.state), "version": doc.history[-1]["version"] if doc.history else 0}

    def history(self, doc_id: str) -> List[Dict[str, Any]]:
        return list(self._doc(doc_id).history)

    def doc_as_of(self, doc_id: str, version: int) -> Optional[Dict[str, Any]]:
        return crdt.doc_as_of(self._doc(doc_id).history, version)

    async def join(self, doc_id: str, client_id: str, actor: str, ws: Any) -> Dict[str, Any]:
        async with self._lock:
            doc = self._doc(doc_id)
            if not doc.history:
                self.seed(doc_id, {})
            doc.clients[client_id] = {"actor": actor, "ws": ws, "cursor": None}
            init = {"type": "init", **self.state(doc_id), "presence": self.presence(doc_id)}
        await self._broadcast(doc_id, {"type": "presence", "presence": self.presence(doc_id)}, exclude=client_id)
        return init

    async def leave(self, doc_id: str, client_id: str) -> None:
        async with self._lock:
            doc = self._doc(doc_id)
            doc.clients.pop(client_id, None)
        await self._broadcast(doc_id, {"type": "presence", "presence": self.presence(doc_id)})

    async def edit(self, doc_id: str, actor: str, changes: Dict[str, Any], label: Optional[str] = None) -> Dict[str, Any]:
        """Apply a client's field changes as a new stamped version, then broadcast. Returns the
        server-authoritative merged state so the caller (WS handler) can ack."""
        async with self._lock:
            doc = self._doc(doc_id)
            ts = doc._next_ts()
            doc.state = crdt.apply_edit(doc.state, changes, ts=ts, actor=actor)
            version = (doc.history[-1]["version"] + 1) if doc.history else 1
            doc.history.append({"version": version, "doc": crdt.to_plain(doc.state), "actor": actor,
                                "ts": time.time(), "label": label or "edit"})
            payload = {"type": "edit", "changes": changes, "actor": actor, "version": version,
                       "doc": crdt.to_plain(doc.state)}
        await self._broadcast(doc_id, payload)
        return payload

    async def restore(self, doc_id: str, version: int, actor: str) -> Optional[Dict[str, Any]]:
        """Time-travel restore: re-apply a past version's fields as a NEW forward version (history is
        append-only — you can always travel back again)."""
        past = self.doc_as_of(doc_id, version)
        if past is None:
            return None
        return await self.edit(doc_id, actor, past, label="restore v{0}".format(version))

    async def cursor(self, doc_id: str, client_id: str, field: Optional[str]) -> None:
        async with self._lock:
            doc = self._doc(doc_id)
            if client_id in doc.clients:
                doc.clients[client_id]["cursor"] = field
        await self._broadcast(doc_id, {"type": "presence", "presence": self.presence(doc_id)})

    async def _broadcast(self, doc_id: str, message: Dict[str, Any], exclude: Optional[str] = None) -> None:
        doc = self._doc(doc_id)
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
