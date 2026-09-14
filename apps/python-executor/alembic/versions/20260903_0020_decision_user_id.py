"""Add decisions.user_id — separate the decision SUBJECT from the device idempotency key.

The /decide path wrote the request's `userId` into `client_id`, because case-wise history
("show me this customer's decisions") filtered on that column and nothing else carried the
subject. But `client_id` is under UNIQUE(tenant_id, client_id) — the on-device dedupe key, so
that an at-least-once SDK retry never double-counts.

A userId repeats on every re-decision of the same customer. So the SECOND decision for any
customer violated the constraint and was silently discarded: the decision-log write is async,
so the caller had already received 200 and a decision_id that would never exist. Downstream:
no audit row (the compliance artifact this product exists to produce), no usage recorded
(under-billing every repeat decision), reason-codes 404, and the WAL replaying the doomed row
forever.

Backfill copies client_id into user_id ONLY for server-originated rows (source <> 'on_device'),
which is exactly the set that was mis-populated. Device rows keep client_id as the idempotency
key it always was and get a NULL user_id, which is accurate — the SDK does not send a subject.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260903_0020"
down_revision = "20260903_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("decisions")}
    if "user_id" not in columns:
        op.add_column("decisions", sa.Column("user_id", sa.String(length=255), nullable=True))
        # Recover the subject for rows the old code wrote it into. Scoped to non-device rows so
        # a genuine device idempotency key is never mistaken for a customer id.
        op.execute(
            "UPDATE decisions SET user_id = client_id "
            "WHERE user_id IS NULL AND client_id IS NOT NULL "
            "AND (source IS NULL OR source <> 'on_device')"
        )
    indexes = {idx["name"] for idx in inspector.get_indexes("decisions")}
    if "ix_decisions_user_id" not in indexes:
        op.create_index("ix_decisions_user_id", "decisions", ["user_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {idx["name"] for idx in inspector.get_indexes("decisions")}
    if "ix_decisions_user_id" in indexes:
        op.drop_index("ix_decisions_user_id", table_name="decisions")
    columns = {col["name"] for col in inspector.get_columns("decisions")}
    if "user_id" in columns:
        op.drop_column("decisions", "user_id")
