"""Add predictors + predictor_values — a monitored number per decision (parity P20).

"Predictor" was only a synonym before this: app/ai.py prompts the model to turn a description
into a SCORECARD, so there was no entity, no stored values and no report. Nothing could answer
"has this score's population moved since we approved the policy?".

TWO TABLES, NOT ONE, because they have opposite lifecycles and opposite access patterns.
`predictors` is a handful of author-managed rows read on every decision; `predictor_values` grows
with decision volume and is read in bulk over a time window. Putting the observations in a JSON
column on the predictor would make every decision rewrite an unbounded blob.

`predictor_values` is deliberately NOT encrypted, unlike the decision columns. A bare score with
no identifier beside it is not personal data, and the whole point of the table is to be scanned
in bulk for a distribution -- decrypting per row would turn a 30-day baseline into a per-request
cost rather than a query. The decision_id is a pointer for drill-down; the decision it names is
still encrypted where it lives.

The composite index is (tenant_id, predictor_id, created_at) in that order, matching the only
query the table serves. Without it a baseline window becomes a full scan as decision volume
grows, which is exactly when someone starts looking at drift.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260905_0026"
down_revision = "20260905_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent, matching the house pattern: Storage.__init__ runs create_all, so on a fresh
    # database the tables already exist by the time this runs and a bare create_table would abort
    # the whole chain.
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("predictors"):
        op.create_table(
            "predictors",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("tenant_id", sa.String(length=36), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("source_type", sa.String(length=32), nullable=False),
            sa.Column("source_id", sa.String(length=128), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("baseline_days", sa.Integer(), nullable=False, server_default="30"),
            sa.Column("current_days", sa.Integer(), nullable=False, server_default="7"),
            sa.Column("bins", sa.Integer(), nullable=False, server_default="10"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("tenant_id", "name", name="uq_predictors_tenant_name"),
        )
        op.create_index("ix_predictors_tenant_id", "predictors", ["tenant_id"])
        op.create_index("ix_predictors_is_active", "predictors", ["is_active"])

    if not inspector.has_table("predictor_values"):
        op.create_table(
            "predictor_values",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("tenant_id", sa.String(length=36), nullable=False),
            sa.Column("predictor_id", sa.String(length=36), nullable=False),
            sa.Column("decision_id", sa.String(length=128), nullable=True),
            sa.Column("value", sa.Float(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_predictor_values_lookup", "predictor_values",
                        ["tenant_id", "predictor_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_predictor_values_lookup", table_name="predictor_values")
    op.drop_table("predictor_values")
    op.drop_index("ix_predictors_is_active", table_name="predictors")
    op.drop_index("ix_predictors_tenant_id", table_name="predictors")
    op.drop_table("predictors")
