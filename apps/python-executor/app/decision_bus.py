"""Real-time decision fan-out for the live SSE feed.

The dashboard's live decision feed used to poll the DB once per second **per open connection** —
`O(viewers x polls)` full-tenant queries, and it can't see decisions served by another replica.
This bus decouples producers (any process that logs a decision) from consumers (SSE streams):

  - **With Redis** (production / multi-replica): each logged decision is `PUBLISH`ed to a
    per-tenant channel; every replica's SSE consumers `SUBSCRIBE` and receive it — no DB polling,
    and cross-replica by construction.
  - **Without Redis** (single-replica dev): publishing is a no-op and the SSE endpoint falls back
    to its DB-polling tail, so the feature still works locally with zero infrastructure.

Publishing is best-effort and never allowed to affect the decision write path.
"""
from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator, Dict, Optional

from .runtime import redis_client

_CHANNEL = "rulemind:decisions:{0}"


def channel_for(tenant_id: str) -> str:
    return _CHANNEL.format(tenant_id)


def compact_frame(decision: Dict[str, Any]) -> Dict[str, Any]:
    """The small projection the live feed streams (no payload/trace)."""
    return {
        "id": decision.get("id"),
        "policy_id": decision.get("policy_id"),
        "outcome": decision.get("outcome"),
        "source": decision.get("source"),
        "latency_ms": decision.get("latency_ms"),
        "experiment_variant": decision.get("experiment_variant"),
        "created_at": decision.get("created_at"),
    }


def publish_decision(tenant_id: str, decision: Dict[str, Any]) -> None:
    """Best-effort publish of one decision to the tenant's live channel. No-op without Redis,
    and never raises — a fan-out hiccup must not touch the decision write path."""
    if not tenant_id:
        return
    client = redis_client()
    if client is None:
        return
    try:
        client.publish(channel_for(tenant_id), json.dumps(compact_frame(decision)))
    except Exception:
        pass


def has_redis() -> bool:
    return bool(os.getenv("REDIS_URL")) and redis_client() is not None


async def subscribe_decisions(tenant_id: str) -> AsyncIterator[Dict[str, Any]]:
    """Async-yield decisions published to the tenant's channel via redis.asyncio pub/sub.

    Yields a sentinel `{}` roughly once a second when idle so the caller can emit a heartbeat and
    check for client disconnect. Silently returns (empty stream) if Redis is unavailable, so the
    caller falls back to DB polling.
    """
    url = os.getenv("REDIS_URL")
    if not url:
        return
    try:
        import redis.asyncio as aioredis
    except Exception:
        return

    client: Optional[Any] = None
    pubsub = None
    try:
        client = aioredis.from_url(url, decode_responses=True)
        pubsub = client.pubsub()
        await pubsub.subscribe(channel_for(tenant_id))
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message and message.get("type") == "message":
                try:
                    yield json.loads(message["data"])
                except (ValueError, TypeError):
                    continue
            else:
                yield {}  # idle tick -> heartbeat + disconnect check upstream
    finally:
        if pubsub is not None:
            try:
                await pubsub.unsubscribe(channel_for(tenant_id))
                await pubsub.aclose()
            except Exception:
                pass
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass
