"""Add decisions.experiment_id so A/B analytics can scope decisions to one experiment.

Variant ids (champion/challenger) repeat across experiments, so aggregating by variant id
alone cross-counts. This column records which experiment assigned each decision.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260801_0013"
down_revision = "20260731_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("decisions")}
    if "experiment_id" not in columns:
        op.add_column("decisions", sa.Column("experiment_id", sa.String(length=128), nullable=True))
    indexes = {idx["name"] for idx in inspector.get_indexes("decisions")}
    if "ix_decisions_experiment_id" not in indexes:
        op.create_index("ix_decisions_experiment_id", "decisions", ["experiment_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {idx["name"] for idx in inspector.get_indexes("decisions")}
    if "ix_decisions_experiment_id" in indexes:
        op.drop_index("ix_decisions_experiment_id", table_name="decisions")
    columns = {col["name"] for col in inspector.get_columns("decisions")}
    if "experiment_id" in columns:
        op.drop_column("decisions", "experiment_id")
