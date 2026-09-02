"""Add the tamper-evident hash chain to audit_events: seq, prev_hash, hash.

THE GENESIS BOUNDARY IS THE POINT OF THIS MIGRATION. Every column is added NULLABLE and
existing rows are left with NULLs. They are then reported by /api/v1/audit/verify as
`pre_chain` -- counted, and explicitly NOT counted as verified.

The tempting alternative is to backfill: walk the existing rows, compute hashes, link them up.
Do not. Those rows were written with no integrity guarantee, so a backfill would compute a
valid-looking chain over history that may ALREADY have been tampered with, and the verify
endpoint would then vouch for it. A compliance feature that certifies unverifiable data is
worse than no feature -- it converts "we don't know" into a false "we checked".

The first event appended after this migration anchors to GENESIS_HASH and starts seq at 1.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260903_0017"
down_revision = "20260808_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("audit_events")}
    if "seq" not in columns:
        op.add_column("audit_events", sa.Column("seq", sa.Integer(), nullable=True))
    if "prev_hash" not in columns:
        op.add_column("audit_events", sa.Column("prev_hash", sa.String(length=64), nullable=True))
    if "hash" not in columns:
        op.add_column("audit_events", sa.Column("hash", sa.String(length=64), nullable=True))
    # Deliberately NO backfill -- see the module docstring.

    constraints = {c["name"] for c in inspector.get_unique_constraints("audit_events")}
    if "uq_audit_events_tenant_seq" not in constraints:
        # Turns a concurrent-append race into a retryable IntegrityError rather than a forked
        # chain. Legacy rows hold NULL seq and are distinct under SQL NULL semantics.
        op.create_unique_constraint("uq_audit_events_tenant_seq", "audit_events", ["tenant_id", "seq"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    constraints = {c["name"] for c in inspector.get_unique_constraints("audit_events")}
    if "uq_audit_events_tenant_seq" in constraints:
        op.drop_constraint("uq_audit_events_tenant_seq", "audit_events", type_="unique")
    columns = {col["name"] for col in inspector.get_columns("audit_events")}
    for name in ("hash", "prev_hash", "seq"):
        if name in columns:
            op.drop_column("audit_events", name)
