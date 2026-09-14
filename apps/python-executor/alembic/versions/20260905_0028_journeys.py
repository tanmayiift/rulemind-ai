"""Add journeys — the screen flow that collects a payload and runs a policy (parity P15).

The parity row called this the largest item in the matrix and said a journey "exists only as
fixtures in three places that do not share a definition, so there is not even one shape to build
a table from". That was wrong on the decisive point: `app/experience_studio.py` defines the shape,
`/sdk/v1/experience-manifest` serves it, and `packages/sdk-flutter/example/lib/state/app_state.dart`
reads `manifest['journeys']` and runs them. One of the row's four e2e clauses -- "run it from an
SDK" -- already worked.

What did not exist is a per-tenant record. `build_experience_manifest` returned a deep copy of one
static template, so every workspace saw the same three demo journeys and none could author its
own. This table is that template's shape, per tenant.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260905_0028"
down_revision = "20260905_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent, matching the house pattern: Storage.__init__ runs create_all, so on a fresh
    # database the table already exists by the time this runs.
    bind = op.get_bind()
    if sa.inspect(bind).has_table("journeys"):
        return
    op.create_table(
        "journeys",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("public_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("category", sa.String(length=128), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("policy_id", sa.String(length=128), nullable=True),
        sa.Column("primary_color", sa.String(length=32), nullable=True),
        sa.Column("screens", sa.JSON(), nullable=False),
        sa.Column("is_locked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="dev"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("tenant_id", "public_id", name="uq_journeys_tenant_public"),
    )
    op.create_index("ix_journeys_tenant_id", "journeys", ["tenant_id"])
    op.create_index("ix_journeys_policy_id", "journeys", ["policy_id"])
    op.create_index("ix_journeys_is_active", "journeys", ["is_active"])


def downgrade() -> None:
    op.drop_index("ix_journeys_is_active", table_name="journeys")
    op.drop_index("ix_journeys_policy_id", table_name="journeys")
    op.drop_index("ix_journeys_tenant_id", table_name="journeys")
    op.drop_table("journeys")
