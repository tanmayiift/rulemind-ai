"""Row-level security on every tenant table (plan 5.5).

Tenant isolation rested on ~613 hand-written predicates. This makes Postgres enforce it: every
table with a `tenant_id` gets ENABLE + FORCE ROW LEVEL SECURITY and a policy that, inside a
transaction bound to a workspace, admits only that workspace's rows -- for reads and writes.
Also creates `rulemind_tenant`, the NOSUPERUSER NOBYPASSRLS role such transactions switch to,
because Postgres never applies policies to a superuser and the usual first-install database user
is one. The logic lives in app/rls.py, shared with Storage's fresh-database path; see its
docstring for what is and is not in scope.

Idempotent, like `rls.install`. Postgres only: SQLite has no row security.
"""
from __future__ import annotations

from alembic import op

revision = "20260924_0036"
down_revision = "20260924_0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    from app import rls
    from app.models import Base

    rls.install(bind, rls.tenant_tables(Base.metadata))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    from app import rls
    from app.models import Base

    for table in rls.tenant_tables(Base.metadata):
        exists = bind.exec_driver_sql("SELECT to_regclass(%s)", (table,)).scalar()
        if not exists:
            continue
        bind.exec_driver_sql(f'DROP POLICY IF EXISTS {rls.POLICY} ON "{table}"')
        bind.exec_driver_sql(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')
        bind.exec_driver_sql(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
    # The role is cluster-wide and may serve other databases; it is left in place.
