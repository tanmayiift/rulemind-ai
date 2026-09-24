"""Durable outbound webhook deliveries.

`webhook_dispatch.py` promised that a failed delivery is "retried by the scheduler, and
dead-lettered after a bound". What shipped was a daemon thread per event per subscription with an
in-thread retry loop and last-known state on the subscription row -- so a restart dropped every
in-flight event with no record, and there was nothing to replay from. This table is the record:
an event is written here before delivery is attempted, and the scheduler drain delivers, retries
with backoff and dead-letters from it.

Idempotent per table, the house pattern: Storage.__init__ runs create_all, so on a fresh database
the table already exists by the time this runs.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260924_0032"
down_revision = "20260924_0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("webhook_deliveries"):
        return
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("subscription_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("event", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("last_status", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_webhook_deliveries_tenant_id", "webhook_deliveries", ["tenant_id"])
    op.create_index("ix_webhook_deliveries_subscription_id", "webhook_deliveries", ["subscription_id"])
    op.create_index("ix_webhook_deliveries_status", "webhook_deliveries", ["status"])
    op.create_index("ix_webhook_deliveries_due", "webhook_deliveries", ["status", "next_attempt_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("webhook_deliveries"):
        op.drop_table("webhook_deliveries")
