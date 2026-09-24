from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (JSON, BigInteger, Boolean, Date, DateTime, Float, Index, Integer,
                        LargeBinary, String, Text, UniqueConstraint)
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
    # A device-supplied client id is unique only *within a tenant* (two tenants may independently
    # mint the same id). The primary key is a server-generated id; dedupe of on-device decisions is
    # by (tenant_id, client_id), so an at-least-once retry never double-counts and one tenant's id can
    # never collide with another's. client_id is NULL for server/API decisions (many NULLs allowed).
    #
    # That last sentence was the contract and the /decide path broke it: it wrote the request's
    # `userId` into client_id so case-wise history had something to filter on. A userId is a SUBJECT
    # identifier, not an idempotency key -- it repeats on every re-decision of the same customer --
    # so the second decision for any customer hit this constraint and was silently dropped (the
    # write is async, so the caller already had a 200 and a decision_id that never existed).
    # user_id below is the subject; client_id stays the device idempotency key.
    __table_args__ = (UniqueConstraint("tenant_id", "client_id", name="uq_decisions_tenant_client"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    client_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    # The decision SUBJECT (/decide's `userId`). Deliberately NOT unique -- "every decision for
    # this customer" is the whole point of case-wise history.
    user_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
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
    experiment_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    experiment_variant: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # On-device sync lineage: the device that produced the decision, and the bundle version/hash it
    # ran against (so an offline decision made on an old bundle is traceable). Null for API decisions.
    device_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    bundle_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # WHICH VERSION of the policy decided this. Policies are mutated in place with `version`
    # incrementing on the row, so policy_id alone cannot answer "what was live eight months
    # ago" -- it had to be inferred from created_at against EntityHistory, by hand.
    # Nullable: rows written before 20260924_0031 genuinely do not know, and backfilling a
    # guess would put fabricated lineage into an attestable record.
    policy_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Set when the subject's personal data was erased (data_subjects.erase_subject): the payload,
    # variables, trace and scorecard are gone; the outcome, policy version, time and adverse-action
    # reason codes are kept, because lenders must retain them. Migration 20260924_0035.
    erased_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # created_at is the DECISION time (device clock for on-device decisions — subject to drift);
    # received_at is the trustworthy SERVER receipt time, used for ordering/retention/analytics.
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)


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
    # The entity's decision definition at promotion time — lets us diff what actually changed
    # between successive promotions (draft-vs-live), and record that diff on the approval.
    snapshot_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
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
    # 512, not 128: this column holds a Fernet token when AUDIT_ENCRYPT_AT_REST is on, and an
    # encrypted IPv6 address lands around 140 characters. SQLite does not enforce declared
    # widths, so the old 128 would have failed only on Postgres, only on IPv6, in production.
    ip_address: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    # --- tamper-evident chain (see app/audit_chain.py) ---
    # All three are NULLABLE on purpose. Rows written before the chain existed keep NULLs and are
    # reported as `pre_chain` -- explicitly NOT verified. Backfilling them with computed hashes
    # would make forged-looking history verify clean, which is worse than admitting the gap.
    seq: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    prev_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        # Two concurrent writers computing the same next seq collide here rather than forking
        # the chain -- the loser retries. NULL seqs (legacy rows) are distinct under SQL NULL
        # semantics on both SQLite and Postgres, so this does not constrain them.
        UniqueConstraint("tenant_id", "seq", name="uq_audit_events_tenant_seq"),
    )


class UsageRecord(Base):
    """Metered usage, bucketed per tenant per UTC day per metric.

    DAILY buckets, not monthly. A monthly counter cannot answer "show me the trend", cannot
    prorate a mid-month plan change, and cannot be recomputed if a period boundary was wrong --
    and a month is just a SUM over days, so nothing is lost by storing the finer grain.

    Contrast with the pre-existing AI counters, which live in `Tenant.config["ai_usage"]` as
    lifetime totals with no time dimension. Those cannot be invoiced from: there is no way to
    ask what a workspace used in August. This table is the billable record; that blob stays as
    a convenience display.
    """

    __tablename__ = "usage_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    # Stored as a DATE, not a timestamp: the bucket IS the day, and a time component would let
    # two writers create two rows for the same day and silently split the count.
    day: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    metric: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        # The upsert target. Without this a concurrent increment inserts a second row for the
        # same bucket and the tenant is under-billed -- the failure mode that silently loses
        # revenue rather than loudly erroring.
        UniqueConstraint("tenant_id", "day", "metric", name="uq_usage_tenant_day_metric"),
    )


class DecisionOutbox(Base):
    """The transactional outbox: one row per decision that still has to reach the broker.

    THE POINT IS THE TRANSACTION, not the table. A decision is written to Postgres and published
    to Kafka, and those are two different systems -- so "write then publish" loses the message
    whenever the process dies in between, and "publish then write" emits a decision that never
    existed. Neither is recoverable after the fact, because nothing durable records the gap.

    Writing this row in the SAME transaction as the Decision closes it: the row and the decision
    commit together or neither does. A relay then moves committed rows to Kafka and marks them
    sent, which is at-least-once by construction. Exactly-once comes from the consumer side
    (idempotency_records keyed by the outbox id), not from pretending delivery is reliable.

    Deliberately NOT reusing the existing decision WAL. decision_wal.py exists to survive a
    database that is unreachable -- it is a file on local disk holding decisions already returned
    to a caller. This is the opposite situation: the database is the thing that succeeded, and
    the broker is what might not have. Different failure, different durability, different table.
    """

    __tablename__ = "decision_outbox"
    __table_args__ = (
        # The relay's only hot query is "oldest unsent rows first". Without this it is a
        # sequential scan over a table that grows with every decision ever made.
        Index("ix_decision_outbox_unsent", "sent_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    decision_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    # The Kafka partition key. Per-tenant, so one tenant's decisions keep their relative order
    # (Kafka orders within a partition, never across) while different tenants parallelise.
    partition_key: Mapped[str] = mapped_column(String(128), nullable=False)
    topic: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # NULL until the relay has an acknowledged produce. This is the whole state machine.
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class IdempotencyRecord(Base):
    """One remembered response for an Idempotency-Key, scoped to a tenant.

    Keyed per TENANT, not globally: two customers picking the same uuid must not collide, and a
    globally unique key would let one tenant probe another's key space.

    `request_hash` is what makes this safe rather than merely convenient. Replaying a key with a
    DIFFERENT body is a client bug -- usually a key generated once and reused across distinct
    operations -- and returning the first response for it would silently drop the second write.
    The hash turns that into a loud 422 instead.
    """

    __tablename__ = "idempotency_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # NULL while the first request is still running -- that is how a concurrent duplicate is
    # detected and answered 409 rather than being allowed to execute the write a second time.
    status_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    response_body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    __table_args__ = (
        # The claim. Two concurrent requests with one key race to INSERT here; exactly one wins
        # and executes, the loser gets 409. Without this both would execute the write.
        UniqueConstraint("tenant_id", "key", name="uq_idempotency_tenant_key"),
    )


class LoginAttempt(Base):
    """Failed-credential counters that survive a restart and are shared across replicas.

    The counter had two tiers: Redis when configured, and a per-PROCESS dict otherwise. The
    fallback is what most deployments actually run on, and it is not a lockout:

      * restart the pod and the counter is gone, so an attacker who trips the limit just waits
        for the next deploy, crash or scale event and resumes from zero;
      * run two replicas and each keeps its own count, so N replicas mean N times the attempts
        before anyone is locked out -- and a round-robin load balancer hands the attacker that
        multiplier for free.

    Neither is visible from the outside: the endpoint returns 429 exactly as it should, right up
    until it silently doesn't. The database is the one store every replica already shares and
    that outlives a process, so it is the correct fallback; Redis stays the fast path.

    Keyed by the same salted digest as the Redis bucket (`middleware._login_bucket`), so the two
    tiers agree on identity and neither stores an email address. No tenant_id: the platform-admin
    account has no tenant, and the key already carries the scope.
    """

    __tablename__ = "login_attempts"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Set once on the FIRST failure and never refreshed. Extending the window on every attempt
    # would let an attacker hold a victim's account locked indefinitely just by continuing to
    # guess -- the same rule the Redis tier applies with its one-shot EXPIRE.
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)


class SamlAssertionSeen(Base):
    """One consumed SAML assertion id, so it cannot be replayed (T3.5).

    SAML has no built-in replay defence: an assertion is a signed bearer token, and the signature
    stays valid for as long as the IdP's certificate does. Without this table, an attacker who
    captured one POST to the ACS endpoint -- from a proxy log, a shared browser, an intercepted
    redirect -- could replay it indefinitely and log in as that user forever.

    Keyed per tenant, matching IdempotencyRecord's reasoning: two IdPs can legitimately mint the
    same assertion ID, and a global key would let one workspace probe another's.

    `not_on_or_after` is stored so consumed rows can be pruned once the assertion could no longer
    be accepted anyway -- the table would otherwise grow forever.
    """

    __tablename__ = "saml_assertions_seen"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    assertion_id: Mapped[str] = mapped_column(String(255), nullable=False)
    not_on_or_after: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        # The claim: two concurrent POSTs of the same assertion race to INSERT, exactly one wins.
        # Without this a replay is only "usually" caught.
        UniqueConstraint("tenant_id", "assertion_id", name="uq_saml_assertion_tenant_id"),
    )


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
    # Promotion provenance. `promote_experiment` has always WRITTEN these three -- there were no
    # columns to receive them, and `create_or_update_experiment` copies only known fields, so the
    # write reported success and the record of what was promoted and by whom was dropped. A later
    # GET returned promoted_variant_id: None on an experiment whose status was "completed".
    #
    # The audit event still carries it, but an audit log answers "what happened" and this answers
    # "what is true now" -- reconstructing the live champion by replaying audit rows is not a
    # reasonable thing to ask of a caller.
    promoted_variant_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    promoted_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    promoted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


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


class Predictor(Base, TimestampMixin):
    """A named number a decision already produces, tracked over time (P20).

    It POINTS at a value rather than computing one -- see app/predictors.py for why -- so the
    columns are a pointer (source_type + source_id) plus a baseline window, not a definition.
    """

    __tablename__ = "predictors"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_predictors_tenant_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    # How many days back the baseline window reaches, and how many days the current window
    # covers. Stored per predictor rather than passed per request so that two people asking "is
    # it drifting?" get the same answer -- a drift number is only comparable against its own
    # history if the windows are fixed.
    baseline_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    current_days: Mapped[int] = mapped_column(Integer, default=7, nullable=False)
    bins: Mapped[int] = mapped_column(Integer, default=10, nullable=False)


class PredictorValue(Base):
    """One observation. Deliberately narrow and unencrypted: a score with no identifiers beside
    it is not personal data, and this table is read in bulk for a distribution -- decrypting a
    field per row would make a 30-day window a per-request cost rather than a query."""

    __tablename__ = "predictor_values"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    predictor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    decision_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        # The only query this table serves: one predictor's values in a time window, for one
        # tenant. A composite index in that order is what keeps a 30-day baseline from becoming a
        # full scan as the table grows with decision volume.
        Index("ix_predictor_values_lookup", "tenant_id", "predictor_id", "created_at"),
    )


class WebhookSubscription(Base, TimestampMixin):
    """OUTBOUND. The inverse of `Webhook` above, which is inbound (an endpoint_path a partner
    POSTs to in order to trigger a policy). This says: POST to `url` whenever an event of one of
    `event_types`, matching every one of `filters`, happens in this workspace.

    A separate table rather than a flag on `Webhook`: the two share no column that means the same
    thing. `Webhook` has an endpoint_path and a policy_id (which policy a caller may trigger);
    this has a url, an event selection and a signing secret (where WE send, and how the receiver
    verifies us). Overloading one row would make every query on either say "and the other kind".
    """

    __tablename__ = "webhook_subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    event_types: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    filters: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # Encrypted at rest with the same helper the inbound webhook secret uses, and returned in
    # plaintext exactly once, at creation. Nullable so an unsigned subscription is expressible.
    secret: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    timeout_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    retries: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Delivery health, so a subscriber that has been silently failing is visible without
    # trawling logs. Not a delivery LOG -- that would grow with event volume; this is the
    # last-known state of the one thing an operator asks about ("is it working?").
    last_delivery_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_delivery_ok: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    last_delivery_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class WebhookDelivery(Base):
    """One outbound webhook delivery, durable. THIS TABLE DID NOT EXIST.

    `webhook_dispatch.py`'s own docstring promised that a failed delivery is "retried by the
    scheduler, and dead-lettered after a bound". What shipped was a bare daemon thread per event
    per subscription, retrying in a tight loop inside that thread, persisting only LAST-KNOWN
    STATE onto the subscription row (whose own comment says "Not a delivery LOG"). So a rolling
    deploy or a SIGTERM dropped every in-flight event with no record that it had existed, a
    receiver down for ten minutes lost everything emitted in those ten minutes, and there was no
    dead-letter queue to replay from. `decision_outbox`, one file over, did this correctly.

    Now the event is written HERE first -- a cheap insert on the caller's path -- and delivery
    happens afterwards: promptly by a single in-process worker, and durably by a leader-gated
    scheduler drain that also picks up anything a crashed process left behind. Delivery is
    AT-LEAST-ONCE by construction; the `id` is sent as the delivery id so a receiver can dedupe.
    """

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        # The drain's only hot query: "due rows, oldest first".
        Index("ix_webhook_deliveries_due", "status", "next_attempt_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    subscription_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # pending -> delivering -> delivered | pending (retry) | dead
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


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
    # Retention in CALENDAR months — the unit contracts and regulators are actually written in
    # ("retain decisions for 7 years", "purge PII after 24 months"). Days is a lossy encoding of
    # that: 7 years is 2557 days from today, not the 2555 a hand conversion produces.
    #
    # NULL means "this workspace never set months", and audit_retention_days stays authoritative
    # — that is how every pre-existing tenant keeps the window it already had. Non-NULL wins over
    # days, and audit_retention_days is then maintained as a mirror so the readers that still
    # speak days (the /settings GET, workspace export, the experience-studio form, the web
    # dashboard) stay approximately right. `archiver.retention_cutoff` is the one authority on
    # which of the two applies; nothing should re-derive that precedence itself.
    audit_retention_months: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    theme_mode: Mapped[str] = mapped_column(String(16), default="light", nullable=False)
    # Admin-only, config-driven white-label branding (accent/CTA colour, background,
    # sidebar, brand name/logo, hidden nav items). Empty values fall back to theme defaults.
    branding: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # AI Copilot config: default provider + per-provider {key_encrypted, model}.
    # Keys are Fernet-encrypted; never returned to the client (masked on read).
    ai_config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class HostedModel(Base):
    """A hosted ML model (ONNX, PMML, XGBoost, LightGBM or pickle) usable as a policy "model" step.
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
    # Detected from the blob at upload (model_executor.detect_format), stored for listing only --
    # scoring always re-reads the bytes. Migration 20260924_0034. Nullable so the SQLite
    # reconciler can add it to an existing dev database; absent means pickle, the only format
    # there was before this column.
    model_format: Mapped[Optional[str]] = mapped_column(String(24), default="pickle", server_default="pickle", nullable=True)
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


class ModelSet(Base, TimestampMixin):
    """A versioned group of hosted models with a routing rule (P05).

    Members live in a JSON column rather than a second table because they are an ORDERED list
    whose order is the routing semantics -- first match wins. A child table would need an explicit
    position column and an ordering join to reproduce exactly that, for a list bounded at 20.
    """

    __tablename__ = "model_sets"
    __table_args__ = (UniqueConstraint("tenant_id", "public_id", name="uq_model_sets_tenant_public"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    public_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # [{model_id, label, condition, pinned_version}, ...] -- see app/modelsets.py.
    members: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(24), default="dev", nullable=False)


class Journey(Base, TimestampMixin):
    """An end-user-facing flow of screens that collects a payload and runs a policy (P15).

    THE SHAPE IS NOT NEW. `app/experience_studio.py` has carried it since the mobile studio
    shipped -- id, title, category, policyId, and an ordered list of typed screens -- it is served
    at `/sdk/v1/experience-manifest`, and `packages/sdk-flutter/example` renders and RUNS it. What
    was missing is that the manifest returns a deep copy of one static template, so every tenant
    saw the same three demo journeys and no workspace could author its own. This table is that
    template's shape, per tenant.

    `screens` is a JSON column for the reason `ModelSet.members` is: it is an ORDERED list whose
    order is the semantics (screen 2 follows screen 1), and a child table would need a position
    column and an ordering join to say exactly that.

    LOCKED is separate from `status`. Status is the dev/uat/prod lifecycle every authoring entity
    has; `is_locked` is "this journey is in the field and its screens must not move under the
    people filling them in". A locked journey can still be promoted, and can still be duplicated
    -- duplication is the intended way to change one.
    """

    __tablename__ = "journeys"
    __table_args__ = (UniqueConstraint("tenant_id", "public_id", name="uq_journeys_tenant_public"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    public_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The policy this journey's collected payload is decided by.
    policy_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    primary_color: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # [{id, title, subtitle, type, fields: [...]}, ...] -- experience_studio's screen shape.
    screens: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    is_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(24), default="dev", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class ModelEvaluation(Base):
    """A predictive-model evaluation run (Gini/AUC/KS/PR-AUC/calibration/PSI/…) against
    ground-truth labels, optionally tied to a hosted model. Persisted so results survive
    restarts and gate dev→uat→prod promotion. Only metrics + a dataset summary are stored —
    never the raw scored rows (PII-minimising, mirrors decision-payload redaction)."""

    __tablename__ = "model_evaluations"
    __table_args__ = (UniqueConstraint("tenant_id", "public_id", name="uq_model_evaluations_tenant_public"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    public_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Optional link to the hosted model that produced the scores (from-model evaluations).
    model_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    task: Mapped[str] = mapped_column(String(24), default="binary", nullable=False)  # binary|multilabel|uplift
    dataset_summary: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    segments: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    temporal: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    gate_status: Mapped[str] = mapped_column(String(24), default="unknown", nullable=False)  # pass|fail|unknown
    gate_result: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="dev", nullable=False)
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
