"""Add decision_outbox — the transactional outbox behind exactly-once delivery (parity P09).

A decision is written to Postgres and published to Kafka: two systems, so there is no single
commit that covers both. "Write then publish" loses the message if the process dies in between;
"publish then write" emits a decision that never existed. Neither is recoverable afterwards,
because nothing durable records that the gap happened.

This table closes it. The outbox row is inserted in the SAME transaction as the Decision, so the
two commit together or neither does. A relay then moves committed rows to the broker and stamps
sent_at — at-least-once by construction. Exactly-once is completed on the consumer side, by
deduplicating on the outbox id, not by assuming delivery is reliable.

Deliberately not an extension of the decision WAL: decision_wal.py exists for the case where the
DATABASE is unreachable and holds decisions already returned to a caller. Here the database is
the part that succeeded.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260914_0029"
down_revision = "20260905_0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent, matching the house pattern: Storage.__init__ runs create_all, so on a fresh
    # database the table already exists by the time this runs.
    bind = op.get_bind()
    if sa.inspect(bind).has_table("decision_outbox"):
        return
    op.create_table(
        "decision_outbox",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("decision_id", sa.String(length=36), nullable=False),
        sa.Column("partition_key", sa.String(length=128), nullable=False),
        sa.Column("topic", sa.String(length=255), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_decision_outbox_tenant_id", "decision_outbox", ["tenant_id"])
    op.create_index("ix_decision_outbox_decision_id", "decision_outbox", ["decision_id"])
    op.create_index("ix_decision_outbox_sent_at", "decision_outbox", ["sent_at"])
    # The relay's only hot query is "oldest unsent first". Without this composite index that is a
    # sequential scan over a table which grows with every decision ever made.
    op.create_index("ix_decision_outbox_unsent", "decision_outbox", ["sent_at", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_decision_outbox_unsent", table_name="decision_outbox")
    op.drop_index("ix_decision_outbox_sent_at", table_name="decision_outbox")
    op.drop_index("ix_decision_outbox_decision_id", table_name="decision_outbox")
    op.drop_index("ix_decision_outbox_tenant_id", table_name="decision_outbox")
    op.drop_table("decision_outbox")
