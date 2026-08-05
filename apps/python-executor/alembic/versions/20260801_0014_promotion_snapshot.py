"""Add promotions.snapshot_json so promotions capture the policy definition for diffing."""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260801_0014"
down_revision = "20260801_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("promotions")}
    if "snapshot_json" not in columns:
        op.add_column("promotions", sa.Column("snapshot_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("promotions")}
    if "snapshot_json" in columns:
        op.drop_column("promotions", "snapshot_json")
