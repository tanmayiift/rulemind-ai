"""Add login_attempts — make the failed-credential lockout survive a restart.

The counter had two tiers: Redis when configured, and a per-PROCESS dict otherwise. The
fallback is what most deployments actually run on, and it is not a lockout.

Restart the pod and the dict is gone, so an attacker who trips the limit waits for the next
deploy, crash or scale event and resumes from zero. Run two replicas and each keeps its own
count, so N replicas mean N times the attempts before anyone is locked out — and a round-robin
load balancer hands the attacker that multiplier without their having to do anything. Neither
is visible from outside: the endpoint returns 429 exactly as designed, right up until it
silently doesn't.

The database is the store every replica already shares and that outlives a process, so it goes
between Redis and the dict: Redis stays the fast path, this is the correct fallback, and the
in-process dict is now only the last resort when both are unreachable.

Keyed by the same salted digest as the Redis bucket (`middleware._login_bucket`), so the tiers
agree on identity and neither stores an email address. No tenant_id — the platform-admin
account has no tenant, and the key already carries the scope.

Rows are deleted on read once `expires_at` has passed. These are only ever consulted on the
login path, so the reader is the one process guaranteed to notice, and the table stays bounded
without a sweep job that could silently stop running.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260904_0024"
down_revision = "20260904_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent, matching the house pattern: Storage.__init__ runs create_all, so on a fresh
    # database the table already exists by the time this runs and a bare create_table would
    # abort the whole chain.
    bind = op.get_bind()
    if sa.inspect(bind).has_table("login_attempts"):
        return
    op.create_table(
        "login_attempts",
        sa.Column("key", sa.String(length=128), primary_key=True),
        sa.Column("failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_login_attempts_expires_at", "login_attempts", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_login_attempts_expires_at", table_name="login_attempts")
    op.drop_table("login_attempts")
