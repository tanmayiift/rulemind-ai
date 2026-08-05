from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import os
import time as _time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from cryptography.fernet import Fernet
from sqlalchemy import case, delete, desc, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import (
    api_key_kid,
    bcrypt_hash,
    bcrypt_verify,
    generate_api_key,
    generate_otp_code,
    generate_session_token,
    key_lookup_hash,
    mask_api_key,
    otp_code_hash,
    session_token_hash,
)
from .context import get_current_tenant_id
from .db import engine_for, session_factory
from .logic import generate_rule_expression_definition, json_dumps, now_iso
from .models import (
    ActionLog,
    ApiKey,
    AuditEvent,
    Base,
    Bundle,
    Connector,
    CronSchedule,
    Decision,
    DecisionTable,
    EmailOutbox,
    EntityHistory,
    ErrorEvent,
    Experiment,
    HostedModel,
    MemberOtp,
    MemberSession,
    PlatformAdminUser,
    Policy,
    Promotion,
    ReportDefinition,
    ReviewTask,
    Rule,
    SchedulerLease,
    Scorecard,
    SdkEvent,
    Setting,
    Tenant,
    Variable,
    Webhook,
    WorkflowExecution,
    WorkspaceMember,
    uuid4_str,
)
from .seed_data import CONNECTORS, DEFAULT_SETTINGS, POLICIES, RULES, SCORECARDS, VARIABLES


SECRET_FIELD_MARKERS = ("token", "secret", "password", "api_key", "apikey", "client_secret", "private_key")

# Verified API keys are cached per-Storage-instance (bcrypt is deliberately
# ~200ms; running it per request caps the whole API at a few req/s). TTL below.
_API_KEY_CACHE_TTL = float(os.getenv("API_KEY_CACHE_TTL", "300"))


def _parse_client_datetime(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp sent by an SDK (e.g. an on-device decision time),
    returning a naive UTC datetime, or None if absent/unparseable."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is not None:
        # Normalize to naive UTC (not naive *local*): stored created_at is naive UTC and
        # serialize_datetime treats naive values as UTC, so the round-trip must too.
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def serialize_datetime(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(tz=None).replace(tzinfo=None)
    return value.replace(microsecond=0).isoformat() + "Z"


def fernet_key() -> bytes:
    raw = os.getenv("RULEMIND_CONFIG_KEY", "rulemind-dev-master-key")
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_config_payload(value: Dict[str, Any]) -> str:
    return Fernet(fernet_key()).encrypt(json.dumps(value).encode("utf-8")).decode("utf-8")


def decrypt_config_payload(value: str) -> Dict[str, Any]:
    if not value:
        return {}
    try:
        return json.loads(Fernet(fernet_key()).decrypt(value.encode("utf-8")).decode("utf-8"))
    except Exception:
        try:
            return json.loads(value)
        except Exception:
            return {}


def decision_encryption_enabled() -> bool:
    """Encrypt decision payloads at rest (default ON). Financial/KYC/health/any-PII inputs must
    not sit in the DB as plaintext. Disable only for a plaintext-required migration."""
    return (os.getenv("DECISION_ENCRYPT_AT_REST", "1") or "1").strip() not in ("0", "false", "no")


def encrypt_decision_field(value: Any) -> Any:
    """Encrypt a decision JSON blob (payload/variables) for storage. Returns a Fernet token
    string when encryption is enabled, else the value unchanged (dev/migration)."""
    if value is None or not decision_encryption_enabled():
        return value
    return Fernet(fernet_key()).encrypt(json_dumps(value).encode("utf-8")).decode("utf-8")


def decrypt_decision_field(value: Any) -> Any:
    """Decrypt a stored decision blob. Handles both encrypted (Fernet token string) and legacy
    plaintext (a JSON object already), so old rows keep reading after encryption is turned on."""
    if not isinstance(value, str):
        return value  # legacy plaintext dict/list, or None
    try:
        return json.loads(Fernet(fernet_key()).decrypt(value.encode("utf-8")).decode("utf-8"))
    except Exception:
        try:
            return json.loads(value)
        except Exception:
            return value


def encrypt_secret_text(value: str) -> str:
    return Fernet(fernet_key()).encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret_text(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return Fernet(fernet_key()).decrypt(value.encode("utf-8")).decode("utf-8")
    except Exception:
        return value


def encrypt_model_blob(blob: bytes) -> bytes:
    """Encrypt a hosted model's raw bytes at rest. The DB (and any dump / backup /
    replica) only ever holds ciphertext — the model's weights are unreadable without
    RULEMIND_CONFIG_KEY. Protects proprietary / black-box model IP."""
    return Fernet(fernet_key()).encrypt(blob)


def decrypt_model_blob(blob: bytes) -> bytes:
    try:
        return Fernet(fernet_key()).decrypt(blob)
    except Exception:
        # Tolerate a pre-encryption blob (defensive; shouldn't occur post-migration).
        return blob


def mask_secret_values(value: Any) -> Any:
    if isinstance(value, dict):
        masked: Dict[str, Any] = {}
        for key, item in value.items():
            if any(marker in key.lower() for marker in SECRET_FIELD_MARKERS):
                masked[key] = "••••••••"
            else:
                masked[key] = mask_secret_values(item)
        return masked
    if isinstance(value, list):
        return [mask_secret_values(item) for item in value]
    return value


class Storage:
    def __init__(self, path: Optional[str] = None):
        self.path = path
        self.engine = engine_for(path)
        self.SessionLocal = session_factory(path)
        Base.metadata.create_all(self.engine)
        self._reconcile_sqlite_columns()
        self.default_tenant_id: Optional[str] = None
        self.default_api_key: Optional[str] = None
        # Per-instance verified-API-key cache (see get_tenant_by_api_key). Keeping
        # it on the instance keeps it correct in prod (Storage is a singleton) and
        # isolated in tests (each Storage/DB gets its own cache).
        self._api_key_cache: Dict[str, Any] = {}
        # Resolved human-login sessions (token_hash -> (result, expiry)). Same
        # rationale as the API-key cache; role is read live so a role change /
        # deactivation takes effect within the TTL (and eagerly, see _invalidate_member).
        self._member_session_cache: Dict[str, Any] = {}
        self.default_admin_email = os.getenv("RULEMIND_ADMIN_EMAIL", "admin@rulemind.local")
        self.default_admin_password = os.getenv("RULEMIND_ADMIN_PASSWORD", "rulemind-admin")
        self.seed_if_empty()

    @contextmanager
    def connect(self) -> Iterator[Session]:
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _tenant_id(self, tenant_id: Optional[str] = None) -> str:
        resolved = tenant_id or get_current_tenant_id() or self.default_tenant_id
        if not resolved:
            raise ValueError("Tenant context is required.")
        return resolved

    def _seed_default_tenant(self, session: Session) -> Tenant:
        plaintext = os.getenv("RULEMIND_DEV_API_KEY", "rm_live_devlocaltenantkey000000000000")
        lookup_hash = key_lookup_hash(plaintext)
        tenant = session.scalar(select(Tenant).where(Tenant.api_key_hash == lookup_hash).limit(1))
        if tenant:
            return tenant
        tenant = session.scalar(select(Tenant).order_by(Tenant.created_at).limit(1))
        if tenant:
            return tenant
        tenant = Tenant(name="Default Tenant", plan="standard", config={"locale": "en-IN"}, is_active=True)
        session.add(tenant)
        session.flush()
        return tenant

    def _seed_default_api_key(self, session: Session, tenant: Tenant) -> None:
        plaintext = os.getenv("RULEMIND_DEV_API_KEY", "rm_live_devlocaltenantkey000000000000")
        lookup_hash = key_lookup_hash(plaintext)
        existing_for_lookup = session.scalar(select(ApiKey).where(ApiKey.lookup_hash == lookup_hash).limit(1))
        if existing_for_lookup:
            self.default_api_key = plaintext
            self.default_tenant_id = existing_for_lookup.tenant_id
            existing_tenant = session.get(Tenant, existing_for_lookup.tenant_id)
            if existing_tenant and existing_tenant.api_key_hash != lookup_hash:
                existing_tenant.api_key_hash = lookup_hash
            return
        api_key = session.scalar(select(ApiKey).where(ApiKey.tenant_id == tenant.id, ApiKey.is_active.is_(True)))
        if api_key:
            self.default_api_key = plaintext
            self.default_tenant_id = tenant.id
            return

        model = ApiKey(
            tenant_id=tenant.id,
            kid=api_key_kid(plaintext),
            masked_key=mask_api_key(plaintext),
            lookup_hash=lookup_hash,
            key_hash=bcrypt_hash(plaintext),
            is_active=True,
        )
        tenant.api_key_hash = lookup_hash
        session.add(model)
        self.default_api_key = plaintext
        self.default_tenant_id = tenant.id

    def _seed_default_admin(self, session: Session) -> None:
        existing = session.scalar(select(PlatformAdminUser).where(PlatformAdminUser.email == self.default_admin_email))
        if existing:
            return
        session.add(
            PlatformAdminUser(
                email=self.default_admin_email,
                name="RuleMind Admin",
                password_hash=bcrypt_hash(self.default_admin_password),
                is_active=True,
            )
        )

    def seed_if_empty(self) -> None:
        with self.connect() as session:
            tenant = self._seed_default_tenant(session)
            self._seed_default_api_key(session, tenant)
            if self.default_tenant_id and self.default_tenant_id != tenant.id:
                tenant = session.get(Tenant, self.default_tenant_id) or tenant
            self._seed_default_admin(session)

            existing_variable = session.scalar(select(Variable).where(Variable.tenant_id == tenant.id).limit(1))
            if existing_variable:
                self._ensure_settings(session, tenant.id)
                self._ensure_seed_inventory(session, tenant.id)
                return

            # Demo inventory (sample connectors/variables/rules/scorecards/policies)
            # is OPT-IN: a fresh clone starts clean so a new customer builds their own
            # via guided onboarding. Set RULEMIND_SEED_DEMO=1 to load the samples (the
            # test suite sets it per-file). Workspace settings always exist regardless.
            if os.getenv("RULEMIND_SEED_DEMO", "0") != "1":
                self._ensure_settings(session, tenant.id)
                return

            now = datetime.utcnow()
            for connector in CONNECTORS:
                session.add(
                    Connector(
                        tenant_id=tenant.id,
                        public_id=connector["id"],
                        name=connector["name"],
                        icon=connector.get("icon"),
                        color=connector.get("color"),
                        description=connector.get("description"),
                        schema_fields=copy.deepcopy(connector.get("schema_paths", [])),
                        sample_payload=copy.deepcopy(connector.get("sample_payload", {})),
                        is_active=bool(connector.get("is_active", True)),
                        encrypted_config=encrypt_config_payload(copy.deepcopy(connector.get("config", {}))),
                        created_at=now,
                        updated_at=now,
                    )
                )

            for variable in VARIABLES:
                last_test_result = {
                    "value": variable["seed_value"],
                    "error": None,
                    "latency_ms": 1.0,
                    "tested_at": now_iso(),
                    "passed": True,
                }
                session.add(
                    Variable(
                        tenant_id=tenant.id,
                        public_id=variable["id"],
                        name=variable["name"],
                        category=variable["category"],
                        source_id=variable["source_id"],
                        code=variable["code"],
                        description=variable.get("description"),
                        status=variable["status"],
                        last_test_result=last_test_result,
                        version=1,
                        created_at=now,
                        updated_at=now,
                    )
                )
                session.add(
                    EntityHistory(
                        tenant_id=tenant.id,
                        entity_type="variable",
                        entity_id=variable["id"],
                        version=1,
                        snapshot={**copy.deepcopy(variable), "last_test_result": last_test_result},
                        created_at=now,
                    )
                )

            variable_lookup = {item["id"]: item for item in VARIABLES}
            for rule in RULES:
                expression = generate_rule_expression_definition(rule, variable_lookup)
                session.add(
                    Rule(
                        tenant_id=tenant.id,
                        public_id=rule["id"],
                        name=rule["name"],
                        nodes=copy.deepcopy(rule.get("nodes")),
                        tree=copy.deepcopy(rule.get("tree")),
                        rule_format=rule.get("rule_format", "v1"),
                        expression=expression,
                        status=rule["status"],
                        last_test_result=None,
                        version=1,
                        created_at=now,
                        updated_at=now,
                    )
                )
                session.add(
                    EntityHistory(
                        tenant_id=tenant.id,
                        entity_type="rule",
                        entity_id=rule["id"],
                        version=1,
                        snapshot={**copy.deepcopy(rule), "expression": expression},
                        created_at=now,
                    )
                )

            for scorecard in SCORECARDS:
                session.add(
                    Scorecard(
                        tenant_id=tenant.id,
                        public_id=scorecard["id"],
                        name=scorecard["name"],
                        base_score=scorecard["base_score"],
                        max_score=scorecard["max_score"],
                        bins=copy.deepcopy(scorecard["bins"]),
                        status=scorecard["status"],
                        last_test_result=None,
                        version=1,
                        created_at=now,
                        updated_at=now,
                    )
                )
                session.add(
                    EntityHistory(
                        tenant_id=tenant.id,
                        entity_type="scorecard",
                        entity_id=scorecard["id"],
                        version=1,
                        snapshot=copy.deepcopy(scorecard),
                        created_at=now,
                    )
                )

            for policy in POLICIES:
                session.add(
                    Policy(
                        tenant_id=tenant.id,
                        public_id=policy["id"],
                        name=policy["name"],
                        trigger=copy.deepcopy(policy.get("trigger")),
                        steps=copy.deepcopy(policy["steps"]),
                        default_outcome=policy.get("defaultOutcome"),
                        status=policy["status"],
                        last_test_result=None,
                        version=1,
                        created_at=now,
                        updated_at=now,
                    )
                )
                session.add(
                    EntityHistory(
                        tenant_id=tenant.id,
                        entity_type="policy",
                        entity_id=policy["id"],
                        version=1,
                        snapshot=copy.deepcopy(policy),
                        created_at=now,
                    )
                )

            self._ensure_settings(session, tenant.id)
            # The session disables autoflush, so persist the freshly inserted seed
            # entities before the inventory backfill checks query for existing rows.
            session.flush()
            self._ensure_seed_inventory(session, tenant.id)

    def _reconcile_sqlite_columns(self) -> None:
        """Add any additive, nullable columns the ORM models declare but an existing SQLite
        table is missing. `create_all` only creates missing *tables*, never ALTERs existing
        ones, so a new nullable column would otherwise be invisible on a dev/test DB until it
        was deleted. SQLite only by design: Postgres/prod schema changes go through Alembic
        migrations (the authoritative record); this is a dev-ergonomics safety net, and it only
        ever adds nullable columns (no backfill, no data loss)."""
        if self.engine.dialect.name != "sqlite":
            return
        from sqlalchemy import inspect as sa_inspect, text

        inspector = sa_inspect(self.engine)
        with self.engine.begin() as conn:
            for table in Base.metadata.sorted_tables:
                if not inspector.has_table(table.name):
                    continue
                existing = {col["name"] for col in inspector.get_columns(table.name)}
                for column in table.columns:
                    if column.name in existing or not column.nullable:
                        continue
                    col_type = column.type.compile(dialect=self.engine.dialect)
                    conn.execute(text('ALTER TABLE "{0}" ADD COLUMN "{1}" {2}'.format(table.name, column.name, col_type)))

    def _ensure_settings(self, session: Session, tenant_id: str) -> None:
        existing = session.scalar(select(Setting).where(Setting.tenant_id == tenant_id))
        if existing:
            return
        session.add(
            Setting(
                tenant_id=tenant_id,
                api_base_url=DEFAULT_SETTINGS["api_base_url"],
                auth_config=copy.deepcopy(DEFAULT_SETTINGS["auth_config"]),
                engine_config=copy.deepcopy(DEFAULT_SETTINGS["engine_config"]),
                source_defaults=copy.deepcopy(DEFAULT_SETTINGS["source_defaults"]),
                audit_retention_days=DEFAULT_SETTINGS["audit_retention_days"],
                theme_mode=DEFAULT_SETTINGS["theme_mode"],
                branding=copy.deepcopy(DEFAULT_SETTINGS["branding"]),
                ai_config=copy.deepcopy(DEFAULT_SETTINGS.get("ai_config", {})),
            )
        )

    def _ensure_seed_inventory(self, session: Session, tenant_id: str) -> None:
        now = datetime.utcnow()
        for connector in CONNECTORS:
            existing = session.scalar(select(Connector).where(Connector.tenant_id == tenant_id, Connector.public_id == connector["id"]))
            if existing:
                continue
            session.add(
                Connector(
                    tenant_id=tenant_id,
                    public_id=connector["id"],
                    name=connector["name"],
                    icon=connector.get("icon"),
                    color=connector.get("color"),
                    description=connector.get("description"),
                    schema_fields=copy.deepcopy(connector.get("schema_paths", [])),
                    sample_payload=copy.deepcopy(connector.get("sample_payload", {})),
                    is_active=bool(connector.get("is_active", True)),
                    encrypted_config=encrypt_config_payload(copy.deepcopy(connector.get("config", {}))),
                    created_at=now,
                    updated_at=now,
                )
            )

        for variable in VARIABLES:
            existing = session.scalar(select(Variable).where(Variable.tenant_id == tenant_id, Variable.public_id == variable["id"]))
            if existing:
                continue
            last_test_result = {
                "value": variable["seed_value"],
                "error": None,
                "latency_ms": 1.0,
                "tested_at": now_iso(),
                "passed": True,
            }
            session.add(
                Variable(
                    tenant_id=tenant_id,
                    public_id=variable["id"],
                    name=variable["name"],
                    category=variable["category"],
                    source_id=variable["source_id"],
                    code=variable["code"],
                    description=variable.get("description"),
                    status=variable["status"],
                    last_test_result=last_test_result,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                EntityHistory(
                    tenant_id=tenant_id,
                    entity_type="variable",
                    entity_id=variable["id"],
                    version=1,
                    snapshot={**copy.deepcopy(variable), "last_test_result": last_test_result},
                    created_at=now,
                )
            )

        variable_lookup = {item["id"]: item for item in VARIABLES}
        for rule in RULES:
            existing = session.scalar(select(Rule).where(Rule.tenant_id == tenant_id, Rule.public_id == rule["id"]))
            if existing:
                continue
            expression = generate_rule_expression_definition(rule, variable_lookup)
            session.add(
                Rule(
                    tenant_id=tenant_id,
                    public_id=rule["id"],
                    name=rule["name"],
                    nodes=copy.deepcopy(rule.get("nodes")),
                    tree=copy.deepcopy(rule.get("tree")),
                    rule_format=rule.get("rule_format", "v2" if rule.get("tree") else "v1"),
                    expression=expression,
                    status=rule["status"],
                    last_test_result=None,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                EntityHistory(
                    tenant_id=tenant_id,
                    entity_type="rule",
                    entity_id=rule["id"],
                    version=1,
                    snapshot={**copy.deepcopy(rule), "expression": expression},
                    created_at=now,
                )
            )

        for scorecard in SCORECARDS:
            existing = session.scalar(select(Scorecard).where(Scorecard.tenant_id == tenant_id, Scorecard.public_id == scorecard["id"]))
            if existing:
                continue
            session.add(
                Scorecard(
                    tenant_id=tenant_id,
                    public_id=scorecard["id"],
                    name=scorecard["name"],
                    base_score=scorecard["base_score"],
                    max_score=scorecard["max_score"],
                    bins=copy.deepcopy(scorecard["bins"]),
                    status=scorecard["status"],
                    last_test_result=None,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                EntityHistory(
                    tenant_id=tenant_id,
                    entity_type="scorecard",
                    entity_id=scorecard["id"],
                    version=1,
                    snapshot=copy.deepcopy(scorecard),
                    created_at=now,
                )
            )

        for policy in POLICIES:
            existing = session.scalar(select(Policy).where(Policy.tenant_id == tenant_id, Policy.public_id == policy["id"]))
            if existing:
                continue
            session.add(
                Policy(
                    tenant_id=tenant_id,
                    public_id=policy["id"],
                    name=policy["name"],
                    trigger=copy.deepcopy(policy.get("trigger")),
                    steps=copy.deepcopy(policy["steps"]),
                    default_outcome=policy.get("defaultOutcome"),
                    status=policy["status"],
                    last_test_result=None,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                EntityHistory(
                    tenant_id=tenant_id,
                    entity_type="policy",
                    entity_id=policy["id"],
                    version=1,
                    snapshot=copy.deepcopy(policy),
                    created_at=now,
                )
            )

    def _connector_to_dict(self, model: Connector, include_secrets: bool = False) -> Dict[str, Any]:
        config = decrypt_config_payload(model.encrypted_config)
        return {
            "id": model.public_id,
            "name": model.name,
            "icon": model.icon,
            "color": model.color,
            "description": model.description,
            "schema_paths": copy.deepcopy(model.schema_fields or []),
            "sample_payload": copy.deepcopy(model.sample_payload or {}),
            "is_active": bool(model.is_active),
            "config": copy.deepcopy(config) if include_secrets else mask_secret_values(config),
            "created_at": serialize_datetime(model.created_at),
            "updated_at": serialize_datetime(model.updated_at),
        }

    @staticmethod
    def _variable_to_dict(model: Variable) -> Dict[str, Any]:
        return {
            "id": model.public_id,
            "name": model.name,
            "category": model.category,
            "source_id": model.source_id,
            "code": model.code,
            "description": model.description,
            "status": model.status,
            "last_test_result": copy.deepcopy(model.last_test_result),
            "version": model.version,
            "created_at": serialize_datetime(model.created_at),
            "updated_at": serialize_datetime(model.updated_at),
        }

    @staticmethod
    def _rule_to_dict(model: Rule) -> Dict[str, Any]:
        return {
            "id": model.public_id,
            "name": model.name,
            "nodes": copy.deepcopy(model.nodes or []),
            "tree": copy.deepcopy(model.tree),
            "rule_format": model.rule_format if model.rule_format else ("v2" if model.tree else "v1"),
            "expression": model.expression,
            "status": model.status,
            "last_test_result": copy.deepcopy(model.last_test_result),
            "version": model.version,
            "created_at": serialize_datetime(model.created_at),
            "updated_at": serialize_datetime(model.updated_at),
        }

    @staticmethod
    def _scorecard_to_dict(model: Scorecard) -> Dict[str, Any]:
        return {
            "id": model.public_id,
            "name": model.name,
            "base_score": model.base_score,
            "max_score": model.max_score,
            "bins": copy.deepcopy(model.bins or []),
            "status": model.status,
            "last_test_result": copy.deepcopy(model.last_test_result),
            "version": model.version,
            "created_at": serialize_datetime(model.created_at),
            "updated_at": serialize_datetime(model.updated_at),
        }

    @staticmethod
    def _policy_to_dict(model: Policy) -> Dict[str, Any]:
        return {
            "id": model.public_id,
            "name": model.name,
            "trigger": copy.deepcopy(model.trigger),
            "steps": copy.deepcopy(model.steps or []),
            "defaultOutcome": model.default_outcome,
            "status": model.status,
            "lifecycle_status": getattr(model, "lifecycle_status", "draft") or "draft",
            "last_test_result": copy.deepcopy(model.last_test_result),
            "version": model.version,
            "created_at": serialize_datetime(model.created_at),
            "updated_at": serialize_datetime(model.updated_at),
        }

    @staticmethod
    def _decision_to_dict(model: Decision) -> Dict[str, Any]:
        return {
            "id": model.id,
            "policy_id": model.policy_id,
            "payload": copy.deepcopy(decrypt_decision_field(model.payload_preview) or {}),
            "payload_hash": model.payload_hash,
            "computed_variables": copy.deepcopy(decrypt_decision_field(model.computed_variables) or {}),
            "rule_results": copy.deepcopy(model.rule_results or []),
            "scorecard_result": copy.deepcopy(model.scorecard_result),
            "trace": copy.deepcopy(model.trace or []),
            "outcome": model.outcome,
            "latency_ms": model.latency_ms,
            "source": model.source,
            "sdk_version": model.sdk_version,
            "experiment_id": model.experiment_id,
            "experiment_variant": model.experiment_variant,
            "created_at": serialize_datetime(model.created_at),
        }

    @staticmethod
    def _error_event_to_dict(model: ErrorEvent) -> Dict[str, Any]:
        return {
            "id": model.id,
            "tenant_id": model.tenant_id,
            "scope": model.scope,
            "entity_type": model.entity_type,
            "entity_id": model.entity_id,
            "stage": model.stage,
            "message": model.message,
            "details": copy.deepcopy(model.details_json or {}),
            "created_at": serialize_datetime(model.created_at),
        }

    @staticmethod
    def _promotion_to_dict(model: Promotion) -> Dict[str, Any]:
        return {
            "id": model.id,
            "tenant_id": model.tenant_id,
            "entity_type": model.entity_type,
            "entity_id": model.entity_id,
            "from_status": model.from_status,
            "to_status": model.to_status,
            "promoted_by": model.promoted_by,
            "reason": model.reason,
            "created_at": serialize_datetime(model.created_at),
        }

    @staticmethod
    def _audit_event_to_dict(model: AuditEvent) -> Dict[str, Any]:
        return {
            "id": model.id,
            "tenant_id": model.tenant_id,
            "event_type": model.event_type,
            "entity_type": model.entity_type,
            "entity_id": model.entity_id,
            "detail": model.detail,
            "metadata": copy.deepcopy(model.metadata_json or {}),
            "user_id": model.user_id,
            "ip_address": model.ip_address,
            "created_at": serialize_datetime(model.created_at),
        }

    @staticmethod
    def _bundle_to_dict(model: Bundle) -> Dict[str, Any]:
        return {
            "id": model.id,
            "version": model.version,
            "content": copy.deepcopy(model.content or {}),
            "encrypted_content": model.encrypted_content,
            "encrypted_key": model.encrypted_key,
            "signature": model.signature,
            "checksum": model.checksum,
            "superseded": model.superseded,
            "compiled_at": serialize_datetime(model.compiled_at),
            "expires_at": serialize_datetime(model.expires_at),
        }

    def list_connectors(self, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            rows = session.scalars(select(Connector).where(Connector.tenant_id == resolved).order_by(Connector.created_at)).all()
            return [self._connector_to_dict(row) for row in rows]

    def get_connector(self, connector_id: str, include_secrets: bool = False, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            model = session.scalar(select(Connector).where(Connector.tenant_id == resolved, Connector.public_id == connector_id))
            return self._connector_to_dict(model, include_secrets=include_secrets) if model else None

    def create_connector(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id or payload.get("tenant_id"))
        now = datetime.utcnow()
        with self.connect() as session:
            session.add(
                Connector(
                    tenant_id=resolved,
                    public_id=payload["id"],
                    name=payload["name"],
                    icon=payload.get("icon"),
                    color=payload.get("color"),
                    description=payload.get("description"),
                    schema_fields=copy.deepcopy(payload.get("schema_paths", [])),
                    sample_payload=copy.deepcopy(payload.get("sample_payload", {})),
                    is_active=bool(payload.get("is_active", True)),
                    encrypted_config=encrypt_config_payload(copy.deepcopy(payload.get("config", {}))),
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                EntityHistory(
                    tenant_id=resolved,
                    entity_type="connector",
                    entity_id=payload["id"],
                    version=1,
                    snapshot=copy.deepcopy(payload),
                    created_at=now,
                )
            )
        return self.get_connector(payload["id"], tenant_id=resolved)

    def update_connector(self, connector_id: str, patch: Dict[str, Any], tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        current = self.get_connector(connector_id, include_secrets=True, tenant_id=resolved)
        if not current:
            return None
        next_value = copy.deepcopy(current)
        next_value.update({key: value for key, value in patch.items() if value is not None})
        with self.connect() as session:
            model = session.scalar(select(Connector).where(Connector.tenant_id == resolved, Connector.public_id == connector_id))
            if not model:
                return None
            model.name = next_value["name"]
            model.icon = next_value.get("icon")
            model.color = next_value.get("color")
            model.description = next_value.get("description")
            model.schema_fields = copy.deepcopy(next_value.get("schema_paths", []))
            model.sample_payload = copy.deepcopy(next_value.get("sample_payload", {}))
            model.is_active = bool(next_value.get("is_active", True))
            model.encrypted_config = encrypt_config_payload(copy.deepcopy(next_value.get("config", {})))
            model.updated_at = datetime.utcnow()
        return self.get_connector(connector_id, tenant_id=resolved)

    def delete_connector(self, connector_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        current = self.get_connector(connector_id, tenant_id=resolved)
        if not current:
            return None
        with self.connect() as session:
            model = session.scalar(select(Connector).where(Connector.tenant_id == resolved, Connector.public_id == connector_id))
            if model:
                session.delete(model)
        return current

    def _query_by_public_id(self, session: Session, model_type, public_id: str, tenant_id: str):
        return session.scalar(select(model_type).where(model_type.tenant_id == tenant_id, model_type.public_id == public_id))

    def list_variables(self, source_id: Optional[str] = None, status: Optional[str] = None, category: Optional[str] = None, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            stmt = select(Variable).where(Variable.tenant_id == resolved)
            if source_id:
                stmt = stmt.where(Variable.source_id == source_id)
            if status:
                stmt = stmt.where(Variable.status == status)
            if category:
                stmt = stmt.where(Variable.category == category)
            rows = session.scalars(stmt.order_by(Variable.name)).all()
            return [self._variable_to_dict(row) for row in rows]

    def get_variable(self, variable_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = self._query_by_public_id(session, Variable, variable_id, resolved)
            return self._variable_to_dict(row) if row else None

    def create_variable(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id or payload.get("tenant_id"))
        now = datetime.utcnow()
        snapshot = copy.deepcopy(payload)
        with self.connect() as session:
            model = Variable(
                tenant_id=resolved,
                public_id=payload["id"],
                name=payload["name"],
                category=payload["category"],
                source_id=payload["source_id"],
                code=payload["code"],
                description=payload.get("description"),
                status=payload.get("status", "dev"),
                last_test_result=copy.deepcopy(payload.get("last_test_result")),
                version=int(payload.get("version", 1)),
                created_at=now,
                updated_at=now,
            )
            session.add(model)
            session.add(
                EntityHistory(
                    tenant_id=resolved,
                    entity_type="variable",
                    entity_id=payload["id"],
                    version=int(payload.get("version", 1)),
                    snapshot=snapshot,
                    created_at=now,
                )
            )
        return self.get_variable(payload["id"], tenant_id=resolved)

    def update_variable(self, variable_id: str, patch: Dict[str, Any], bump_version: bool = True, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        current = self.get_variable(variable_id, tenant_id=resolved)
        if not current:
            return None
        next_value = copy.deepcopy(current)
        next_value.update({key: value for key, value in patch.items() if value is not None})
        next_value["version"] = current["version"] + 1 if bump_version and patch else current["version"]
        with self.connect() as session:
            model = self._query_by_public_id(session, Variable, variable_id, resolved)
            if not model:
                return None
            model.name = next_value["name"]
            model.category = next_value["category"]
            model.source_id = next_value["source_id"]
            model.code = next_value["code"]
            model.description = next_value.get("description")
            model.status = next_value["status"]
            model.last_test_result = copy.deepcopy(next_value.get("last_test_result"))
            model.version = next_value["version"]
            model.updated_at = datetime.utcnow()
            if bump_version:
                session.add(
                    EntityHistory(
                        tenant_id=resolved,
                        entity_type="variable",
                        entity_id=variable_id,
                        version=next_value["version"],
                        snapshot=copy.deepcopy(next_value),
                    )
                )
        return self.get_variable(variable_id, tenant_id=resolved)

    def delete_variable(self, variable_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        current = self.get_variable(variable_id, tenant_id=resolved)
        if not current:
            return None
        with self.connect() as session:
            model = self._query_by_public_id(session, Variable, variable_id, resolved)
            if model:
                session.delete(model)
        return current

    def list_rules(self, status: Optional[str] = None, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            stmt = select(Rule).where(Rule.tenant_id == resolved)
            if status:
                stmt = stmt.where(Rule.status == status)
            rows = session.scalars(stmt.order_by(Rule.name)).all()
            return [self._rule_to_dict(row) for row in rows]

    def get_rule(self, rule_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = self._query_by_public_id(session, Rule, rule_id, resolved)
            return self._rule_to_dict(row) if row else None

    def create_rule(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id or payload.get("tenant_id"))
        now = datetime.utcnow()
        with self.connect() as session:
            session.add(
                Rule(
                    tenant_id=resolved,
                    public_id=payload["id"],
                    name=payload["name"],
                    nodes=copy.deepcopy(payload.get("nodes")),
                    tree=copy.deepcopy(payload.get("tree")),
                    rule_format=payload.get("rule_format", "v1"),
                    expression=payload.get("expression"),
                    status=payload.get("status", "dev"),
                    last_test_result=copy.deepcopy(payload.get("last_test_result")),
                    version=int(payload.get("version", 1)),
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                EntityHistory(
                    tenant_id=resolved,
                    entity_type="rule",
                    entity_id=payload["id"],
                    version=int(payload.get("version", 1)),
                    snapshot=copy.deepcopy(payload),
                    created_at=now,
                )
            )
        return self.get_rule(payload["id"], tenant_id=resolved)

    def update_rule(self, rule_id: str, patch: Dict[str, Any], bump_version: bool = True, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        current = self.get_rule(rule_id, tenant_id=resolved)
        if not current:
            return None
        next_value = copy.deepcopy(current)
        next_value.update({key: value for key, value in patch.items() if value is not None})
        next_value["version"] = current["version"] + 1 if bump_version and patch else current["version"]
        with self.connect() as session:
            model = self._query_by_public_id(session, Rule, rule_id, resolved)
            if not model:
                return None
            model.name = next_value["name"]
            model.nodes = copy.deepcopy(next_value.get("nodes"))
            model.tree = copy.deepcopy(next_value.get("tree"))
            model.rule_format = next_value.get("rule_format", "v1")
            model.expression = next_value.get("expression")
            model.status = next_value["status"]
            model.last_test_result = copy.deepcopy(next_value.get("last_test_result"))
            model.version = next_value["version"]
            model.updated_at = datetime.utcnow()
            if bump_version:
                session.add(
                    EntityHistory(
                        tenant_id=resolved,
                        entity_type="rule",
                        entity_id=rule_id,
                        version=next_value["version"],
                        snapshot=copy.deepcopy(next_value),
                    )
                )
        return self.get_rule(rule_id, tenant_id=resolved)

    def delete_rule(self, rule_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        current = self.get_rule(rule_id, tenant_id=resolved)
        if not current:
            return None
        with self.connect() as session:
            model = self._query_by_public_id(session, Rule, rule_id, resolved)
            if model:
                session.delete(model)
        return current

    def list_scorecards(self, status: Optional[str] = None, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            stmt = select(Scorecard).where(Scorecard.tenant_id == resolved)
            if status:
                stmt = stmt.where(Scorecard.status == status)
            rows = session.scalars(stmt.order_by(Scorecard.name)).all()
            return [self._scorecard_to_dict(row) for row in rows]

    def get_scorecard(self, scorecard_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = self._query_by_public_id(session, Scorecard, scorecard_id, resolved)
            return self._scorecard_to_dict(row) if row else None

    def create_scorecard(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id or payload.get("tenant_id"))
        now = datetime.utcnow()
        with self.connect() as session:
            session.add(
                Scorecard(
                    tenant_id=resolved,
                    public_id=payload["id"],
                    name=payload["name"],
                    base_score=int(payload.get("base_score", 300)),
                    max_score=int(payload.get("max_score", 900)),
                    bins=copy.deepcopy(payload.get("bins", [])),
                    status=payload.get("status", "dev"),
                    last_test_result=copy.deepcopy(payload.get("last_test_result")),
                    version=int(payload.get("version", 1)),
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                EntityHistory(
                    tenant_id=resolved,
                    entity_type="scorecard",
                    entity_id=payload["id"],
                    version=int(payload.get("version", 1)),
                    snapshot=copy.deepcopy(payload),
                    created_at=now,
                )
            )
        return self.get_scorecard(payload["id"], tenant_id=resolved)

    def update_scorecard(self, scorecard_id: str, patch: Dict[str, Any], bump_version: bool = True, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        current = self.get_scorecard(scorecard_id, tenant_id=resolved)
        if not current:
            return None
        next_value = copy.deepcopy(current)
        next_value.update({key: value for key, value in patch.items() if value is not None})
        next_value["version"] = current["version"] + 1 if bump_version and patch else current["version"]
        with self.connect() as session:
            model = self._query_by_public_id(session, Scorecard, scorecard_id, resolved)
            if not model:
                return None
            model.name = next_value["name"]
            model.base_score = int(next_value.get("base_score", 300))
            model.max_score = int(next_value.get("max_score", 900))
            model.bins = copy.deepcopy(next_value.get("bins", []))
            model.status = next_value["status"]
            model.last_test_result = copy.deepcopy(next_value.get("last_test_result"))
            model.version = next_value["version"]
            model.updated_at = datetime.utcnow()
            if bump_version:
                session.add(
                    EntityHistory(
                        tenant_id=resolved,
                        entity_type="scorecard",
                        entity_id=scorecard_id,
                        version=next_value["version"],
                        snapshot=copy.deepcopy(next_value),
                    )
                )
        return self.get_scorecard(scorecard_id, tenant_id=resolved)

    def delete_scorecard(self, scorecard_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        current = self.get_scorecard(scorecard_id, tenant_id=resolved)
        if not current:
            return None
        with self.connect() as session:
            model = self._query_by_public_id(session, Scorecard, scorecard_id, resolved)
            if model:
                session.delete(model)
        return current

    def list_policies(self, status: Optional[str] = None, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            stmt = select(Policy).where(Policy.tenant_id == resolved)
            if status:
                stmt = stmt.where(Policy.status == status)
            rows = session.scalars(stmt.order_by(Policy.name)).all()
            return [self._policy_to_dict(row) for row in rows]

    def get_policy(self, policy_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = self._query_by_public_id(session, Policy, policy_id, resolved)
            return self._policy_to_dict(row) if row else None

    def create_policy(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id or payload.get("tenant_id"))
        now = datetime.utcnow()
        with self.connect() as session:
            session.add(
                Policy(
                    tenant_id=resolved,
                    public_id=payload["id"],
                    name=payload["name"],
                    trigger=copy.deepcopy(payload.get("trigger")),
                    steps=copy.deepcopy(payload.get("steps", [])),
                    default_outcome=payload.get("defaultOutcome"),
                    status=payload.get("status", "dev"),
                    last_test_result=copy.deepcopy(payload.get("last_test_result")),
                    version=int(payload.get("version", 1)),
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                EntityHistory(
                    tenant_id=resolved,
                    entity_type="policy",
                    entity_id=payload["id"],
                    version=int(payload.get("version", 1)),
                    snapshot=copy.deepcopy(payload),
                    created_at=now,
                )
            )
        return self.get_policy(payload["id"], tenant_id=resolved)

    def update_policy(self, policy_id: str, patch: Dict[str, Any], bump_version: bool = True, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        current = self.get_policy(policy_id, tenant_id=resolved)
        if not current:
            return None
        next_value = copy.deepcopy(current)
        next_value.update({key: value for key, value in patch.items() if value is not None})
        next_value["version"] = current["version"] + 1 if bump_version and patch else current["version"]
        with self.connect() as session:
            model = self._query_by_public_id(session, Policy, policy_id, resolved)
            if not model:
                return None
            model.name = next_value["name"]
            model.trigger = copy.deepcopy(next_value.get("trigger"))
            model.steps = copy.deepcopy(next_value.get("steps", []))
            model.default_outcome = next_value.get("defaultOutcome")
            model.status = next_value["status"]
            model.lifecycle_status = next_value.get("lifecycle_status", getattr(model, "lifecycle_status", "draft")) or "draft"
            model.last_test_result = copy.deepcopy(next_value.get("last_test_result"))
            model.version = next_value["version"]
            model.updated_at = datetime.utcnow()
            if bump_version:
                session.add(
                    EntityHistory(
                        tenant_id=resolved,
                        entity_type="policy",
                        entity_id=policy_id,
                        version=next_value["version"],
                        snapshot=copy.deepcopy(next_value),
                    )
                )
        return self.get_policy(policy_id, tenant_id=resolved)

    def delete_policy(self, policy_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        current = self.get_policy(policy_id, tenant_id=resolved)
        if not current:
            return None
        with self.connect() as session:
            model = self._query_by_public_id(session, Policy, policy_id, resolved)
            if model:
                session.delete(model)
        return current

    def add_decision(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id or payload.get("tenant_id"))
        preview = copy.deepcopy(payload.get("payload", {}))
        payload_hash = payload.get("payload_hash") or hashlib.sha256(json_dumps(preview).encode("utf-8")).hexdigest()
        with self.connect() as session:
            decision = Decision(
                id=payload.get("id") or uuid4_str(),
                tenant_id=resolved,
                policy_id=payload.get("policy_id"),
                payload_hash=payload_hash,
                payload_preview=encrypt_decision_field(preview),
                computed_variables=encrypt_decision_field(copy.deepcopy(payload.get("computed_variables", {}))),
                rule_results=copy.deepcopy(payload.get("rule_results", [])),
                scorecard_result=copy.deepcopy(payload.get("scorecard_result")),
                trace=copy.deepcopy(payload.get("trace", [])),
                outcome=payload["outcome"],
                latency_ms=int(payload.get("latency_ms", 0)),
                source=payload.get("source", "api"),
                sdk_version=payload.get("sdk_version"),
                experiment_id=payload.get("experiment_id"),
                experiment_variant=payload.get("experiment_variant"),
                created_at=datetime.utcnow(),
            )
            session.add(decision)
            session.flush()
            result = self._decision_to_dict(decision)
        # Best-effort real-time fan-out to the live SSE feed (no-op without Redis; never raises).
        from . import decision_bus

        decision_bus.publish_decision(resolved, result)
        return result

    def add_decisions_batch(self, decisions: List[Dict[str, Any]], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """Idempotently ingest a batch of (device-originated) decisions.

        Dedupe is by the client-supplied stable ``id``: a decision whose id already
        exists — or repeats within the same batch — is acknowledged but NOT re-inserted,
        so an at-least-once client retry never double-counts (same guarantee as the
        /decide single-log fix). Every id in the batch is returned in ``acked`` so the
        device can safely clear its local outbox for exactly those rows.

        Returns {received, inserted, duplicates, acked:[id,...]}.
        """
        resolved = self._tenant_id(tenant_id)
        # Client ids are required for idempotency; a decision without one gets a server
        # id and is always inserted (can't be deduped, but also can't be safely retried).
        client_ids = [str(d["id"]) for d in decisions if d.get("id")]
        acked: List[str] = []
        inserted = 0
        duplicates = 0
        with self.connect() as session:
            existing: set = set()
            if client_ids:
                existing = set(session.scalars(select(Decision.id).where(Decision.id.in_(client_ids))).all())
            seen: set = set()
            for record in decisions:
                did = str(record.get("id") or uuid4_str())
                acked.append(did)
                if did in existing or did in seen:
                    duplicates += 1
                    continue
                seen.add(did)
                preview = copy.deepcopy(record.get("payload", {}))
                payload_hash = record.get("payload_hash") or hashlib.sha256(json_dumps(preview).encode("utf-8")).hexdigest()
                session.add(Decision(
                    id=did,
                    tenant_id=resolved,
                    policy_id=record.get("policy_id") or record.get("policyId"),
                    payload_hash=payload_hash,
                    payload_preview=encrypt_decision_field(preview),
                    computed_variables=encrypt_decision_field(copy.deepcopy(record.get("computed_variables", {}))),
                    rule_results=copy.deepcopy(record.get("rule_results", [])),
                    scorecard_result=copy.deepcopy(record.get("scorecard_result")),
                    trace=copy.deepcopy(record.get("trace", [])),
                    outcome=record.get("outcome", "pending"),
                    latency_ms=int(record.get("latency_ms", record.get("latencyMs", 0)) or 0),
                    source=record.get("source", "on_device"),
                    sdk_version=record.get("sdk_version") or record.get("sdkVersion"),
                    experiment_id=record.get("experiment_id") or record.get("experimentId"),
                    experiment_variant=record.get("experiment_variant") or record.get("experimentVariant"),
                    # Preserve the on-device decision time when supplied.
                    created_at=_parse_client_datetime(record.get("created_at") or record.get("createdAt")) or datetime.utcnow(),
                ))
                inserted += 1
        return {"received": len(decisions), "inserted": inserted, "duplicates": duplicates, "acked": acked}

    def list_decisions(self, tenant_id: Optional[str] = None, limit: int = 200, offset: int = 0) -> List[Dict[str, Any]]:
        from . import decision_log
        decision_log.flush()  # read-after-write: see any just-submitted async writes
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            # Bounded by default: the decisions table grows without limit in
            # production (every /decide writes a row), so an unpaged fetch would
            # load tens of thousands of full-context rows and time out / OOM.
            query = select(Decision).where(Decision.tenant_id == resolved).order_by(desc(Decision.created_at)).limit(max(1, min(limit, 1000))).offset(max(0, offset))
            rows = session.scalars(query).all()
            return [self._decision_to_dict(row) for row in rows]

    def count_decisions(self, tenant_id: Optional[str] = None) -> int:
        from . import decision_log
        decision_log.flush()
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            return int(session.scalar(select(func.count()).select_from(Decision).where(Decision.tenant_id == resolved)) or 0)

    def iter_decisions_window(
        self,
        tenant_id: Optional[str] = None,
        since: Optional[datetime] = None,
        max_rows: int = 100000,
        page_size: int = 2000,
    ) -> Tuple[List[Dict[str, Any]], bool]:
        """Fetch decisions within a time window, paginated at the DB level so a
        report is complete (no silent 1000-row truncation) while memory stays
        bounded. The date filter is pushed into the query; returns
        (decisions, truncated) where truncated=True means max_rows was hit."""
        from . import decision_log
        decision_log.flush()
        resolved = self._tenant_id(tenant_id)
        collected: List[Dict[str, Any]] = []
        truncated = False
        with self.connect() as session:
            base = select(Decision).where(Decision.tenant_id == resolved)
            if since is not None:
                base = base.where(Decision.created_at >= since)
            base = base.order_by(desc(Decision.created_at))
            offset = 0
            while len(collected) < max_rows:
                rows = session.scalars(base.limit(page_size).offset(offset)).all()
                if not rows:
                    break
                for row in rows:
                    collected.append(self._decision_to_dict(row))
                    if len(collected) >= max_rows:
                        truncated = True
                        break
                if len(rows) < page_size:
                    break
                offset += page_size
        return collected, truncated

    def decisions_after(
        self,
        tenant_id: Optional[str] = None,
        after: Optional[datetime] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Return decisions created strictly after `after`, oldest-first.

        Backs the live SSE feed: each poll asks for rows newer than the last one it saw, so
        the stream advances a monotonic cursor without re-sending. `after=None` returns the
        most recent `limit` rows (flipped to oldest-first) as the stream's opening backlog.
        The (tenant_id, created_at) index keeps each poll cheap even as the table grows.
        """
        from . import decision_log
        decision_log.flush()  # read-after-write: see just-submitted async writes
        resolved = self._tenant_id(tenant_id)
        def _with_cursor(row: Decision) -> Dict[str, Any]:
            # Carry the full-precision created_at alongside the (second-truncated) serialized
            # form so the SSE loop advances a precise cursor and never re-sends a same-second row.
            data = self._decision_to_dict(row)
            data["_created_at_raw"] = row.created_at
            return data

        with self.connect() as session:
            query = select(Decision).where(Decision.tenant_id == resolved)
            if after is not None:
                query = query.where(Decision.created_at > after).order_by(Decision.created_at).limit(max(1, min(limit, 1000)))
                rows = session.scalars(query).all()
                return [_with_cursor(row) for row in rows]
            # Opening backlog: newest N, returned oldest-first so the cursor advances forward.
            query = query.order_by(desc(Decision.created_at)).limit(max(1, min(limit, 1000)))
            rows = list(session.scalars(query).all())
            rows.reverse()
            return [_with_cursor(row) for row in rows]

    def experiment_variant_rollup(
        self,
        tenant_id: Optional[str],
        experiment_id: str,
    ) -> Dict[str, Dict[str, Any]]:
        """Exact per-variant decision aggregates for ONE A/B experiment, computed in SQL over
        ALL of that experiment's decisions (no 200-row cap). Returns {variant_id: {users,
        approved, rejected, reviewed, latency_sum}}. Scoped by experiment_id — not by variant
        id, which repeats across experiments (two experiments both have a "champion") and would
        otherwise cross-count."""
        if not experiment_id:
            return {}
        resolved = self._tenant_id(tenant_id)
        approved = func.sum(case((Decision.outcome == "approve", 1), else_=0))
        rejected = func.sum(case((Decision.outcome == "reject", 1), else_=0))
        reviewed = func.sum(case((Decision.outcome == "review", 1), else_=0))
        with self.connect() as session:
            rows = session.execute(
                select(
                    Decision.experiment_variant,
                    func.count().label("users"),
                    approved.label("approved"),
                    rejected.label("rejected"),
                    reviewed.label("reviewed"),
                    func.coalesce(func.sum(Decision.latency_ms), 0).label("latency_sum"),
                )
                .where(Decision.tenant_id == resolved, Decision.experiment_id == experiment_id)
                .group_by(Decision.experiment_variant)
            ).all()
        return {
            str(row.experiment_variant): {
                "users": int(row.users or 0),
                "approved": int(row.approved or 0),
                "rejected": int(row.rejected or 0),
                "reviewed": int(row.reviewed or 0),
                "latency_sum": int(row.latency_sum or 0),
            }
            for row in rows
        }

    def decision_facts(
        self,
        tenant_id: Optional[str] = None,
        since: Optional[datetime] = None,
        max_rows: int = 200_000,
        page_size: int = 5_000,
    ) -> List[Dict[str, Any]]:
        """Lightweight, column-projected decision rows for analytics — only the fields the
        dashboards need (no payload/trace/variables), paginated at the DB level within a time
        window. Replaces the 200-row `list_decisions` cap in the analytics path so percentiles,
        timeseries, and rollups are computed over the full (windowed) set without loading heavy
        JSON columns or exhausting memory."""
        from . import decision_log
        decision_log.flush()
        resolved = self._tenant_id(tenant_id)
        collected: List[Dict[str, Any]] = []
        with self.connect() as session:
            base = select(
                Decision.created_at,
                Decision.outcome,
                Decision.source,
                Decision.latency_ms,
                Decision.policy_id,
                Decision.experiment_variant,
            ).where(Decision.tenant_id == resolved)
            if since is not None:
                base = base.where(Decision.created_at >= since)
            base = base.order_by(desc(Decision.created_at))
            offset = 0
            while len(collected) < max_rows:
                rows = session.execute(base.limit(page_size).offset(offset)).all()
                if not rows:
                    break
                for row in rows:
                    collected.append(
                        {
                            "created_at": serialize_datetime(row.created_at),
                            "outcome": row.outcome,
                            "source": row.source,
                            "latency_ms": row.latency_ms,
                            "policy_id": row.policy_id,
                            "experiment_variant": row.experiment_variant,
                        }
                    )
                    if len(collected) >= max_rows:
                        break
                if len(rows) < page_size:
                    break
                offset += page_size
        return collected

    def archive_and_purge_decisions(
        self,
        tenant_id: Optional[str],
        older_than: datetime,
        archiver,
        batch_size: int = 1000,
        max_batches: int = 10_000,
    ) -> int:
        """Move decisions created before `older_than` out of the hot DB into `archiver`, then
        delete them. Archive first, purge only on success — so a sink failure never loses data.
        The archive network write happens OUTSIDE any DB transaction (batches are read, then
        written, then deleted in separate short transactions) so it never holds row locks."""
        resolved = self._tenant_id(tenant_id)
        total = 0
        for _ in range(max_batches):
            with self.connect() as session:
                rows = session.scalars(
                    select(Decision)
                    .where(Decision.tenant_id == resolved, Decision.created_at < older_than)
                    .order_by(Decision.created_at)
                    .limit(batch_size)
                ).all()
                records = []
                ids = []
                for row in rows:
                    record = self._decision_to_dict(row)
                    record["tenant_id"] = resolved
                    records.append(record)
                    ids.append(row.id)
            if not records:
                break
            archiver.write(records)  # raises -> we never reach the purge below
            with self.connect() as session:
                session.execute(delete(Decision).where(Decision.id.in_(ids)))
            total += len(ids)
            if len(records) < batch_size:
                break
        return total

    def add_promotion(self, entity_type: str, entity_id: str, from_status: str, to_status: str, promoted_by: str, reason: str, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            promotion = Promotion(
                tenant_id=resolved,
                entity_type=entity_type,
                entity_id=entity_id,
                from_status=from_status,
                to_status=to_status,
                promoted_by=promoted_by,
                reason=reason,
            )
            session.add(promotion)
            session.flush()
            return self._promotion_to_dict(promotion)

    def list_promotions(self, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            rows = session.scalars(select(Promotion).where(Promotion.tenant_id == resolved).order_by(desc(Promotion.id))).all()
            return [self._promotion_to_dict(row) for row in rows]

    def add_error_event(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id or payload.get("tenant_id"))
        with self.connect() as session:
            model = ErrorEvent(
                tenant_id=resolved,
                scope=payload.get("scope", "system"),
                entity_type=payload.get("entity_type"),
                entity_id=payload.get("entity_id"),
                stage=payload.get("stage", "unknown"),
                message=payload.get("message", "Unknown error"),
                details_json=copy.deepcopy(payload.get("details", {})),
                created_at=datetime.utcnow(),
            )
            session.add(model)
            session.flush()
            return self._error_event_to_dict(model)

    def list_error_events(self, tenant_id: Optional[str] = None, limit: int = 200, offset: int = 0) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            query = select(ErrorEvent).where(ErrorEvent.tenant_id == resolved).order_by(desc(ErrorEvent.created_at)).limit(max(1, min(limit, 1000))).offset(max(0, offset))
            rows = session.scalars(query).all()
            return [self._error_event_to_dict(row) for row in rows]

    def add_audit_event(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id or payload.get("tenant_id"))
        with self.connect() as session:
            model = AuditEvent(
                tenant_id=resolved,
                event_type=payload["event_type"],
                entity_type=payload.get("entity_type"),
                entity_id=payload.get("entity_id"),
                detail=payload.get("detail", ""),
                metadata_json=copy.deepcopy(payload.get("metadata", {})),
                user_id=payload.get("user_id"),
                ip_address=payload.get("ip_address"),
                created_at=datetime.utcnow(),
            )
            session.add(model)
            session.flush()
            return self._audit_event_to_dict(model)

    def list_audit_events(self, tenant_id: Optional[str] = None, event_type: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            stmt = select(AuditEvent).where(AuditEvent.tenant_id == resolved)
            if event_type:
                stmt = stmt.where(AuditEvent.event_type == event_type)
            rows = session.scalars(stmt.order_by(desc(AuditEvent.created_at))).all()
            return [self._audit_event_to_dict(row) for row in rows]

    def get_history(self, entity_type: str, entity_id: str, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            rows = session.scalars(
                select(EntityHistory)
                .where(EntityHistory.tenant_id == resolved, EntityHistory.entity_type == entity_type, EntityHistory.entity_id == entity_id)
                .order_by(desc(EntityHistory.version))
            ).all()
            return [
                {
                    "id": row.id,
                    "entity_type": row.entity_type,
                    "entity_id": row.entity_id,
                    "version": row.version,
                    "snapshot": copy.deepcopy(row.snapshot or {}),
                    "created_at": serialize_datetime(row.created_at),
                }
                for row in rows
            ]

    def get_settings(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = session.scalar(select(Setting).where(Setting.tenant_id == resolved))
            if not row:
                return copy.deepcopy(DEFAULT_SETTINGS)
            return {
                "api_base_url": row.api_base_url,
                "auth_config": copy.deepcopy(row.auth_config or {}),
                "engine_config": copy.deepcopy(row.engine_config or {}),
                "source_defaults": copy.deepcopy(row.source_defaults or {}),
                "audit_retention_days": row.audit_retention_days,
                "theme_mode": row.theme_mode,
                "branding": copy.deepcopy(row.branding or {}),
                "updated_at": serialize_datetime(row.updated_at),
            }

    def update_settings(self, patch: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id)
        current = self.get_settings(tenant_id=resolved)
        next_value = copy.deepcopy(current)
        next_value.update({key: value for key, value in patch.items() if value is not None})
        with self.connect() as session:
            row = session.scalar(select(Setting).where(Setting.tenant_id == resolved))
            if not row:
                row = Setting(tenant_id=resolved, **DEFAULT_SETTINGS)
                session.add(row)
                session.flush()
            row.api_base_url = next_value["api_base_url"]
            row.auth_config = copy.deepcopy(next_value["auth_config"])
            row.engine_config = copy.deepcopy(next_value["engine_config"])
            row.source_defaults = copy.deepcopy(next_value["source_defaults"])
            row.audit_retention_days = int(next_value["audit_retention_days"])
            row.theme_mode = next_value["theme_mode"]
            row.branding = copy.deepcopy(next_value.get("branding") or {})
            row.updated_at = datetime.utcnow()
        return self.get_settings(tenant_id=resolved)

    # ── AI Copilot config (BYO key) ────────────────────────────────────
    def _ai_config_row(self, session: Session, tenant_id: str) -> Setting:
        self._ensure_settings(session, tenant_id)
        return session.scalar(select(Setting).where(Setting.tenant_id == tenant_id))

    def get_ai_config_masked(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """UI-safe view — never exposes keys, only whether each provider is configured."""
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            cfg = (self._ai_config_row(session, resolved).ai_config or {})
        providers = cfg.get("providers", {}) or {}
        provider_view = {
            name: {"configured": bool((providers.get(name) or {}).get("key_encrypted")),
                   "model": (providers.get(name) or {}).get("model", "")}
            for name in ("anthropic", "openai")
        }
        any_configured = any(p["configured"] for p in provider_view.values())
        # AI is "on" only when a key is present AND the admin hasn't explicitly
        # turned it off. No key -> never on (features stay hidden in the UI).
        enabled = any_configured and bool(cfg.get("enabled", True))
        return {
            "default_provider": cfg.get("default_provider", "anthropic"),
            "providers": provider_view,
            "any_configured": any_configured,
            "enabled": enabled,
        }

    def set_ai_config(self, patch: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = self._ai_config_row(session, resolved)
            cfg = copy.deepcopy(row.ai_config or {})
            cfg.setdefault("providers", {})
            if patch.get("default_provider") in ("anthropic", "openai"):
                cfg["default_provider"] = patch["default_provider"]
            if "enabled" in patch:  # admin can turn AI off even while a key stays stored
                cfg["enabled"] = bool(patch["enabled"])
            for name in ("anthropic", "openai"):
                ppatch = patch.get(name) or {}
                slot = cfg["providers"].setdefault(name, {})
                if "model" in ppatch:
                    slot["model"] = ppatch["model"]
                key = ppatch.get("key")
                if key == "__CLEAR__":
                    slot.pop("key_encrypted", None)
                elif key:  # a new plaintext key → encrypt at rest; empty string = leave as-is
                    slot["key_encrypted"] = encrypt_secret_text(key)
            row.ai_config = cfg
            row.updated_at = datetime.utcnow()
        return self.get_ai_config_masked(tenant_id=resolved)

    def get_ai_credentials(self, provider: Optional[str] = None, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Server-side only — decrypts the key for an outbound LLM call."""
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            cfg = (self._ai_config_row(session, resolved).ai_config or {})
        prov = provider or cfg.get("default_provider", "anthropic")
        slot = (cfg.get("providers", {}) or {}).get(prov) or {}
        enc = slot.get("key_encrypted")
        if not enc:
            return None
        return {"provider": prov, "api_key": decrypt_secret_text(enc), "model": slot.get("model") or None}

    def replace_all(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> None:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            for model in [Connector, Variable, Rule, Scorecard, Policy, Promotion, EntityHistory, Decision, ErrorEvent, AuditEvent]:
                session.execute(delete(model).where(model.tenant_id == resolved))
        for connector in payload.get("connectors", []):
            current = copy.deepcopy(connector)
            if self.get_connector(current["id"], tenant_id=resolved):
                self.update_connector(current["id"], current, tenant_id=resolved)
            else:
                with self.connect() as session:
                    session.add(
                        Connector(
                            tenant_id=resolved,
                            public_id=current["id"],
                            name=current["name"],
                            icon=current.get("icon"),
                            color=current.get("color"),
                            description=current.get("description"),
                            schema_fields=copy.deepcopy(current.get("schema_paths", [])),
                            sample_payload=copy.deepcopy(current.get("sample_payload", {})),
                            is_active=bool(current.get("is_active", True)),
                            encrypted_config=encrypt_config_payload(copy.deepcopy(current.get("config", {}))),
                        )
                    )
        for variable in payload.get("variables", []):
            self.create_variable(variable, tenant_id=resolved)
        for rule in payload.get("rules", []):
            self.create_rule(rule, tenant_id=resolved)
        for scorecard in payload.get("scorecards", []):
            self.create_scorecard(scorecard, tenant_id=resolved)
        for policy in payload.get("policies", []):
            self.create_policy(policy, tenant_id=resolved)
        self.update_settings(payload.get("settings", DEFAULT_SETTINGS), tenant_id=resolved)

    def export_config(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id)
        return {
            "connectors": self.list_connectors(tenant_id=resolved),
            "variables": self.list_variables(tenant_id=resolved),
            "rules": self.list_rules(tenant_id=resolved),
            "scorecards": self.list_scorecards(tenant_id=resolved),
            "policies": self.list_policies(tenant_id=resolved),
            "settings": self.get_settings(tenant_id=resolved),
            "error_events": self.list_error_events(tenant_id=resolved),
        }

    def list_tenants(self) -> List[Dict[str, Any]]:
        with self.connect() as session:
            rows = session.scalars(select(Tenant).order_by(Tenant.created_at)).all()
            return [
                {
                    "id": row.id,
                    "name": row.name,
                    "plan": row.plan,
                    "config": copy.deepcopy(row.config or {}),
                    "is_active": row.is_active,
                    "created_at": serialize_datetime(row.created_at),
                    "updated_at": serialize_datetime(row.updated_at),
                }
                for row in rows
            ]

    def create_tenant(self, name: str, plan: str = "standard", config: Optional[Dict[str, Any]] = None, is_active: bool = True) -> Dict[str, Any]:
        with self.connect() as session:
            tenant = Tenant(name=name, plan=plan, config=copy.deepcopy(config or {}), is_active=is_active)
            session.add(tenant)
            session.flush()
            self._ensure_settings(session, tenant.id)
            return {
                "id": tenant.id,
                "name": tenant.name,
                "plan": tenant.plan,
                "config": copy.deepcopy(tenant.config or {}),
                "is_active": tenant.is_active,
                "created_at": serialize_datetime(tenant.created_at),
                "updated_at": serialize_datetime(tenant.updated_at),
            }

    def get_tenant(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as session:
            tenant = session.get(Tenant, tenant_id)
            if not tenant:
                return None
            return {
                "id": tenant.id,
                "name": tenant.name,
                "plan": tenant.plan,
                "config": copy.deepcopy(tenant.config or {}),
                "is_active": tenant.is_active,
                "created_at": serialize_datetime(tenant.created_at),
                "updated_at": serialize_datetime(tenant.updated_at),
            }

    def update_tenant(self, tenant_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with self.connect() as session:
            tenant = session.get(Tenant, tenant_id)
            if not tenant:
                return None
            if patch.get("name") is not None:
                tenant.name = patch["name"]
            if patch.get("plan") is not None:
                tenant.plan = patch["plan"]
            if patch.get("config") is not None:
                tenant.config = copy.deepcopy(patch["config"])
            if patch.get("is_active") is not None:
                tenant.is_active = bool(patch["is_active"])
            tenant.updated_at = datetime.utcnow()
        return self.get_tenant(tenant_id)

    def mark_bundle_compile_queued(self, tenant_id: str, debounce_seconds: int = 5, force: bool = False) -> bool:
        with self.connect() as session:
            tenant = session.get(Tenant, tenant_id)
            if not tenant:
                return False
            now = datetime.utcnow()
            if not force and tenant.last_bundle_queued_at and (now - tenant.last_bundle_queued_at).total_seconds() < debounce_seconds:
                return False
            tenant.last_bundle_queued_at = now
            tenant.updated_at = now
            return True

    def generate_api_key_for_tenant(self, tenant_id: str, environment: str = "prod", label: Optional[str] = None, role: str = "owner") -> Dict[str, Any]:
        from .rbac import normalize_role

        plaintext = generate_api_key()
        lookup_hash = key_lookup_hash(plaintext)
        env = environment if environment in ("dev", "prod", "sandbox") else "prod"
        resolved_role = normalize_role(role)
        with self.connect() as session:
            tenant = session.get(Tenant, tenant_id)
            if not tenant:
                raise ValueError("Tenant not found")
            model = ApiKey(
                tenant_id=tenant_id,
                kid=api_key_kid(plaintext),
                masked_key=mask_api_key(plaintext),
                lookup_hash=lookup_hash,
                key_hash=bcrypt_hash(plaintext),
                is_active=True,
                environment=env,
                label=label,
                role=resolved_role,
            )
            session.add(model)
            tenant.api_key_hash = lookup_hash
            session.flush()
            return {
                "id": model.id,
                "kid": model.kid,
                "masked_key": model.masked_key,
                "plaintext": plaintext,
                "environment": env,
                "label": label,
                "role": resolved_role,
                "created_at": serialize_datetime(model.created_at),
            }

    # ── Email (SMTP) config for scheduled report delivery ───────────────
    def set_email_config(self, patch: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            tenant = session.get(Tenant, resolved)
            if not tenant:
                raise ValueError("Tenant not found")
            cfg = copy.deepcopy(tenant.config or {})
            email = cfg.get("email") or {}
            for key in ("host", "port", "username", "from_addr", "use_tls", "use_ssl"):
                if key in patch:
                    email[key] = patch[key]
            pw = patch.get("password")
            if pw == "__CLEAR__":
                email.pop("password_encrypted", None)
            elif pw:
                email["password_encrypted"] = encrypt_secret_text(pw)
            cfg["email"] = email
            tenant.config = cfg
            session.flush()
        return self.get_email_config_masked(tenant_id=resolved)

    def get_email_config_masked(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            tenant = session.get(Tenant, resolved)
            email = ((tenant.config or {}).get("email") or {}) if tenant else {}
        return {
            "host": email.get("host", ""),
            "port": email.get("port", 587),
            "username": email.get("username", ""),
            "from_addr": email.get("from_addr", ""),
            "use_tls": email.get("use_tls", True),
            "use_ssl": email.get("use_ssl", False),
            "password_set": bool(email.get("password_encrypted")),
            "configured": bool(email.get("host") and email.get("from_addr")),
        }

    def get_email_credentials(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """Server-side only — decrypts the SMTP password for an outbound send."""
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            tenant = session.get(Tenant, resolved)
            email = copy.deepcopy((tenant.config or {}).get("email") or {}) if tenant else {}
        enc = email.pop("password_encrypted", None)
        if enc:
            email["password"] = decrypt_secret_text(enc)
        return email

    # ── AI usage + cost accounting (per workspace) ───────────────────────
    def record_ai_usage(self, provider: str, model: str, input_tokens: int, output_tokens: int,
                        cost_usd: float, tenant_id: Optional[str] = None) -> None:
        """Accumulate token + estimated-cost counters for a workspace's AI usage.
        Off the critical concern of the call itself — best-effort, never raises."""
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            tenant = session.get(Tenant, resolved)
            if not tenant:
                return
            cfg = copy.deepcopy(tenant.config or {})
            usage = cfg.get("ai_usage") or {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "by_model": {}}
            usage["calls"] = int(usage.get("calls", 0)) + 1
            usage["input_tokens"] = int(usage.get("input_tokens", 0)) + int(input_tokens)
            usage["output_tokens"] = int(usage.get("output_tokens", 0)) + int(output_tokens)
            usage["cost_usd"] = round(float(usage.get("cost_usd", 0.0)) + float(cost_usd), 6)
            per_model = usage.get("by_model") or {}
            key = "{0}/{1}".format(provider, model)
            row = per_model.get(key) or {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
            row["calls"] += 1
            row["input_tokens"] += int(input_tokens)
            row["output_tokens"] += int(output_tokens)
            row["cost_usd"] = round(float(row["cost_usd"]) + float(cost_usd), 6)
            per_model[key] = row
            usage["by_model"] = per_model
            usage["updated_at"] = now_iso()
            cfg["ai_usage"] = usage
            tenant.config = cfg

    def get_ai_usage(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            tenant = session.get(Tenant, resolved)
            cfg = (tenant.config or {}) if tenant else {}
            usage = copy.deepcopy(cfg.get("ai_usage") or {})
            budget = float(((cfg.get("settings_ai") or {}).get("monthly_budget_usd", 0)) or 0)
        usage.setdefault("calls", 0)
        usage.setdefault("input_tokens", 0)
        usage.setdefault("output_tokens", 0)
        usage.setdefault("cost_usd", 0.0)
        usage.setdefault("by_model", {})
        usage["budget_usd"] = budget
        usage["over_budget"] = bool(budget) and float(usage.get("cost_usd", 0)) >= budget
        return usage

    def set_ai_budget(self, monthly_budget_usd: float, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            tenant = session.get(Tenant, resolved)
            if not tenant:
                raise ValueError("Tenant not found")
            cfg = copy.deepcopy(tenant.config or {})
            settings_ai = cfg.get("settings_ai") or {}
            settings_ai["monthly_budget_usd"] = float(monthly_budget_usd or 0)
            cfg["settings_ai"] = settings_ai
            tenant.config = cfg
        return self.get_ai_usage(tenant_id=resolved)

    def reset_ai_usage(self, tenant_id: Optional[str] = None) -> None:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            tenant = session.get(Tenant, resolved)
            if not tenant:
                return
            cfg = copy.deepcopy(tenant.config or {})
            cfg["ai_usage"] = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "by_model": {}, "updated_at": now_iso()}
            tenant.config = cfg

    # ── SSO (OIDC / SAML) connection config, per workspace ───────────────
    def set_sso_config(self, patch: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """Upsert the workspace's SSO connection. The OIDC client secret is stored
        Fernet-encrypted and never returned to the client."""
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            tenant = session.get(Tenant, resolved)
            if not tenant:
                raise ValueError("Tenant not found")
            cfg = copy.deepcopy(tenant.config or {})
            sso = cfg.get("sso") or {}
            for key in ("provider", "enabled", "issuer", "client_id", "redirect_uri", "scope",
                        "authorization_endpoint", "token_endpoint", "jwks_uri",
                        "sp_entity_id", "sso_url", "acs_url", "idp_entity_id", "idp_cert",
                        "allowed_domains", "default_role", "jit_provisioning"):
                if key in patch:
                    sso[key] = patch[key]
            secret = patch.get("client_secret")
            if secret == "__CLEAR__":
                sso.pop("client_secret_encrypted", None)
            elif secret:
                sso["client_secret_encrypted"] = encrypt_secret_text(secret)
            cfg["sso"] = sso
            tenant.config = cfg
            session.flush()
        return self.get_sso_config_masked(tenant_id=resolved)

    def get_sso_config_masked(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            tenant = session.get(Tenant, resolved)
            sso = copy.deepcopy((tenant.config or {}).get("sso") or {}) if tenant else {}
        has_secret = bool(sso.pop("client_secret_encrypted", None))
        sso["client_secret_set"] = has_secret
        sso.setdefault("provider", "oidc")
        sso.setdefault("enabled", False)
        sso.setdefault("default_role", "viewer")
        sso.setdefault("jit_provisioning", True)
        sso.setdefault("allowed_domains", [])
        return sso

    def get_sso_config_internal(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """Server-side only — decrypts the client secret for the token exchange."""
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            tenant = session.get(Tenant, resolved)
            sso = copy.deepcopy((tenant.config or {}).get("sso") or {}) if tenant else {}
        enc = sso.pop("client_secret_encrypted", None)
        if enc:
            sso["client_secret"] = decrypt_secret_text(enc)
        return sso

    def seed_sample_inventory(self, tenant_id: str) -> None:
        """Populate a new workspace with the sample connectors/variables/rules/
        scorecards/policies so an onboarding client can try a decision immediately."""
        with self.connect() as session:
            self._ensure_seed_inventory(session, tenant_id)

    # ── Onboarding state (stored on tenant.config["onboarding"]) ────────
    def get_onboarding(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            tenant = session.get(Tenant, resolved)
            cfg = (tenant.config or {}) if tenant else {}
        return copy.deepcopy(cfg.get("onboarding") or {})

    def update_onboarding(self, patch: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            tenant = session.get(Tenant, resolved)
            if not tenant:
                raise ValueError("Tenant not found")
            cfg = copy.deepcopy(tenant.config or {})
            ob = cfg.get("onboarding") or {}
            ob.update(patch)
            cfg["onboarding"] = ob
            tenant.config = cfg
            session.flush()
            return copy.deepcopy(ob)

    def list_api_keys(self, tenant_id: str) -> List[Dict[str, Any]]:
        with self.connect() as session:
            rows = session.scalars(select(ApiKey).where(ApiKey.tenant_id == tenant_id).order_by(desc(ApiKey.created_at))).all()
            return [
                {
                    "id": row.id,
                    "kid": row.kid,
                    "masked_key": row.masked_key,
                    "is_active": row.is_active,
                    "environment": row.environment,
                    "label": row.label,
                    "role": row.role,
                    "revoked_at": serialize_datetime(row.revoked_at),
                    "last_used_at": serialize_datetime(row.last_used_at),
                    "created_at": serialize_datetime(row.created_at),
                }
                for row in rows
            ]

    def revoke_api_key(self, tenant_id: str, kid: str) -> bool:
        with self.connect() as session:
            row = session.scalar(select(ApiKey).where(ApiKey.tenant_id == tenant_id, ApiKey.kid == kid, ApiKey.is_active.is_(True)))
            if not row:
                return False
            row.is_active = False
            row.revoked_at = datetime.utcnow()
            # Eagerly drop any cached verification so the revoke takes effect now.
            self._api_key_cache.clear()
            return True

    def get_tenant_by_api_key(self, api_key: str) -> Optional[Dict[str, Any]]:
        # bcrypt-verifying the API key on EVERY request costs ~200ms and caps the
        # whole API at a few req/s. Verify once, then cache the resolved tenant for
        # a short TTL — subsequent requests skip both bcrypt and the DB round-trip.
        # Revocation is honoured within API_KEY_CACHE_TTL (and eagerly on revoke).
        now = _time.time()
        cached = self._api_key_cache.get(api_key)
        if cached is not None and cached[1] > now:
            return cached[0]
        lookup_hash = key_lookup_hash(api_key)
        with self.connect() as session:
            row = session.scalar(select(ApiKey).where(ApiKey.lookup_hash == lookup_hash, ApiKey.is_active.is_(True)))
            if not row:
                return None
            if not bcrypt_verify(api_key, row.key_hash):
                return None
            tenant = session.get(Tenant, row.tenant_id)
            if not tenant:
                return None
            # Throttle the last_used_at write so it isn't a DB write per request.
            last_used = row.last_used_at
            if last_used is None or (datetime.utcnow() - last_used).total_seconds() > 60:
                row.last_used_at = datetime.utcnow()
            result = {
                "tenant": {
                    "id": tenant.id,
                    "name": tenant.name,
                    "plan": tenant.plan,
                    "is_active": tenant.is_active,
                    "config": copy.deepcopy(tenant.config or {}),
                },
                "api_key": {
                    "id": row.id,
                    "kid": row.kid,
                    "masked_key": row.masked_key,
                    "role": row.role,
                },
            }
        if _API_KEY_CACHE_TTL > 0:
            self._api_key_cache[api_key] = (result, now + _API_KEY_CACHE_TTL)
        return result

    def update_api_key_role(self, tenant_id: str, kid: str, role: str) -> Optional[Dict[str, Any]]:
        """Change a key's role in place (role-change-in-place). Eagerly clears the
        verified-key cache so the new role takes effect on the next request."""
        from .rbac import normalize_role

        resolved_role = normalize_role(role)
        with self.connect() as session:
            row = session.scalar(select(ApiKey).where(ApiKey.tenant_id == tenant_id, ApiKey.kid == kid, ApiKey.is_active.is_(True)))
            if not row:
                return None
            row.role = resolved_role
            self._api_key_cache.clear()
            return {"kid": row.kid, "role": resolved_role}

    # ── Workspace members (human accounts with RBAC roles) ───────────────
    def _member_to_dict(self, row: WorkspaceMember) -> Dict[str, Any]:
        return {
            "id": row.id,
            "tenant_id": row.tenant_id,
            "email": row.email,
            "name": row.name,
            "role": row.role,
            "auth_provider": row.auth_provider,
            "is_active": row.is_active,
            "has_password": bool(row.password_hash),
            "last_login_at": serialize_datetime(row.last_login_at),
            "created_at": serialize_datetime(row.created_at),
        }

    def _invalidate_member(self, member_id: str) -> None:
        """Drop cached sessions for a member so a role change / deactivation is
        honoured immediately rather than only after the TTL."""
        for token_hash in [k for k, v in self._member_session_cache.items()
                           if isinstance(v, tuple) and v[0] and v[0].get("member", {}).get("id") == member_id]:
            self._member_session_cache.pop(token_hash, None)

    def list_members(self, tenant_id: str) -> List[Dict[str, Any]]:
        with self.connect() as session:
            rows = session.scalars(
                select(WorkspaceMember).where(WorkspaceMember.tenant_id == tenant_id).order_by(WorkspaceMember.created_at)
            ).all()
            return [self._member_to_dict(row) for row in rows]

    def get_member(self, tenant_id: str, member_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as session:
            row = session.get(WorkspaceMember, member_id)
            if not row or row.tenant_id != tenant_id:
                return None
            return self._member_to_dict(row)

    def get_member_by_email(self, email: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Resolve a member by email. If tenant_id is given, scope to it; otherwise
        return the single match (None if the email is ambiguous across workspaces)."""
        normalized = (email or "").lower().strip()
        with self.connect() as session:
            stmt = select(WorkspaceMember).where(func.lower(WorkspaceMember.email) == normalized)
            if tenant_id:
                stmt = stmt.where(WorkspaceMember.tenant_id == tenant_id)
            rows = session.scalars(stmt).all()
            if len(rows) != 1:
                return None
            return self._member_to_dict(rows[0])

    def create_member(self, tenant_id: str, email: str, name: str, role: str,
                      password: Optional[str] = None, auth_provider: str = "password",
                      external_id: Optional[str] = None) -> Dict[str, Any]:
        from .rbac import normalize_role

        normalized = (email or "").lower().strip()
        with self.connect() as session:
            existing = session.scalar(
                select(WorkspaceMember).where(WorkspaceMember.tenant_id == tenant_id,
                                              func.lower(WorkspaceMember.email) == normalized)
            )
            if existing:
                raise ValueError("A member with this email already exists in the workspace.")
            row = WorkspaceMember(
                tenant_id=tenant_id,
                email=normalized,
                name=name or normalized,
                role=normalize_role(role),
                password_hash=bcrypt_hash(password) if password else None,
                auth_provider=auth_provider,
                external_id=external_id,
                is_active=True,
            )
            session.add(row)
            session.flush()
            return self._member_to_dict(row)

    def update_member(self, tenant_id: str, member_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        from .rbac import normalize_role

        with self.connect() as session:
            row = session.get(WorkspaceMember, member_id)
            if not row or row.tenant_id != tenant_id:
                return None
            if "name" in patch and patch["name"]:
                row.name = patch["name"]
            if "role" in patch and patch["role"]:
                row.role = normalize_role(patch["role"])
            if "is_active" in patch:
                row.is_active = bool(patch["is_active"])
            pw = patch.get("password")
            if pw == "__CLEAR__":
                row.password_hash = None
            elif pw:
                row.password_hash = bcrypt_hash(pw)
            result = self._member_to_dict(row)
        # Role/active change must take effect now, not after the session-cache TTL.
        self._invalidate_member(member_id)
        return result

    def verify_member_password(self, member_id: str, password: str) -> bool:
        with self.connect() as session:
            row = session.get(WorkspaceMember, member_id)
            if not row or not row.is_active or not row.password_hash:
                return False
            return bcrypt_verify(password, row.password_hash)

    def ensure_member(self, tenant_id: str, email: str, name: str, role: str,
                      password: Optional[str] = None) -> Dict[str, Any]:
        """Idempotent member bootstrap (used by dev seeding / tests)."""
        existing = self.get_member_by_email(email, tenant_id=tenant_id)
        if existing:
            return existing
        return self.create_member(tenant_id, email, name, role, password=password)

    # ── Member login sessions ────────────────────────────────────────────
    def create_member_session(self, tenant_id: str, member_id: str, ttl_hours: int = 12) -> Dict[str, Any]:
        token = generate_session_token()
        expires_at = datetime.utcnow() + timedelta(hours=ttl_hours)
        with self.connect() as session:
            member = session.get(WorkspaceMember, member_id)
            if member:
                member.last_login_at = datetime.utcnow()
            session.add(MemberSession(
                tenant_id=tenant_id, member_id=member_id,
                token_hash=session_token_hash(token), expires_at=expires_at,
            ))
        return {"token": token, "expires_at": serialize_datetime(expires_at)}

    def resolve_member_session(self, token: str) -> Optional[Dict[str, Any]]:
        """Resolve a bearer session token to its tenant + member + LIVE role. Cached
        for a short TTL; the role is re-read from the member so admin changes apply
        promptly (and eagerly via _invalidate_member)."""
        if not token:
            return None
        token_hash = session_token_hash(token)
        now = _time.time()
        cached = self._member_session_cache.get(token_hash)
        if cached is not None and cached[1] > now:
            return cached[0]
        with self.connect() as session:
            row = session.scalar(select(MemberSession).where(MemberSession.token_hash == token_hash))
            if not row or row.revoked or row.expires_at < datetime.utcnow():
                return None
            member = session.get(WorkspaceMember, row.member_id)
            if not member or not member.is_active:
                return None
            last_seen = row.last_seen_at
            if last_seen is None or (datetime.utcnow() - last_seen).total_seconds() > 60:
                row.last_seen_at = datetime.utcnow()
            result = {
                "tenant": {"id": member.tenant_id},
                "member": {"id": member.id, "email": member.email, "name": member.name, "role": member.role},
                "role": member.role,
                "session_id": row.id,
            }
        if _API_KEY_CACHE_TTL > 0:
            self._member_session_cache[token_hash] = (result, now + min(_API_KEY_CACHE_TTL, 60.0))
        return result

    def revoke_member_session(self, token: str) -> bool:
        token_hash = session_token_hash(token)
        with self.connect() as session:
            row = session.scalar(select(MemberSession).where(MemberSession.token_hash == token_hash))
            if not row:
                self._member_session_cache.pop(token_hash, None)
                return False
            row.revoked = True
        self._member_session_cache.pop(token_hash, None)
        return True

    # ── Email login OTP ──────────────────────────────────────────────────
    def issue_member_otp(self, tenant_id: str, email: str, ttl_minutes: int = 10) -> str:
        """Create a one-time passcode (returns the plaintext code for delivery)."""
        normalized = (email or "").lower().strip()
        code = generate_otp_code()
        with self.connect() as session:
            session.add(MemberOtp(
                tenant_id=tenant_id, email=normalized,
                code_hash=otp_code_hash(tenant_id, normalized, code),
                expires_at=datetime.utcnow() + timedelta(minutes=ttl_minutes),
            ))
        return code

    def verify_member_otp(self, tenant_id: str, email: str, code: str, max_attempts: int = 5) -> bool:
        normalized = (email or "").lower().strip()
        target = otp_code_hash(tenant_id, normalized, code)
        with self.connect() as session:
            row = session.scalar(
                select(MemberOtp).where(
                    MemberOtp.tenant_id == tenant_id, MemberOtp.email == normalized,
                    MemberOtp.consumed.is_(False),
                ).order_by(desc(MemberOtp.created_at))
            )
            if not row or row.expires_at < datetime.utcnow() or row.attempts >= max_attempts:
                return False
            row.attempts += 1
            if not hmac.compare_digest(row.code_hash, target):
                return False
            row.consumed = True
            return True

    def get_platform_admin_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        with self.connect() as session:
            row = session.scalar(select(PlatformAdminUser).where(PlatformAdminUser.email == email))
            if not row:
                return None
            return {
                "id": row.id,
                "email": row.email,
                "name": row.name,
                "password_hash": row.password_hash,
                "is_active": row.is_active,
                "created_at": serialize_datetime(row.created_at),
                "updated_at": serialize_datetime(row.updated_at),
            }

    def get_platform_admin_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as session:
            row = session.get(PlatformAdminUser, user_id)
            if not row:
                return None
            return {
                "id": row.id,
                "email": row.email,
                "name": row.name,
                "is_active": row.is_active,
                "created_at": serialize_datetime(row.created_at),
                "updated_at": serialize_datetime(row.updated_at),
            }

    def create_platform_admin_user(self, email: str, password: str, name: str) -> Dict[str, Any]:
        with self.connect() as session:
            existing = session.scalar(select(PlatformAdminUser).where(PlatformAdminUser.email == email))
            if existing:
                raise ValueError("Admin user already exists")
            model = PlatformAdminUser(email=email, name=name, password_hash=bcrypt_hash(password), is_active=True)
            session.add(model)
            session.flush()
            return {
                "id": model.id,
                "email": model.email,
                "name": model.name,
                "is_active": model.is_active,
                "created_at": serialize_datetime(model.created_at),
                "updated_at": serialize_datetime(model.updated_at),
            }

    def get_platform_admin_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        with self.connect() as session:
            row = session.scalar(select(PlatformAdminUser).where(PlatformAdminUser.email == email))
            if not row:
                return None
            return {
                "id": row.id,
                "email": row.email,
                "name": row.name,
                "is_active": row.is_active,
                "created_at": serialize_datetime(row.created_at),
                "updated_at": serialize_datetime(row.updated_at),
            }

    def create_or_update_experiment(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id or payload.get("tenant_id"))
        public_id = payload.get("id")
        with self.connect() as session:
            model = self._query_by_public_id(session, Experiment, public_id, resolved) if public_id else None
            if not model:
                model = Experiment(
                    tenant_id=resolved,
                    public_id=public_id,
                    name=payload["name"],
                    description=payload.get("description"),
                    status=payload.get("status", "draft"),
                    variants=copy.deepcopy(payload.get("variants", [])),
                    hash_key=payload.get("hash_key", "user_id"),
                    target_policy_id=payload.get("target_policy_id"),
                    start_date=payload.get("start_date"),
                    end_date=payload.get("end_date"),
                )
                session.add(model)
            else:
                model.name = payload.get("name", model.name)
                model.description = payload.get("description", model.description)
                model.status = payload.get("status", model.status)
                model.variants = copy.deepcopy(payload.get("variants", model.variants))
                model.hash_key = payload.get("hash_key", model.hash_key)
                model.target_policy_id = payload.get("target_policy_id", model.target_policy_id)
                model.start_date = payload.get("start_date", model.start_date)
                model.end_date = payload.get("end_date", model.end_date)
                model.updated_at = datetime.utcnow()
            session.flush()
            return self._experiment_to_dict(model)

    def list_experiments(self, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            rows = session.scalars(select(Experiment).where(Experiment.tenant_id == resolved).order_by(Experiment.created_at)).all()
            return [self._experiment_to_dict(row) for row in rows]

    def get_experiment(self, experiment_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = self._query_by_public_id(session, Experiment, experiment_id, resolved)
            return self._experiment_to_dict(row) if row else None

    @staticmethod
    def _experiment_to_dict(model: Experiment) -> Dict[str, Any]:
        return {
            "id": model.public_id,
            "name": model.name,
            "description": model.description,
            "status": model.status,
            "variants": copy.deepcopy(model.variants or []),
            "hash_key": model.hash_key,
            "target_policy_id": model.target_policy_id,
            "start_date": serialize_datetime(model.start_date),
            "end_date": serialize_datetime(model.end_date),
            "created_at": serialize_datetime(model.created_at),
            "updated_at": serialize_datetime(model.updated_at),
        }

    def delete_experiment(self, experiment_id: str, tenant_id: Optional[str] = None) -> bool:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = self._query_by_public_id(session, Experiment, experiment_id, resolved)
            if not row:
                return False
            session.delete(row)
            return True

    def save_bundle(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id or payload.get("tenant_id"))
        with self.connect() as session:
            current = session.scalars(select(Bundle).where(Bundle.tenant_id == resolved, Bundle.superseded.is_(False))).all()
            for row in current:
                row.superseded = True
            bundle = Bundle(
                tenant_id=resolved,
                version=int(payload["version"]),
                content=copy.deepcopy(payload.get("content", {})),
                encrypted_content=payload["encrypted_content"],
                encrypted_key=payload.get("encrypted_key"),
                signature=payload["signature"],
                checksum=payload["checksum"],
                superseded=False,
                compiled_at=payload.get("compiled_at", datetime.utcnow()),
                expires_at=payload.get("expires_at", datetime.utcnow() + timedelta(days=30)),
            )
            session.add(bundle)
            session.flush()
            return self._bundle_to_dict(bundle)

    def latest_bundle(self, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = session.scalar(select(Bundle).where(Bundle.tenant_id == resolved, Bundle.superseded.is_(False)).order_by(desc(Bundle.version)).limit(1))
            return self._bundle_to_dict(row) if row else None

    def list_bundles(self, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            rows = session.scalars(select(Bundle).where(Bundle.tenant_id == resolved).order_by(desc(Bundle.version))).all()
            return [self._bundle_to_dict(row) for row in rows]

    def add_sdk_events(self, events: List[Dict[str, Any]], tenant_id: Optional[str] = None) -> int:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            for event in events:
                session.add(
                    SdkEvent(
                        tenant_id=resolved,
                        event_type=event.get("type", "unknown"),
                        payload=copy.deepcopy(event),
                        created_at=datetime.utcnow(),
                    )
                )
            return len(events)

    def list_sdk_events(self, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            rows = session.scalars(select(SdkEvent).where(SdkEvent.tenant_id == resolved).order_by(desc(SdkEvent.created_at))).all()
            return [
                {
                    "id": row.id,
                    "tenant_id": row.tenant_id,
                    "type": row.event_type,
                    "payload": copy.deepcopy(row.payload or {}),
                    "created_at": serialize_datetime(row.created_at),
                }
                for row in rows
            ]

    def create_workflow_execution(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id or payload.get("tenant_id"))
        with self.connect() as session:
            model = WorkflowExecution(
                id=payload.get("id") or uuid4_str(),
                tenant_id=resolved,
                policy_id=payload["policy_id"],
                status=payload.get("status", "running"),
                context=copy.deepcopy(payload.get("context", {})),
                current_step_index=int(payload.get("current_step_index", 0)),
                trigger_type=payload.get("trigger_type"),
                trigger_metadata=copy.deepcopy(payload.get("trigger_metadata")),
                started_at=payload.get("started_at", datetime.utcnow()),
                paused_at=payload.get("paused_at"),
                completed_at=payload.get("completed_at"),
            )
            session.add(model)
            session.flush()
            return self._workflow_execution_to_dict(model)

    def update_workflow_execution(self, execution_id: str, patch: Dict[str, Any], tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            model = session.get(WorkflowExecution, execution_id)
            if not model or model.tenant_id != resolved:
                return None
            for field in ["status", "context", "current_step_index", "trigger_type", "trigger_metadata", "paused_at", "completed_at"]:
                if field in patch and patch[field] is not None:
                    setattr(model, field, copy.deepcopy(patch[field]) if isinstance(patch[field], (dict, list)) else patch[field])
            model.updated_at = datetime.utcnow()
            return self._workflow_execution_to_dict(model)

    def get_workflow_execution(self, execution_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            model = session.get(WorkflowExecution, execution_id)
            if not model or model.tenant_id != resolved:
                return None
            return self._workflow_execution_to_dict(model)

    @staticmethod
    def _workflow_execution_to_dict(model: WorkflowExecution) -> Dict[str, Any]:
        return {
            "id": model.id,
            "tenant_id": model.tenant_id,
            "policy_id": model.policy_id,
            "status": model.status,
            "context": copy.deepcopy(model.context or {}),
            "current_step_index": model.current_step_index,
            "trigger_type": model.trigger_type,
            "trigger_metadata": copy.deepcopy(model.trigger_metadata or {}),
            "started_at": serialize_datetime(model.started_at),
            "paused_at": serialize_datetime(model.paused_at),
            "completed_at": serialize_datetime(model.completed_at),
            "created_at": serialize_datetime(model.created_at),
            "updated_at": serialize_datetime(model.updated_at),
        }

    def create_review_task(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id or payload.get("tenant_id"))
        with self.connect() as session:
            model = ReviewTask(
                id=payload.get("id") or uuid4_str(),
                tenant_id=resolved,
                execution_id=payload["execution_id"],
                policy_id=payload.get("policy_id"),
                step_id=payload.get("step_id"),
                queue=payload.get("queue", "default"),
                status=payload.get("status", "pending"),
                context_snapshot=copy.deepcopy(payload.get("context_snapshot", {})),
                required_fields=copy.deepcopy(payload.get("required_fields", [])),
                reviewer_response=copy.deepcopy(payload.get("reviewer_response")),
                reviewed_by=payload.get("reviewed_by"),
                assigned_at=payload.get("assigned_at", datetime.utcnow()),
                timeout_at=payload.get("timeout_at"),
                reviewed_at=payload.get("reviewed_at"),
            )
            session.add(model)
            session.flush()
            return self._review_task_to_dict(model)

    def update_review_task(self, task_id: str, patch: Dict[str, Any], tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            model = session.get(ReviewTask, task_id)
            if not model or model.tenant_id != resolved:
                return None
            for field in ["status", "context_snapshot", "required_fields", "reviewer_response", "reviewed_by", "timeout_at", "reviewed_at"]:
                if field in patch and patch[field] is not None:
                    setattr(model, field, copy.deepcopy(patch[field]) if isinstance(patch[field], (dict, list)) else patch[field])
            return self._review_task_to_dict(model)

    def get_review_task(self, task_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            model = session.get(ReviewTask, task_id)
            if not model or model.tenant_id != resolved:
                return None
            return self._review_task_to_dict(model)

    def list_review_tasks(self, queue: Optional[str] = None, status: Optional[str] = None, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            stmt = select(ReviewTask).where(ReviewTask.tenant_id == resolved)
            if queue:
                stmt = stmt.where(ReviewTask.queue == queue)
            if status:
                stmt = stmt.where(ReviewTask.status == status)
            rows = session.scalars(stmt.order_by(desc(ReviewTask.assigned_at))).all()
            return [self._review_task_to_dict(row) for row in rows]

    def get_expired_review_tasks(self, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self.connect() as session:
            stmt = select(ReviewTask).where(
                ReviewTask.status == "pending",
                ReviewTask.timeout_at.is_not(None),
                ReviewTask.timeout_at < datetime.utcnow(),
            )
            if tenant_id is not None:
                stmt = stmt.where(ReviewTask.tenant_id == self._tenant_id(tenant_id))
            rows = session.scalars(stmt).all()
            return [self._review_task_to_dict(row) for row in rows]

    @staticmethod
    def _review_task_to_dict(model: ReviewTask) -> Dict[str, Any]:
        snapshot = copy.deepcopy(model.context_snapshot or {})
        routing = snapshot.get("routing", {}) if isinstance(snapshot, dict) else {}
        sla_at = routing.get("sla_at")
        sla_breached = False
        if sla_at and model.status == "pending":
            try:
                deadline = datetime.fromisoformat(str(sla_at).replace("Z", "+00:00")).replace(tzinfo=None)
                sla_breached = datetime.utcnow() > deadline
            except ValueError:
                sla_breached = False
        return {
            "id": model.id,
            "tenant_id": model.tenant_id,
            "execution_id": model.execution_id,
            "policy_id": model.policy_id,
            "step_id": model.step_id,
            "queue": model.queue,
            "role": routing.get("role"),
            "priority": routing.get("priority", "normal"),
            "sla_at": sla_at,
            "sla_breached": sla_breached,
            "status": model.status,
            "context_snapshot": snapshot,
            "required_fields": copy.deepcopy(model.required_fields or []),
            "reviewer_response": copy.deepcopy(model.reviewer_response),
            "reviewed_by": model.reviewed_by,
            "assigned_at": serialize_datetime(model.assigned_at),
            "timeout_at": serialize_datetime(model.timeout_at),
            "reviewed_at": serialize_datetime(model.reviewed_at),
        }

    def create_webhook(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id or payload.get("tenant_id"))
        with self.connect() as session:
            model = Webhook(
                id=payload.get("id") or uuid4_str(),
                tenant_id=resolved,
                policy_id=payload["policy_id"],
                endpoint_path=payload["endpoint_path"],
                is_active=bool(payload.get("is_active", True)),
                secret_hash=encrypt_secret_text(payload["secret_hash"]) if payload.get("secret_hash") else None,
                payload_mapping=copy.deepcopy(payload.get("payload_mapping")),
            )
            session.add(model)
            session.flush()
            return self._webhook_to_dict(model)

    def update_webhook(self, webhook_id: str, patch: Dict[str, Any], tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            model = session.get(Webhook, webhook_id)
            if not model or model.tenant_id != resolved:
                return None
            for field in ["policy_id", "endpoint_path", "is_active", "secret_hash", "payload_mapping"]:
                if field in patch and patch[field] is not None:
                    value = copy.deepcopy(patch[field]) if isinstance(patch[field], (dict, list)) else patch[field]
                    if field == "secret_hash":
                        value = encrypt_secret_text(str(value))
                    setattr(model, field, value)
            model.updated_at = datetime.utcnow()
            return self._webhook_to_dict(model)

    def get_webhook(self, webhook_id: str, tenant_id: Optional[str] = None, include_secret: bool = False) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            model = session.get(Webhook, webhook_id)
            if not model or model.tenant_id != resolved:
                return None
            return self._webhook_to_dict(model, include_secret=include_secret)

    def get_webhook_by_path(self, endpoint_path: str, include_secret: bool = False) -> Optional[Dict[str, Any]]:
        with self.connect() as session:
            model = session.scalar(select(Webhook).where(Webhook.endpoint_path == endpoint_path, Webhook.is_active.is_(True)))
            return self._webhook_to_dict(model, include_secret=include_secret) if model else None

    def list_webhooks(self, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            rows = session.scalars(select(Webhook).where(Webhook.tenant_id == resolved).order_by(desc(Webhook.created_at))).all()
            return [self._webhook_to_dict(row) for row in rows]

    @staticmethod
    def _webhook_to_dict(model: Webhook, include_secret: bool = False) -> Dict[str, Any]:
        return {
            "id": model.id,
            "tenant_id": model.tenant_id,
            "policy_id": model.policy_id,
            "endpoint_path": model.endpoint_path,
            "is_active": model.is_active,
            "secret_hash": decrypt_secret_text(model.secret_hash) if include_secret else ("••••••••" if model.secret_hash else None),
            "payload_mapping": copy.deepcopy(model.payload_mapping or {}),
            "created_at": serialize_datetime(model.created_at),
            "updated_at": serialize_datetime(model.updated_at),
        }

    def create_schedule(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id or payload.get("tenant_id"))
        with self.connect() as session:
            model = CronSchedule(
                id=payload.get("id") or uuid4_str(),
                tenant_id=resolved,
                policy_id=payload["policy_id"],
                cron_expression=payload["cron_expression"],
                is_active=bool(payload.get("is_active", True)),
                last_run_at=payload.get("last_run_at"),
                next_run_at=payload.get("next_run_at"),
                payload_source=copy.deepcopy(payload.get("payload_source")),
                config=copy.deepcopy(payload.get("config")),
            )
            session.add(model)
            session.flush()
            return self._schedule_to_dict(model)

    def update_schedule(self, schedule_id: str, patch: Dict[str, Any], tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            model = session.get(CronSchedule, schedule_id)
            if not model or model.tenant_id != resolved:
                return None
            for field in ["policy_id", "cron_expression", "is_active", "last_run_at", "next_run_at", "payload_source", "config"]:
                if field in patch and patch[field] is not None:
                    setattr(model, field, copy.deepcopy(patch[field]) if isinstance(patch[field], (dict, list)) else patch[field])
            model.updated_at = datetime.utcnow()
            return self._schedule_to_dict(model)

    def get_schedule(self, schedule_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            model = session.get(CronSchedule, schedule_id)
            if not model or model.tenant_id != resolved:
                return None
            return self._schedule_to_dict(model)

    def list_schedules(self, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            rows = session.scalars(select(CronSchedule).where(CronSchedule.tenant_id == resolved).order_by(desc(CronSchedule.created_at))).all()
            return [self._schedule_to_dict(row) for row in rows]

    def list_active_schedules(self) -> List[Dict[str, Any]]:
        with self.connect() as session:
            rows = session.scalars(select(CronSchedule).where(CronSchedule.is_active.is_(True))).all()
            return [self._schedule_to_dict(row) for row in rows]

    @staticmethod
    def _schedule_to_dict(model: CronSchedule) -> Dict[str, Any]:
        return {
            "id": model.id,
            "tenant_id": model.tenant_id,
            "policy_id": model.policy_id,
            "cron_expression": model.cron_expression,
            "is_active": model.is_active,
            "last_run_at": serialize_datetime(model.last_run_at),
            "next_run_at": serialize_datetime(model.next_run_at),
            "payload_source": copy.deepcopy(model.payload_source or {}),
            "config": copy.deepcopy(model.config or {}),
            "created_at": serialize_datetime(model.created_at),
            "updated_at": serialize_datetime(model.updated_at),
        }

    def add_action_log(self, payload: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id or payload.get("tenant_id"))
        with self.connect() as session:
            model = ActionLog(
                tenant_id=resolved,
                execution_id=payload["execution_id"],
                step_id=payload.get("step_id"),
                action_name=payload.get("action_name"),
                url=payload.get("url"),
                method=payload.get("method"),
                request_body=copy.deepcopy(payload.get("request_body")),
                response_status=payload.get("response_status"),
                response_body=payload.get("response_body"),
                latency_ms=payload.get("latency_ms"),
                success=bool(payload.get("success", False)),
                retry_count=int(payload.get("retry_count", 0)),
                error=payload.get("error"),
                created_at=datetime.utcnow(),
            )
            session.add(model)
            session.flush()
            return self._action_log_to_dict(model)

    def list_action_logs(self, execution_id: Optional[str] = None, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            stmt = select(ActionLog).where(ActionLog.tenant_id == resolved)
            if execution_id:
                stmt = stmt.where(ActionLog.execution_id == execution_id)
            rows = session.scalars(stmt.order_by(desc(ActionLog.created_at))).all()
            return [self._action_log_to_dict(row) for row in rows]

    @staticmethod
    def _action_log_to_dict(model: ActionLog) -> Dict[str, Any]:
        return {
            "id": model.id,
            "tenant_id": model.tenant_id,
            "execution_id": model.execution_id,
            "step_id": model.step_id,
            "action_name": model.action_name,
            "url": model.url,
            "method": model.method,
            "request_body": copy.deepcopy(model.request_body or {}),
            "response_status": model.response_status,
            "response_body": model.response_body,
            "latency_ms": model.latency_ms,
            "success": model.success,
            "retry_count": model.retry_count,
            "error": model.error,
            "created_at": serialize_datetime(model.created_at),
        }

    # ───────────────────────────────────────────────────────────────────
    # MODEL HOSTING — persisted in the DB (hosted_models). Must NOT live in
    # process memory: the API runs multiple uvicorn workers / replicas, so an
    # in-memory store makes uploads invisible to other workers (random 404s)
    # and loses every model on restart.
    # ───────────────────────────────────────────────────────────────────
    def _hosted_model_to_dict(self, row: HostedModel, include_blob: bool = False) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "id": row.public_id,
            "name": row.name,
            "description": row.description,
            "model_type": row.model_type,
            "input_schema": copy.deepcopy(row.input_schema or {}),
            "output_schema": copy.deepcopy(row.output_schema or {}),
            "metrics": copy.deepcopy(row.metrics or {}),
            "status": row.status,
            "version": row.version,
            "has_predict": row.has_predict,
            "has_predict_proba": row.has_predict_proba,
            "last_test_result": copy.deepcopy(row.last_test_result or {}),
            "created_at": serialize_datetime(row.created_at),
            "updated_at": serialize_datetime(row.updated_at),
        }
        if include_blob:
            # Decrypt only for server-side execution; the blob is never sent over the API.
            out["model_blob"] = decrypt_model_blob(row.model_blob)
        return out

    def list_models(self, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            rows = session.scalars(select(HostedModel).where(HostedModel.tenant_id == resolved).order_by(desc(HostedModel.created_at))).all()
            return [self._hosted_model_to_dict(row) for row in rows]

    def get_model(self, model_id: str, include_blob: bool = False, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = session.scalar(select(HostedModel).where(HostedModel.tenant_id == resolved, HostedModel.public_id == model_id))
            return self._hosted_model_to_dict(row, include_blob=include_blob) if row else None

    def create_model(self, data: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = HostedModel(
                tenant_id=resolved,
                public_id=data["id"],
                name=data["name"],
                description=data.get("description"),
                model_type=data.get("model_type", "sklearn"),
                model_blob=encrypt_model_blob(data["model_blob"]),
                input_schema=copy.deepcopy(data.get("input_schema") or {}),
                output_schema=copy.deepcopy(data.get("output_schema") or {}),
                metrics=copy.deepcopy(data.get("metrics") or {}),
                status=data.get("status", "dev"),
                version=int(data.get("version", 1)),
                has_predict=bool(data.get("has_predict", True)),
                has_predict_proba=bool(data.get("has_predict_proba", False)),
                last_test_result={},
            )
            session.add(row)
            session.flush()
            return self._hosted_model_to_dict(row)

    def update_model(self, model_id: str, patch: Dict[str, Any], tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = session.scalar(select(HostedModel).where(HostedModel.tenant_id == resolved, HostedModel.public_id == model_id))
            if not row:
                return None
            for key in ("name", "description", "model_type", "input_schema", "output_schema", "metrics", "status", "version", "has_predict", "has_predict_proba", "last_test_result"):
                if key in patch:
                    setattr(row, key, patch[key])
            return self._hosted_model_to_dict(row)

    def delete_model(self, model_id: str, tenant_id: Optional[str] = None) -> bool:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            result = session.execute(delete(HostedModel).where(HostedModel.tenant_id == resolved, HostedModel.public_id == model_id))
            return result.rowcount > 0

    # ---- Decision tables --------------------------------------------------- #
    @staticmethod
    def _decision_table_to_dict(row: DecisionTable) -> Dict[str, Any]:
        return {
            "id": row.public_id,
            "name": row.name,
            "description": row.description,
            "hit_policy": row.hit_policy,
            "inputs": copy.deepcopy(row.inputs or []),
            "outputs": copy.deepcopy(row.outputs or []),
            "rows": copy.deepcopy(row.rows or []),
            "default_row": copy.deepcopy(row.default_row) if row.default_row else None,
            "status": row.status,
            "version": row.version,
            "last_test_result": copy.deepcopy(row.last_test_result) if row.last_test_result else None,
            "created_at": serialize_datetime(row.created_at),
            "updated_at": serialize_datetime(row.updated_at),
        }

    def list_decision_tables(self, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            rows = session.scalars(select(DecisionTable).where(DecisionTable.tenant_id == resolved).order_by(DecisionTable.name)).all()
            return [self._decision_table_to_dict(row) for row in rows]

    def get_decision_table(self, table_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = session.scalar(select(DecisionTable).where(DecisionTable.tenant_id == resolved, DecisionTable.public_id == table_id))
            return self._decision_table_to_dict(row) if row else None

    def create_decision_table(self, data: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id)
        now = datetime.utcnow()
        with self.connect() as session:
            row = DecisionTable(
                tenant_id=resolved,
                public_id=data["id"],
                name=data["name"],
                description=data.get("description"),
                hit_policy=data.get("hit_policy", "first"),
                inputs=copy.deepcopy(data.get("inputs") or []),
                outputs=copy.deepcopy(data.get("outputs") or []),
                rows=copy.deepcopy(data.get("rows") or []),
                default_row=copy.deepcopy(data.get("default_row")),
                status=data.get("status", "dev"),
                version=int(data.get("version", 1)),
                last_test_result=copy.deepcopy(data.get("last_test_result")),
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.add(EntityHistory(
                tenant_id=resolved, entity_type="decision_table", entity_id=data["id"],
                version=1, snapshot=self._decision_table_to_dict(row), created_at=now,
            ))
            session.flush()
            return self._decision_table_to_dict(row)

    def update_decision_table(self, table_id: str, patch: Dict[str, Any], tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = session.scalar(select(DecisionTable).where(DecisionTable.tenant_id == resolved, DecisionTable.public_id == table_id))
            if not row:
                return None
            for key in ("name", "description", "hit_policy", "inputs", "outputs", "rows", "default_row", "status", "last_test_result"):
                if key in patch:
                    setattr(row, key, copy.deepcopy(patch[key]))
            row.version = (row.version or 1) + 1
            row.updated_at = datetime.utcnow()
            session.add(EntityHistory(
                tenant_id=resolved, entity_type="decision_table", entity_id=table_id,
                version=row.version, snapshot=self._decision_table_to_dict(row), created_at=row.updated_at,
            ))
            return self._decision_table_to_dict(row)

    def delete_decision_table(self, table_id: str, tenant_id: Optional[str] = None) -> bool:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            result = session.execute(delete(DecisionTable).where(DecisionTable.tenant_id == resolved, DecisionTable.public_id == table_id))
            return result.rowcount > 0

    # ---- Report definitions ------------------------------------------------ #
    @staticmethod
    def _report_to_dict(row: ReportDefinition) -> Dict[str, Any]:
        return {
            "id": row.public_id,
            "name": row.name,
            "description": row.description,
            "columns": copy.deepcopy(row.columns or []),
            "filters": copy.deepcopy(row.filters or {}),
            "timezone": row.timezone,
            "schedule": copy.deepcopy(row.schedule) if row.schedule else None,
            "last_run": copy.deepcopy(row.last_run) if row.last_run else None,
            "version": row.version,
            "created_at": serialize_datetime(row.created_at),
            "updated_at": serialize_datetime(row.updated_at),
        }

    def list_reports(self, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            rows = session.scalars(select(ReportDefinition).where(ReportDefinition.tenant_id == resolved).order_by(ReportDefinition.name)).all()
            return [self._report_to_dict(row) for row in rows]

    def list_scheduled_reports(self) -> List[Dict[str, Any]]:
        """All reports (across tenants) with an enabled schedule — for the scheduler."""
        with self.connect() as session:
            rows = session.scalars(select(ReportDefinition)).all()
            out = []
            for row in rows:
                sched = row.schedule or {}
                if sched.get("enabled") and sched.get("cron"):
                    d = self._report_to_dict(row)
                    d["tenant_id"] = row.tenant_id
                    out.append(d)
            return out

    def get_report(self, report_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = session.scalar(select(ReportDefinition).where(ReportDefinition.tenant_id == resolved, ReportDefinition.public_id == report_id))
            return self._report_to_dict(row) if row else None

    def create_report(self, data: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id)
        now = datetime.utcnow()
        with self.connect() as session:
            row = ReportDefinition(
                tenant_id=resolved,
                public_id=data["id"],
                name=data["name"],
                description=data.get("description"),
                columns=copy.deepcopy(data.get("columns") or []),
                filters=copy.deepcopy(data.get("filters") or {}),
                timezone=data.get("timezone", "UTC"),
                schedule=copy.deepcopy(data.get("schedule")),
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return self._report_to_dict(row)

    def update_report(self, report_id: str, patch: Dict[str, Any], tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            row = session.scalar(select(ReportDefinition).where(ReportDefinition.tenant_id == resolved, ReportDefinition.public_id == report_id))
            if not row:
                return None
            for key in ("name", "description", "columns", "filters", "timezone", "schedule", "last_run"):
                if key in patch:
                    setattr(row, key, copy.deepcopy(patch[key]))
            if any(k in patch for k in ("name", "columns", "filters", "timezone", "schedule")):
                row.version = (row.version or 1) + 1
            row.updated_at = datetime.utcnow()
            return self._report_to_dict(row)

    def delete_report(self, report_id: str, tenant_id: Optional[str] = None) -> bool:
        resolved = self._tenant_id(tenant_id)
        with self.connect() as session:
            result = session.execute(delete(ReportDefinition).where(ReportDefinition.tenant_id == resolved, ReportDefinition.public_id == report_id))
            return result.rowcount > 0

    # ---- Durable email outbox (report delivery) -------------------------- #
    @staticmethod
    def _outbox_to_dict(row: EmailOutbox) -> Dict[str, Any]:
        return {
            "id": row.id, "tenant_id": row.tenant_id, "recipients": list(row.recipients or []),
            "subject": row.subject, "body": row.body, "csv_content": row.csv_content,
            "csv_filename": row.csv_filename, "status": row.status, "attempts": row.attempts,
            "last_error": row.last_error, "created_at": serialize_datetime(row.created_at),
        }

    def enqueue_email(self, data: Dict[str, Any], tenant_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = self._tenant_id(tenant_id or data.get("tenant_id"))
        with self.connect() as session:
            row = EmailOutbox(
                tenant_id=resolved,
                recipients=copy.deepcopy(data.get("recipients") or []),
                subject=data.get("subject", ""),
                body=data.get("body", ""),
                csv_content=data.get("csv_content", ""),
                csv_filename=data.get("csv_filename", "report.csv"),
                status=data.get("status", "pending"),
                attempts=int(data.get("attempts", 0)),
                last_error=data.get("last_error"),
            )
            session.add(row)
            session.flush()
            return self._outbox_to_dict(row)

    def list_pending_emails(self, max_attempts: int = 5, limit: int = 100) -> List[Dict[str, Any]]:
        with self.connect() as session:
            rows = session.scalars(
                select(EmailOutbox).where(EmailOutbox.status == "pending", EmailOutbox.attempts < max_attempts)
                .order_by(EmailOutbox.created_at).limit(limit)
            ).all()
            return [self._outbox_to_dict(row) for row in rows]

    def mark_email(self, email_id: str, status: str, error: Optional[str] = None, increment_attempt: bool = False) -> None:
        with self.connect() as session:
            row = session.get(EmailOutbox, email_id)
            if not row:
                return
            row.status = status
            if error is not None:
                row.last_error = error
            if increment_attempt:
                row.attempts = (row.attempts or 0) + 1
            row.updated_at = datetime.utcnow()

    def count_outbox(self, status: Optional[str] = None, tenant_id: Optional[str] = None) -> int:
        with self.connect() as session:
            stmt = select(func.count()).select_from(EmailOutbox)
            if tenant_id:
                stmt = stmt.where(EmailOutbox.tenant_id == self._tenant_id(tenant_id))
            if status:
                stmt = stmt.where(EmailOutbox.status == status)
            return int(session.scalar(stmt) or 0)

    # ---- Scheduler leader lease (multi-replica single-fire) --------------- #
    def try_acquire_scheduler_lease(self, owner: str, ttl_seconds: int = 30) -> bool:
        """Atomically acquire or renew the singleton scheduler lease. Returns True
        iff this owner now holds it. Race-free across replicas: the conditional
        UPDATE is serialised by the DB, so at most one owner wins per interval."""
        now = datetime.utcnow()
        until = now + timedelta(seconds=ttl_seconds)
        with self.connect() as session:
            # Take (or keep) leadership if we already own it, or the lease expired.
            updated = session.execute(
                update(SchedulerLease)
                .where(SchedulerLease.id == "singleton",
                       or_(SchedulerLease.owner == owner, SchedulerLease.lease_until < now))
                .values(owner=owner, lease_until=until, updated_at=now)
            )
            if updated.rowcount and updated.rowcount > 0:
                return True
            # No row updated: either it exists and is held by someone else, or it
            # has never been created. Try to create it (first replica to boot wins).
            if session.get(SchedulerLease, "singleton") is None:
                try:
                    session.add(SchedulerLease(id="singleton", owner=owner, lease_until=until, updated_at=now))
                    session.flush()
                    return True
                except IntegrityError:
                    session.rollback()  # another replica created it first
                    return False
            return False

    def release_scheduler_lease(self, owner: str) -> None:
        """Relinquish leadership on graceful shutdown so another replica takes over
        immediately instead of waiting for the lease to expire."""
        now = datetime.utcnow()
        with self.connect() as session:
            session.execute(
                update(SchedulerLease)
                .where(SchedulerLease.id == "singleton", SchedulerLease.owner == owner)
                .values(lease_until=now, updated_at=now)
            )
