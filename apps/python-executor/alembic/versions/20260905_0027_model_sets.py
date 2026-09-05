"""Add model_sets — a versioned group of models with a routing rule (parity P05).

Models were flat blobs addressed one at a time: a `model` step names one public_id and that is
the whole story. There was no way to route between candidates by rule, and no way to compare two
on the same input without wiring two policies.

Members are a JSON column rather than a child table because they are an ORDERED list whose order
IS the routing semantics (first match wins). A child table would need an explicit position column
and an ordering join to reproduce exactly that, for a list bounded at 20.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260905_0027"
down_revision = "20260905_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent, matching the house pattern: Storage.__init__ runs create_all, so on a fresh
    # database the table already exists by the time this runs.
    bind = op.get_bind()
    if sa.inspect(bind).has_table("model_sets"):
        return
    op.create_table(
        "model_sets",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("public_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("members", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="dev"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("tenant_id", "public_id", name="uq_model_sets_tenant_public"),
    )
    op.create_index("ix_model_sets_tenant_id", "model_sets", ["tenant_id"])
    op.create_index("ix_model_sets_is_active", "model_sets", ["is_active"])


def downgrade() -> None:
    op.drop_index("ix_model_sets_is_active", table_name="model_sets")
    op.drop_index("ix_model_sets_tenant_id", table_name="model_sets")
    op.drop_table("model_sets")
