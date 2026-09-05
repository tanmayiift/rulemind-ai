"""Outbound webhook subscriptions (parity P03).

The inverse of `/api/v1/webhooks`, which is INBOUND (an endpoint a partner POSTs to in order to
trigger a policy). A subscription says "POST to this URL whenever an event of these types, matching
these filters, happens in this workspace".

WHY THESE ROUTES NEED manage_access AND NOT author. `_WRITE_PREFIX_CAPABILITY` in rbac.py already
puts `/api/v1/settings` and `/api/v1/branding` behind manage_access, and a subscription belongs
with those rather than with the authoring entities: it is standing configuration that sends this
workspace's decision data to a third party, for every matching event, until someone removes it.
A `policy_maker` can already POST to an arbitrary URL from an action step, so this is not about
capability to reach the network -- it is that a subscription is durable egress configuration, and
the role documented as "cannot manage access" should not be able to point the workspace's event
stream at a new destination.

The prefix MUST stay listed in rbac.py. The unknown-write default is fail-closed, so an unmapped
prefix denies every write -- which is the safe direction, but it would deny it for owners too and
the failure would look like a bug rather than a policy.
"""
from __future__ import annotations

import secrets
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import main, webhook_dispatch
from ..logic import now_iso
from ..context import get_current_api_key_id
from ..main import active_role, active_tenant_id

router = APIRouter()


class SubscriptionRequest(BaseModel):
    name: str
    url: str
    event_types: List[str]
    filters: Optional[List[Dict[str, Any]]] = None
    is_active: bool = True
    timeout_ms: Optional[int] = None
    retries: Optional[int] = None


class SubscriptionPatch(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    event_types: Optional[List[str]] = None
    filters: Optional[List[Dict[str, Any]]] = None
    is_active: Optional[bool] = None
    timeout_ms: Optional[int] = None
    retries: Optional[int] = None


def _audit(event_type: str, tenant_id: str, entity_id: str, detail: str,
           metadata: Optional[Dict[str, Any]] = None) -> None:
    """Every mutation is audited WITH THE ACTOR — one of the five definition-of-done items.
    Best effort: an audit failure must not roll back the change the operator asked for."""
    try:
        main.storage.add_audit_event(
            {
                "tenant_id": tenant_id,
                "event_type": event_type,
                "entity_type": "webhook_subscription",
                "entity_id": entity_id,
                "detail": detail,
                "metadata": {**(metadata or {}),
                             "actor_api_key_id": get_current_api_key_id(),
                             "actor_role": active_role()},
            },
            tenant_id=tenant_id,
        )
    except Exception:  # pragma: no cover - audit is best effort
        pass


def _validated(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Reject at authoring time rather than storing a row that silently never fires."""
    try:
        if payload.get("event_types") is not None:
            payload["event_types"] = webhook_dispatch.validate_event_types(payload["event_types"])
        if "filters" in payload:
            payload["filters"] = webhook_dispatch.validate_filters(payload.get("filters"))
    except webhook_dispatch.SubscriptionInvalid as invalid:
        raise HTTPException(status_code=422, detail=str(invalid)) from invalid
    return payload


@router.get("/api/v1/webhook-subscriptions/event-types")
def list_event_types() -> Dict[str, Any]:
    """The closed list, so the UI does not have to hardcode it and drift."""
    return {"event_types": list(webhook_dispatch.EVENT_TYPES)}


@router.get("/api/v1/webhook-subscriptions")
def list_subscriptions(request: Request) -> List[Dict[str, Any]]:
    return main.storage.list_webhook_subscriptions(tenant_id=active_tenant_id(request))


@router.post("/api/v1/webhook-subscriptions")
def create_subscription(body: SubscriptionRequest, request: Request) -> Dict[str, Any]:
    tenant_id = active_tenant_id(request)
    existing = main.storage.list_webhook_subscriptions(tenant_id=tenant_id)
    if len(existing) >= webhook_dispatch.MAX_SUBSCRIPTIONS_PER_TENANT:
        # A bound, for the same reason every other author-supplied fan-out has one: each
        # subscription multiplies the outbound requests one event produces.
        raise HTTPException(
            status_code=422,
            detail="This workspace already has the maximum of {0} webhook subscriptions.".format(
                webhook_dispatch.MAX_SUBSCRIPTIONS_PER_TENANT))
    payload = _validated(body.model_dump())
    # Generated here, never accepted from the caller: a secret the client chose is a secret the
    # client has already had in plaintext somewhere else, and it is returned exactly once below.
    payload["secret"] = secrets.token_urlsafe(32)
    created = main.storage.create_webhook_subscription(payload, tenant_id=tenant_id)
    _audit("webhook_subscription_created", tenant_id, created["id"],
           "Created webhook subscription '{0}' -> {1}".format(body.name, body.url),
           {"event_types": payload["event_types"]})
    # The ONLY response that carries the plaintext secret. Everywhere else it is masked, which is
    # the same contract POST /api/v1/access/keys has for a minted key.
    return {**created, "secret": payload["secret"]}


@router.get("/api/v1/webhook-subscriptions/{subscription_id}")
def get_subscription(subscription_id: str, request: Request) -> Dict[str, Any]:
    found = main.storage.get_webhook_subscription(subscription_id, tenant_id=active_tenant_id(request))
    if not found:
        # 404, not 403, across the tenant boundary: a 403 says "this exists, you may not have it",
        # which turns id enumeration into a workspace census.
        raise HTTPException(status_code=404, detail="Webhook subscription not found.")
    return found


@router.patch("/api/v1/webhook-subscriptions/{subscription_id}")
def update_subscription(subscription_id: str, body: SubscriptionPatch, request: Request) -> Dict[str, Any]:
    tenant_id = active_tenant_id(request)
    patch = _validated({k: v for k, v in body.model_dump().items() if v is not None})
    updated = main.storage.update_webhook_subscription(subscription_id, patch, tenant_id=tenant_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Webhook subscription not found.")
    _audit("webhook_subscription_updated", tenant_id, subscription_id,
           "Updated webhook subscription '{0}'".format(updated.get("name") or subscription_id),
           {"changed": sorted(patch)})
    return updated


@router.delete("/api/v1/webhook-subscriptions/{subscription_id}")
def delete_subscription(subscription_id: str, request: Request) -> Dict[str, Any]:
    tenant_id = active_tenant_id(request)
    if not main.storage.delete_webhook_subscription(subscription_id, tenant_id=tenant_id):
        raise HTTPException(status_code=404, detail="Webhook subscription not found.")
    _audit("webhook_subscription_deleted", tenant_id, subscription_id,
           "Deleted webhook subscription {0}".format(subscription_id))
    return {"deleted": True, "id": subscription_id}


@router.post("/api/v1/webhook-subscriptions/{subscription_id}/test")
async def test_subscription(subscription_id: str, request: Request) -> Dict[str, Any]:
    """Send one synthetic event, synchronously, and report exactly what happened.

    The equivalent of `POST /connectors/{id}/test`, and it exists for the same reason: without it
    the only way to find out that a URL is wrong, unreachable or blocked by the egress policy is
    to wait for a real event and then notice the absence of one delivery among many.
    """
    tenant_id = active_tenant_id(request)
    subscription = main.storage.get_webhook_subscription(
        subscription_id, tenant_id=tenant_id, include_secret=True)
    if not subscription:
        raise HTTPException(status_code=404, detail="Webhook subscription not found.")
    event = {
        "type": (subscription.get("event_types") or ["decision.completed"])[0],
        "tenant_id": tenant_id,
        "created_at": now_iso(),
        "data": {"test": True, "subscription_id": subscription_id},
    }
    # Deliberately bypasses `event_matches`: the operator asked to test THIS subscription, and a
    # filter that excludes the synthetic event should not read as "delivery is broken".
    result = await webhook_dispatch.deliver(
        subscription, event, "dlv_test_{0}".format(uuid.uuid4().hex[:12]))
    _audit("webhook_subscription_tested", tenant_id, subscription_id,
           "Tested webhook subscription: {0}".format(
               "delivered" if result.get("delivered") else result.get("error")),
           {"delivered": bool(result.get("delivered"))})
    return result
