from __future__ import annotations

import copy
from typing import Any, Dict

from .auth import verify_hmac_signature
from .executor import PolicyExecutor
from .storage import Storage


class WebhookAuthenticationError(PermissionError):
    pass


def apply_mapping(payload: Dict[str, Any], mapping: Dict[str, Any]) -> Dict[str, Any]:
    if not mapping:
        return copy.deepcopy(payload)
    result = {}
    for key, source in mapping.items():
        if isinstance(source, str):
            result[key] = payload.get(source)
        else:
            result[key] = source
    return result


from typing import Optional


async def trigger_webhook(storage: Storage, webhook_id: str, body: Dict[str, Any], signature: Optional[str] = None) -> Dict[str, Any]:
    webhook = storage.get_webhook(webhook_id, include_secret=True)
    if not webhook or not webhook.get("is_active", False):
        raise ValueError("Webhook not found")
    if webhook.get("secret_hash"):
        import hashlib

        secret = webhook["secret_hash"]
        valid = verify_hmac_signature(secret, json_bytes(body), signature)
        if not valid:
            raise WebhookAuthenticationError("Invalid signature")
    payload = apply_mapping(body, webhook.get("payload_mapping") or {})
    policy = storage.get_policy(webhook["policy_id"], tenant_id=webhook["tenant_id"])
    if not policy:
        raise ValueError("Policy not found")
    executor = PolicyExecutor(storage)
    result = await executor.execute(policy=policy, payload=payload, tenant_id=webhook["tenant_id"], user_id=body.get("user_id"), source="webhook")
    storage.add_audit_event(
        {
            "tenant_id": webhook["tenant_id"],
            "event_type": "webhook_triggered",
            "entity_type": "policy",
            "entity_id": webhook["policy_id"],
            "detail": "Webhook {0} triggered policy {1}".format(webhook_id, webhook["policy_id"]),
            "metadata": {"trigger_type": "webhook"},
        },
        tenant_id=webhook["tenant_id"],
    )
    return {"executionId": result.execution_id, "outcome": result.outcome, "status": result.status, "latencyMs": result.total_latency_ms}


def json_bytes(payload: Dict[str, Any]) -> bytes:
    import json

    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
