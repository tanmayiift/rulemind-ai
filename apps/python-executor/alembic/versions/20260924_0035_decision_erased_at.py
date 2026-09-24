"""Mark decisions whose subject's personal data has been erased.

Data-subject erasure (plan 6.7) redacts a decision in place rather than deleting it: the outcome,
policy version, time and adverse-action reason codes stay, because a lender has to keep them. This
column says when the rest went, so a reader of the row knows the empty payload is an erasure and not
a decision made on nothing.

Idempotent per column, the house pattern.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260924_0035"
down_revision = "20260924_0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("decisions"):
        return
    if "erased_at" in {c["name"] for c in inspector.get_columns("decisions")}:
        return
    op.add_column("decisions", sa.Column("erased_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if "erased_at" in {c["name"] for c in sa.inspect(bind).get_columns("decisions")}:
        op.drop_column("decisions", "erased_at")
