"""Add on-device sync lineage to decisions: device_id, bundle_hash, and a server received_at.

On-device decisions carry a device-clock created_at (subject to drift) and are produced against a
specific bundle version. These columns record which device and bundle produced a decision, and a
trustworthy SERVER receipt time for ordering/retention/analytics independent of device clocks.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260806_0015"
down_revision = "20260801_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("decisions")}
    if "client_id" not in columns:
        op.add_column("decisions", sa.Column("client_id", sa.String(length=128), nullable=True))
    if "device_id" not in columns:
        op.add_column("decisions", sa.Column("device_id", sa.String(length=128), nullable=True))
    if "bundle_hash" not in columns:
        op.add_column("decisions", sa.Column("bundle_hash", sa.String(length=128), nullable=True))
    if "received_at" not in columns:
        # Backfill existing rows' received_at from created_at (best available), then keep server-set.
        op.add_column("decisions", sa.Column("received_at", sa.DateTime(), nullable=True))
        op.execute("UPDATE decisions SET received_at = created_at WHERE received_at IS NULL")

    indexes = {idx["name"] for idx in inspector.get_indexes("decisions")}
    if "ix_decisions_client_id" not in indexes:
        op.create_index("ix_decisions_client_id", "decisions", ["client_id"])
    if "ix_decisions_device_id" not in indexes:
        op.create_index("ix_decisions_device_id", "decisions", ["device_id"])
    if "ix_decisions_received_at" not in indexes:
        op.create_index("ix_decisions_received_at", "decisions", ["received_at"])
    # Per-tenant uniqueness of the device-supplied client id (NULLs — API decisions — are distinct).
    constraints = {c["name"] for c in inspector.get_unique_constraints("decisions")}
    if "uq_decisions_tenant_client" not in constraints:
        op.create_unique_constraint("uq_decisions_tenant_client", "decisions", ["tenant_id", "client_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    constraints = {c["name"] for c in inspector.get_unique_constraints("decisions")}
    if "uq_decisions_tenant_client" in constraints:
        op.drop_constraint("uq_decisions_tenant_client", "decisions", type_="unique")
    indexes = {idx["name"] for idx in inspector.get_indexes("decisions")}
    for name in ("ix_decisions_received_at", "ix_decisions_device_id", "ix_decisions_client_id"):
        if name in indexes:
            op.drop_index(name, table_name="decisions")
    columns = {col["name"] for col in inspector.get_columns("decisions")}
    for name in ("received_at", "bundle_hash", "device_id", "client_id"):
        if name in columns:
            op.drop_column("decisions", name)
