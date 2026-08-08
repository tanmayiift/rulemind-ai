"""Create model_evaluations — persisted predictive-model evaluation runs.

Backs the model-evaluation endpoints (`/api/v1/evaluations`): a run stores the computed metric suite
(Gini/AUC/KS/PR-AUC/calibration/PSI/decile, multi-label, uplift), a dataset summary, per-segment and
temporal breakdowns, and a promotion-gate verdict — never the raw scored rows. Without this migration
the table exists only via create_all (dev); production (Alembic) needs it explicitly.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260808_0016"
down_revision = "20260806_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "model_evaluations" in inspector.get_table_names():
        return  # idempotent — created by create_all in some dev setups
    op.create_table(
        "model_evaluations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("public_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("model_id", sa.String(length=128), nullable=True),
        sa.Column("task", sa.String(length=24), nullable=False, server_default="binary"),
        sa.Column("dataset_summary", sa.JSON(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("segments", sa.JSON(), nullable=False),
        sa.Column("temporal", sa.JSON(), nullable=False),
        sa.Column("gate_status", sa.String(length=24), nullable=False, server_default="unknown"),
        sa.Column("gate_result", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="dev"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("tenant_id", "public_id", name="uq_model_evaluations_tenant_public"),
    )
    op.create_index("ix_model_evaluations_tenant_id", "model_evaluations", ["tenant_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "model_evaluations" not in inspector.get_table_names():
        return
    indexes = {idx["name"] for idx in inspector.get_indexes("model_evaluations")}
    if "ix_model_evaluations_tenant_id" in indexes:
        op.drop_index("ix_model_evaluations_tenant_id", table_name="model_evaluations")
    op.drop_table("model_evaluations")
