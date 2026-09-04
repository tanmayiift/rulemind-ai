"""Add settings.audit_retention_months — express retention in calendar months, not days.

Retention was days-only (`audit_retention_days`, default 90). Days is a lossy encoding of what
customers actually buy and regulators actually write down: "retain decisions for 7 years",
"purge PII after 24 months". Seven years is 2557 days measured against the real calendar, not
the 2555 that `7 * 365` gives — so an operator converting the contract by hand purges two days
of decisions early, and nobody notices until an audit asks for one of them.

NULL is the meaningful default, not a placeholder. It means "this workspace never set months",
and `audit_retention_days` stays authoritative for it — which is how every pre-existing tenant
keeps exactly the window it already had. No backfill: fabricating a month count for a tenant
whose 90 days is not a whole number of months would silently change that tenant's window, and
this migration must not move anybody's purge date. Months only ever take effect when somebody
sets them.

`app/archiver.py:retention_cutoff` is the single place that resolves months-over-days; the
purge job in `app/scheduler.py` and the settings write path in `app/storage.py` both go
through it rather than re-deriving the precedence.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260904_0022"
down_revision = "20260903_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent, matching the house pattern: Storage.__init__ runs create_all (and migration
    # 0001 is create_all), so on a fresh database the column already exists by the time this
    # runs and a bare add_column would abort the whole chain.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("settings")}
    if "audit_retention_months" not in columns:
        # Nullable with no server_default: NULL is load-bearing here (see the docstring), so
        # there is nothing to backfill and existing rows are correct the moment the column lands.
        op.add_column("settings", sa.Column("audit_retention_months", sa.Integer(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("settings")}
    if "audit_retention_months" in columns:
        op.drop_column("settings", "audit_retention_months")
