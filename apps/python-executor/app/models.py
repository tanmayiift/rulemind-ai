from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def uuid4_str() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class Tenant(Base, TimestampMixin):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    api_key_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    plan: Mapped[str] = mapped_column(String(32), default="standard", nullable=False)
    config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_bundle_queued_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class ApiKey(Base, TimestampMixin):
    __tablename__ = "api_keys"
    __table_args__ = (UniqueConstraint("tenant_id", "kid", name="uq_api_keys_tenant_kid"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    kid: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    masked_key: Mapped[str] = mapped_column(String(128), nullable=False)
    lookup_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Environment this key is scoped to (dev / prod / sandbox) + a human label.
    environment: Mapped[str] = mapped_column(String(16), default="prod", nullable=False)
    label: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # RBAC role governing what this key may do (owner/admin/policy_maker/reviewer/viewer).
    role: Mapped[str] = mapped_column(String(32), default="owner", nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class PlatformAdminUser(Base, TimestampMixin):
    __tablename__ = "platform_admin_users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class WorkspaceMember(Base, TimestampMixin):
    """A human account scoped to a workspace (tenant), carrying an RBAC role.

    Roles are the same capability roles the API keys use (owner/admin/policy_maker/
    reviewer/viewer) — a member logs in (password or email OTP; SSO later) and their
    session is authorised exactly like a role-scoped key. `password_hash` is nullable
    so SSO-only members can exist without a local password."""

    __tablename__ = "workspace_members"
    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_members_tenant_email"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="viewer", nullable=False)
    password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # How this member authenticates: password / oidc / saml (SSO providers land later).
    auth_provider: Mapped[str] = mapped_column(String(16), default="password", nullable=False)
    external_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class MemberSession(Base):
    """A logged-in member's bearer session. The token is stored hashed (never in
    plaintext); resolving a session reads the member's role LIVE, so an admin's
    role change or deactivation takes effect immediately (bounded by a short cache
    TTL, and eagerly on change)."""

    __tablename__ = "member_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    member_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class MemberOtp(Base):
    """A one-time passcode for email login. Stored hashed with a short expiry and a
    small attempt budget; consumed on first successful verify."""

    __tablename__ = "member_otps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    code_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class Connector(Base, TimestampMixin):
    __tablename__ = "connectors"
    __table_args__ = (UniqueConstraint("tenant_id", "public_id", name="uq_connectors_tenant_public_id"),)

    pk: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    public_id: Mapped[str] = mapped_column("public_id", String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    icon: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    color: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    schema_fields: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    sample_payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    encrypted_config: Mapped[str] = mapped_column(Text, default="", nullable=False)


class Variable(Base, TimestampMixin):
    __tablename__ = "variables"
    __table_args__ = (UniqueConstraint("tenant_id", "public_id", name="uq_variables_tenant_public_id"),)

    pk: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    public_id: Mapped[str] = mapped_column("public_id", String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(128), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="dev", nullable=False, index=True)
    last_test_result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class Rule(Base, TimestampMixin):
    __tablename__ = "rules"
    __table_args__ = (UniqueConstraint("tenant_id", "public_id", name="uq_rules_tenant_public_id"),)

    pk: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    public_id: Mapped[str] = mapped_column("public_id", String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    rule_format: Mapped[str] = mapped_column(String(16), default="v1", nullable=False)
    nodes: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    tree: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    expression: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="dev", nullable=False, index=True)
    last_test_result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class ReportDefinition(Base, TimestampMixin):
    """A saved report over the decision log: dynamic columns + filters + timezone,
    optionally delivered on a schedule by email."""

    __tablename__ = "report_definitions"
    __table_args__ = (UniqueConstraint("tenant_id", "public_id", name="uq_reports_tenant_public"),)

    pk: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    public_id: Mapped[str] = mapped_column("public_id", String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    columns: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    filters: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    schedule: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    last_run: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class Scorecard(Base, TimestampMixin):
    __tablename__ = "scorecards"
    __table_args__ = (UniqueConstraint("tenant_id", "public_id", name="uq_scorecards_tenant_public_id"),)

    pk: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    public_id: Mapped[str] = mapped_column("public_id", String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    base_score: Mapped[int] = mapped_column(Integer, default=300, nullable=False)
    max_score: Mapped[int] = mapped_column(Integer, default=900, nullable=False)
    bins: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="dev", nullable=False, index=True)
    last_test_result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class Policy(Base, TimestampMixin):
    __tablename__ = "policies"
    __table_args__ = (UniqueConstraint("tenant_id", "public_id", name="uq_policies_tenant_public_id"),)

    pk: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    public_id: Mapped[str] = mapped_column("public_id", String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    trigger: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    steps: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    default_outcome: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="dev", nullable=False, index=True)
    # Lifecycle stage, orthogonal to the dev/uat/prod `status` environment:
    # draft -> in_review -> ready -> live, plus rejected / archived.
    lifecycle_status: Mapped[str] = mapped_column(String(24), default="draft", nullable=False, index=True)
    last_test_result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class Decision(Base):
    __tablename__ = "decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    policy_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    payload_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_preview: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    computed_variables: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    rule_results: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    scorecard_result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    trace: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="api", nullable=False)
    sdk_version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    experiment_variant: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class Promotion(Base):
    __tablename__ = "promotions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    from_status: Mapped[str] = mapped_column(String(16), nullable=False)
    to_status: Mapped[str] = mapped_column(String(16), nullable=False)
    promoted_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entity_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[Optional[dict]] = mapped_column("metadata_json", JSON, nullable=True)
    user_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class ErrorEvent(Base):
    __tablename__ = "error_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details_json: Mapped[dict] = mapped_column("details_json", JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class Bundle(Base):
    __tablename__ = "bundles"
    __table_args__ = (UniqueConstraint("tenant_id", "version", name="uq_bundles_tenant_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    content: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    encrypted_content: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    signature: Mapped[str] = mapped_column(Text, nullable=False)
    checksum: Mapped[str] = mapped_column(String(255), nullable=False)
    superseded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    compiled_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class SdkEvent(Base):
    __tablename__ = "sdk_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class Experiment(Base, TimestampMixin):
    __tablename__ = "experiments"
    __table_args__ = (UniqueConstraint("tenant_id", "public_id", name="uq_experiments_tenant_public_id"),)

    pk: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    public_id: Mapped[str] = mapped_column("public_id", String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    variants: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    hash_key: Mapped[str] = mapped_column(String(64), default="user_id", nullable=False)
    target_policy_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    start_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class WorkflowExecution(Base, TimestampMixin):
    __tablename__ = "workflow_executions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    policy_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="running", nullable=False, index=True)
    context: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    current_step_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    trigger_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    trigger_metadata: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    paused_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class ReviewTask(Base):
    __tablename__ = "review_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    execution_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    policy_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    step_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    queue: Mapped[str] = mapped_column(String(128), default="default", nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    context_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    required_fields: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    reviewer_response: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    assigned_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    timeout_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class Webhook(Base, TimestampMixin):
    __tablename__ = "webhooks"
    __table_args__ = (UniqueConstraint("tenant_id", "endpoint_path", name="uq_webhooks_tenant_endpoint"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    policy_id: Mapped[str] = mapped_column(String(128), nullable=False)
    endpoint_path: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    secret_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    payload_mapping: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)


class CronSchedule(Base, TimestampMixin):
    __tablename__ = "cron_schedules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    policy_id: Mapped[str] = mapped_column(String(128), nullable=False)
    cron_expression: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    next_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    payload_source: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)


class ActionLog(Base):
    __tablename__ = "action_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    execution_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    step_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    action_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    method: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    request_body: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    response_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    response_body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class EntityHistory(Base):
    __tablename__ = "entity_histories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class Setting(Base):
    __tablename__ = "settings"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_settings_tenant"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    api_base_url: Mapped[str] = mapped_column(String(255), nullable=False)
    auth_config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    engine_config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    source_defaults: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    audit_retention_days: Mapped[int] = mapped_column(Integer, default=90, nullable=False)
    theme_mode: Mapped[str] = mapped_column(String(16), default="light", nullable=False)
    # Admin-only, config-driven white-label branding (accent/CTA colour, background,
    # sidebar, brand name/logo, hidden nav items). Empty values fall back to theme defaults.
    branding: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # AI Copilot config: default provider + per-provider {key_encrypted, model}.
    # Keys are Fernet-encrypted; never returned to the client (masked on read).
    ai_config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class HostedModel(Base):
    """A hosted ML model (pickled sklearn/xgboost/…) usable as a policy "model" step.
    Persisted in the DB (not in-process memory) so it survives restarts and is
    consistent across uvicorn workers / replicas."""

    __tablename__ = "hosted_models"
    __table_args__ = (UniqueConstraint("tenant_id", "public_id", name="uq_hosted_models_tenant_public"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    public_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    model_type: Mapped[str] = mapped_column(String(64), default="sklearn", nullable=False)
    model_blob: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    input_schema: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    output_schema: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="dev", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    has_predict: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    has_predict_proba: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_test_result: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class DecisionTable(Base, TimestampMixin):
    """Spreadsheet-style decision table: input columns bound to variables, rows
    stating a condition per input and the output(s) to emit. Compiles to the
    same executable semantics as rules; runnable as a policy "decision_table"
    step. Authoring artifact — persisted, versioned, MECE/optimiser-checked."""

    __tablename__ = "decision_tables"
    __table_args__ = (UniqueConstraint("tenant_id", "public_id", name="uq_decision_tables_tenant_public"),)

    pk: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    public_id: Mapped[str] = mapped_column("public_id", String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    hit_policy: Mapped[str] = mapped_column(String(16), default="first", nullable=False)
    inputs: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    outputs: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    rows: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    default_row: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="dev", nullable=False, index=True)
    last_test_result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class SchedulerLease(Base):
    """Single-row leader lease so that, across N API replicas, exactly one runs the
    scheduled jobs (cron policies, report delivery, review timeouts). A replica
    holds leadership only while its lease is unexpired; acquisition is a race-free
    atomic UPDATE (see storage.try_acquire_scheduler_lease)."""

    __tablename__ = "scheduler_lease"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default="singleton")
    owner: Mapped[str] = mapped_column(String(64), nullable=False)
    lease_until: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class EmailOutbox(Base):
    """Durable outbox for report emails. A message is enqueued here when SMTP is
    unconfigured or a send fails, and a leader-gated retry job drains it — so a
    scheduled report is never silently lost (unlike an in-process list)."""

    __tablename__ = "email_outbox"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    recipients: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    csv_content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    csv_filename: Mapped[str] = mapped_column(String(255), default="report.csv", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
