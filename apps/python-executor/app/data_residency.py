"""Where this workspace's data is, and where it can go (plan 6.7).

For a self-hosted product, residency is mostly a question of WHERE THE CUSTOMER RUNS IT: every
workspace on an install stores its data in that install's database, in that install's region. What a
procurement review actually needs is the list of places data can LEAVE to -- and that was not
written down anywhere; it was spread across connector configs, webhook subscriptions, AI settings,
the archive sink, the broker and the tracing exporter.

`residency_report` assembles it from the live configuration, so it cannot drift from what the
install will actually do. `RULEMIND_DATA_REGION` is the operator's declaration of where the install
runs (e.g. `in-mumbai-1`, `eu-frankfurt`); it is reported, never inferred.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from . import env

AI_PROVIDER_HOSTS = {
    "anthropic": "api.anthropic.com",
    "openai": "api.openai.com",
    "gemini": "generativelanguage.googleapis.com",
}


def _host(url: Any) -> Optional[str]:
    if not isinstance(url, str) or not url.strip():
        return None
    parsed = urlparse(url.strip() if "://" in url else "https://" + url.strip())
    return parsed.hostname


def _database() -> Dict[str, Any]:
    """Engine and host of the database. Never the credentials."""
    from sqlalchemy.engine import make_url

    raw = env.env_str("DATABASE_URL", "")
    if not raw:
        return {"engine": "sqlite", "host": None, "note": "local SQLite file on the API host"}
    try:
        url = make_url(raw)
    except Exception:  # noqa: BLE001 - a malformed URL is reported, not raised
        return {"engine": "unknown", "host": None}
    return {"engine": url.get_backend_name(), "host": url.host, "database": url.database}


def residency_report(storage: Any, tenant_id: str) -> Dict[str, Any]:
    from .storage import audit_encryption_enabled, decision_encryption_enabled

    egress: List[Dict[str, Any]] = []
    for connector in storage.list_connectors(tenant_id=tenant_id):
        config = connector.get("config") or {}
        host = _host(config.get("base_url") or config.get("webhook_url"))
        if host and not str(config.get("base_url") or "").startswith("simulate:"):
            egress.append({"kind": "connector", "id": connector.get("id"), "host": host,
                           "sends": "decision inputs, per its request mapping",
                           "at_decision_time": bool(config.get("fetch_at_decision"))})
            token_host = _host(config.get("token_url"))
            if token_host and token_host != host:
                egress.append({"kind": "connector_token_endpoint", "id": connector.get("id"), "host": token_host,
                               "sends": "client credentials only"})
    for subscription in storage.list_webhook_subscriptions(tenant_id=tenant_id):
        host = _host(subscription.get("url"))
        if host:
            egress.append({"kind": "webhook_subscription", "id": subscription.get("id"), "host": host,
                           "sends": "decision id, outcome and subject id -- never the payload"})
    ai = storage.get_ai_config_masked(tenant_id=tenant_id)
    if ai.get("enabled"):
        for name, provider in (ai.get("providers") or {}).items():
            if provider.get("configured"):
                egress.append({"kind": "ai_provider", "id": name, "host": AI_PROVIDER_HOSTS.get(name, name),
                               "sends": "prompts built from what an author asks about, which can include decision data"})
    sink = env.env_str("DECISION_ARCHIVE_SINK", "none")
    if sink not in ("", "none", "discard"):
        target = env.env_str("DECISION_ARCHIVE_S3_BUCKET", "") or env.env_str("DECISION_ARCHIVE_S3_ENDPOINT", "")
        egress.append({"kind": "decision_archive", "id": sink, "host": target or None,
                       "region": os.getenv("AWS_REGION") or env.env_str("DECISION_ARCHIVE_S3_REGION", "") or None,
                       "sends": "full decisions past the retention window"})
    brokers = env.env_str("KAFKA_BOOTSTRAP_SERVERS", "")
    if brokers:
        egress.append({"kind": "kafka", "id": env.env_str("RULEMIND_KAFKA_TOPIC", "") or None,
                       "host": ",".join(b.split(":")[0] for b in brokers.split(",") if b.strip()),
                       "sends": "decision events"})
    otel = _host(env.env_str("OTEL_EXPORTER_OTLP_ENDPOINT", ""))
    if otel:
        egress.append({"kind": "tracing", "id": "otlp", "host": otel,
                       "sends": "request spans; payloads are not attached"})

    return {
        "install_region": env.env_str("RULEMIND_DATA_REGION", "") or None,
        "statement": ("Every workspace on this install stores its data in this install's database. "
                      "Data leaves only to the destinations listed under egress."),
        "database": _database(),
        "encryption_at_rest": {"decisions": decision_encryption_enabled(), "audit_log": audit_encryption_enabled()},
        # Whether the database itself keeps this install's workspaces apart (plan 5.5), as found
        # at startup -- not as configured, since a missing role or table owner silently disables it.
        "tenant_isolation": storage.row_security,
        "egress": egress,
        "data_subject_endpoints": {"access": "GET /api/v1/data-subjects/{subject_id}",
                                   "erasure": "POST /api/v1/data-subjects/{subject_id}/erase"},
    }
