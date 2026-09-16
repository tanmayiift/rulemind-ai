"""Record WHAT was promoted and BY WHOM on an experiment.

`promote_experiment` (routers/experiments.py) has always written `promoted_variant_id`,
`promoted_by` and `promoted_at` into the payload it hands to `create_or_update_experiment`. The
Experiment model had no such columns, and that function copies only the fields it knows -- so the
three were silently dropped. The endpoint returned 200, the status moved to "completed", and a
subsequent GET reported `promoted_variant_id: None`.

The audit event did carry it, so nothing was unrecoverable. But an audit log answers "what
happened" and the entity answers "what is true now"; asking a caller to replay audit rows to
learn which variant is live is not a reasonable substitute, and no UI did it.

Found by the evaluations sweep on 2026-09-16, by promoting through the API and reading the
experiment back.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260916_0030"
down_revision = "20260914_0029"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("promoted_variant_id", sa.String(length=128)),
    ("promoted_by", sa.String(length=255)),
    ("promoted_at", sa.DateTime()),
)


def upgrade() -> None:
    # Idempotent per COLUMN, not just per table, matching the house pattern: Storage.__init__
    # runs create_all, so on a fresh database the table already has these by the time this runs.
    # Checking the table alone would then skip a partially-migrated one.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("experiments"):
        return
    existing = {column["name"] for column in inspector.get_columns("experiments")}
    for name, type_ in _COLUMNS:
        if name not in existing:
            op.add_column("experiments", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("experiments"):
        return
    existing = {column["name"] for column in inspector.get_columns("experiments")}
    for name, _ in _COLUMNS:
        if name in existing:
            op.drop_column("experiments", name)
