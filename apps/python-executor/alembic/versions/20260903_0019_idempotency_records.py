"""Create idempotency_records — remembered responses for Idempotency-Key (Track B6).

A client retrying a POST after a timeout cannot know whether the first attempt was applied.
Without a key the retry silently duplicates the write. This table lets the server replay the
original response instead of executing again.

Opt-in per request: no header, no behaviour change, so existing integrations are unaffected
by the upgrade.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260903_0019"
down_revision = "20260903_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "idempotency_records" in inspector.get_table_names():
        return
    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        # NULL while the first request is still running -- how a concurrent duplicate is
        # detected and answered 409 instead of executing the write twice.
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        # The claim. Concurrent requests with one key race at this constraint; exactly one wins.
        sa.UniqueConstraint("tenant_id", "key", name="uq_idempotency_tenant_key"),
    )
    op.create_index("ix_idempotency_records_tenant_id", "idempotency_records", ["tenant_id"])
    op.create_index("ix_idempotency_records_created_at", "idempotency_records", ["created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "idempotency_records" not in inspector.get_table_names():
        return
    for name in ("ix_idempotency_records_created_at", "ix_idempotency_records_tenant_id"):
        op.drop_index(name, table_name="idempotency_records")
    op.drop_table("idempotency_records")
