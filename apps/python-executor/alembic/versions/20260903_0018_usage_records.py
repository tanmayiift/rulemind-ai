"""Create usage_records — metered usage per tenant, per UTC day, per metric (Track B4).

The billable record. Before this there was no way to invoice a workspace: decision volume was
not counted at all, and the only usage figures that existed (AI tokens) were lifetime totals
in Tenant.config with no time dimension, so "what did this customer use in August" had no
answer.

Daily grain, not monthly: a month is a SUM over days, whereas a monthly counter cannot show a
trend, cannot prorate a mid-month plan change, and cannot be re-cut if a period boundary was
wrong. No backfill -- historical decision rows could be counted, but a bill assembled from
retroactively invented usage is not one a customer should be asked to pay, and Decision rows
are subject to retention purging so the count would be wrong for exactly the oldest periods.
Metering starts from the deploy.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260903_0018"
down_revision = "20260903_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "usage_records" in inspector.get_table_names():
        return
    op.create_table(
        "usage_records",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("metric", sa.String(length=64), nullable=False),
        # BigInteger: a high-volume tenant can exceed 2^31 decisions in a lifetime, and an
        # overflow in a billing counter is not a failure mode worth risking to save 4 bytes.
        sa.Column("count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        # The upsert target. Without it a concurrent increment inserts a second row for the same
        # bucket and the tenant is quietly UNDER-billed.
        sa.UniqueConstraint("tenant_id", "day", "metric", name="uq_usage_tenant_day_metric"),
    )
    op.create_index("ix_usage_records_tenant_id", "usage_records", ["tenant_id"])
    op.create_index("ix_usage_records_day", "usage_records", ["day"])
    op.create_index("ix_usage_records_metric", "usage_records", ["metric"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "usage_records" not in inspector.get_table_names():
        return
    for name in ("ix_usage_records_metric", "ix_usage_records_day", "ix_usage_records_tenant_id"):
        op.drop_index(name, table_name="usage_records")
    op.drop_table("usage_records")
