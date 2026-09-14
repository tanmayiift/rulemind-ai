"""Add webhook_subscriptions — outbound webhooks with event types and filters (parity P03).

The product could receive a webhook and could not send one, except through the ad-hoc `alertUrl`
on a monitor step: one URL hard-wired into one step of one policy, fired once, best-effort, with
the exception swallowed into `result["alertError"]`. Nothing could say "tell this URL whenever
ANY policy in this workspace is promoted", nothing was signed, and nothing retried.

A separate table from `webhooks` rather than a flag on it. The two share no column that means the
same thing: `webhooks` has an endpoint_path and a policy_id (which policy an inbound caller may
trigger), this has a url, an event selection and a signing secret (where WE send, and how the
receiver verifies us). Overloading one row would put "and the other kind" into every query on
either.

`secret` is Text rather than String(255) because it stores a Fernet token, not the 43-character
token_urlsafe(32) that goes in: encrypting a 43-char secret lands near 200 characters and the
margin is not worth a column-width failure that only appears on Postgres, only in production —
the same lesson as audit_events.ip_address at 512.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260905_0025"
down_revision = "20260904_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent, matching the house pattern: Storage.__init__ runs create_all, so on a fresh
    # database the table already exists by the time this runs and a bare create_table would
    # abort the whole chain.
    bind = op.get_bind()
    if sa.inspect(bind).has_table("webhook_subscriptions"):
        return
    op.create_table(
        "webhook_subscriptions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("url", sa.String(length=1024), nullable=False),
        sa.Column("event_types", sa.JSON(), nullable=False),
        sa.Column("filters", sa.JSON(), nullable=False),
        sa.Column("secret", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("timeout_ms", sa.Integer(), nullable=True),
        sa.Column("retries", sa.Integer(), nullable=True),
        sa.Column("last_delivery_at", sa.DateTime(), nullable=True),
        sa.Column("last_delivery_ok", sa.Boolean(), nullable=True),
        sa.Column("last_delivery_error", sa.Text(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_webhook_subscriptions_tenant_id", "webhook_subscriptions", ["tenant_id"])
    op.create_index("ix_webhook_subscriptions_is_active", "webhook_subscriptions", ["is_active"])


def downgrade() -> None:
    op.drop_index("ix_webhook_subscriptions_is_active", table_name="webhook_subscriptions")
    op.drop_index("ix_webhook_subscriptions_tenant_id", table_name="webhook_subscriptions")
    op.drop_table("webhook_subscriptions")
