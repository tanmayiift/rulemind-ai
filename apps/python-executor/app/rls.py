"""Postgres row-level security: the database enforces tenant isolation (plan 5.5).

THE PROBLEM. Isolation between workspaces rested on ~613 hand-written `tenant_id == ...`
predicates in storage.py. One forgotten predicate, or one explicit tenant id that is not the
caller's, and a request for workspace A reads -- or writes -- workspace B's rows. Review cannot
hold that line at that count; the codebase already records routes that fell through it
(insights.py, sdk.py). R10.19 closed the storage fallback that resolved a lost tenant to the
first one in the table; that is an application-level guard and cannot see a query that simply
omits the predicate.

WHAT THIS DOES. Every table with a `tenant_id` column gets `ENABLE` + `FORCE ROW LEVEL SECURITY`
and one policy:

    USING / WITH CHECK:  rulemind.tenant_id is unset  OR  tenant_id = rulemind.tenant_id

A transaction that runs on behalf of a workspace -- any request the middleware authenticated, and
anything else that sets the tenant context -- sets `rulemind.tenant_id` for the transaction
(`scope_transaction`, called from a Session `after_begin` hook). From then on Postgres returns
only that workspace's rows and refuses to write anyone else's, whatever the SQL says.

A transaction with no tenant context is SYSTEM scope and sees every row: startup and seeding,
migrations, the scheduler, the outbox relay and WAL replay, the platform-admin console, and the
pre-authentication lookups (an API key, a signup, an OAuth token, an SSO callback) that exist
precisely to find out which workspace a caller belongs to. That is the boundary this enforces:
a request acting for one workspace cannot touch another's rows. It does not make a lost tenant
context fail closed -- R10.19 does that, one layer up.

THE ROLE, WHICH IS WHAT MAKES ANY OF THIS REAL. Postgres never applies row security to a
superuser or a BYPASSRLS role, FORCE or not -- and the compose file's `POSTGRES_USER`, like most
first installs, is a superuser. Policies alone would have been decoration there. So a tenant
transaction also switches to `rulemind_tenant` (NOLOGIN, NOSUPERUSER, NOBYPASSRLS) for its
duration, when the connecting role would bypass the policies. `tests/test_rls.py` checks
`current_user` inside a tenant transaction rather than trusting this paragraph.

SQLite has no row security; everything here is a no-op there, and the SQLite lane keeps relying
on the predicates alone.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

LOGGER = logging.getLogger("rulemind.rls")

SETTING = "rulemind.tenant_id"
ROLE = "rulemind_tenant"
# Versioned: a changed predicate gets a new name, so `install` can tell an up-to-date table from
# one carrying an older policy without comparing Postgres's normalised expression text.
POLICY = "rulemind_tenant_isolation_v1"
_PREDICATE = ("coalesce(current_setting('{setting}', true), '') = '' "
              "OR tenant_id = current_setting('{setting}', true)").format(setting=SETTING)


def tenant_tables(metadata: Any) -> List[str]:
    return sorted(table.name for table in metadata.sorted_tables if "tenant_id" in table.columns)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def install(conn: Any, tables: Iterable[str]) -> List[str]:
    """Idempotent. Returns the statements it had to run (none on an up-to-date schema).

    Runs on every boot (from Storage._create_schema) and in migration 0036, so it must cost
    nothing when there is nothing to do: `ALTER TABLE` takes an ACCESS EXCLUSIVE lock, and a
    rolling restart re-issuing it on `decisions` would block live traffic. Everything is read
    from the catalogue first and only what is missing is changed."""
    if conn.dialect.name != "postgresql":
        return []
    ran: List[str] = []

    def run(sql: str) -> None:
        conn.exec_driver_sql(sql)
        ran.append(sql)

    schema = conn.exec_driver_sql("SELECT current_schema()").scalar()
    if not conn.exec_driver_sql("SELECT 1 FROM pg_roles WHERE rolname = %s", (ROLE,)).scalar():
        try:
            # Its own savepoint: without CREATEROLE this fails, and the failure must not abort
            # the surrounding transaction.
            with conn.begin_nested():
                run(f"CREATE ROLE {ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS")
        except Exception as error:  # noqa: BLE001 - reported, and the policies still install
            LOGGER.warning("could not create role %s (%s); tenant transactions will run as the "
                           "connecting role, which enforces row security only if it is neither a "
                           "superuser nor BYPASSRLS", ROLE, str(error).splitlines()[0][:160])
    role_exists = bool(conn.exec_driver_sql("SELECT 1 FROM pg_roles WHERE rolname = %s", (ROLE,)).scalar())

    wanted = list(tables)
    state = {row[0]: row[1:] for row in conn.exec_driver_sql(
        "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, "
        "       EXISTS (SELECT 1 FROM pg_policies p WHERE p.schemaname = n.nspname "
        "               AND p.tablename = c.relname AND p.policyname = %s) "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = current_schema() AND c.relkind = 'r'", (POLICY,)).fetchall()}
    stale = {row[0]: row[1] for row in conn.exec_driver_sql(
        "SELECT tablename, policyname FROM pg_policies WHERE schemaname = current_schema() "
        "AND policyname LIKE 'rulemind_tenant_isolation%%' AND policyname <> %s", (POLICY,)).fetchall()}

    for table in wanted:
        if table not in state:
            continue
        enabled, forced, has_policy = state[table]
        name = _quote(table)
        if not enabled:
            run(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
        if not forced:
            # Without FORCE the table's OWNER is exempt, and the app usually connects as the owner.
            run(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY")
        if table in stale:
            run(f"DROP POLICY {_quote(stale[table])} ON {name}")
        if not has_policy:
            run(f"CREATE POLICY {POLICY} ON {name} USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})")

    if role_exists:
        # The tenant role reads every table the app does -- the policies decide which ROWS.
        missing = [row[0] for row in conn.exec_driver_sql(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p') "
            "AND NOT has_table_privilege(%s, c.oid, 'SELECT, INSERT, UPDATE, DELETE')", (ROLE,)).fetchall()]
        if missing or not conn.exec_driver_sql("SELECT has_schema_privilege(%s, current_schema(), 'USAGE')",
                                               (ROLE,)).scalar():
            run(f"GRANT USAGE ON SCHEMA {_quote(schema)} TO {ROLE}")
            run(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {_quote(schema)} TO {ROLE}")
            run(f"GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA {_quote(schema)} TO {ROLE}")
            # Tables a later migration creates are covered before the next boot re-checks.
            run(f"ALTER DEFAULT PRIVILEGES IN SCHEMA {_quote(schema)} "
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {ROLE}")
            run(f"ALTER DEFAULT PRIVILEGES IN SCHEMA {_quote(schema)} "
                f"GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {ROLE}")
        if not conn.exec_driver_sql("SELECT pg_has_role(current_user, %s, 'MEMBER')", (ROLE,)).scalar():
            # A non-superuser needs membership to SET ROLE to it; a superuser does not, and
            # granting it anyway is harmless.
            run(f"GRANT {ROLE} TO {_quote(conn.exec_driver_sql('SELECT current_user').scalar())}")

    if ran:
        LOGGER.info("row-level security: %d statement(s) applied in schema %s", len(ran), schema)
    return ran


def enforcement(conn: Any, tables: Iterable[str]) -> Dict[str, Any]:
    """What this database actually enforces -- for the startup log and the residency report, so
    an install that cannot enforce it says so instead of looking protected."""
    if conn.dialect.name != "postgresql":
        return {"row_level_security": False,
                "detail": "SQLite has no row-level security; workspaces are kept apart by the "
                          "application's own tenant checks."}
    existing = {row[0]: bool(row[1] and row[2]) for row in conn.exec_driver_sql(
        "SELECT c.relname, c.relrowsecurity AND c.relforcerowsecurity, "
        "       EXISTS (SELECT 1 FROM pg_policies p WHERE p.schemaname = n.nspname "
        "               AND p.tablename = c.relname AND p.policyname = %s) "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = current_schema() AND c.relkind = 'r'", (POLICY,)).fetchall()}
    wanted = [table for table in tables if table in existing]
    missing = [table for table in wanted if not existing[table]]
    if missing:
        return {"row_level_security": False,
                "detail": "{0} of {1} workspace tables have no row-level security policy ({2}). Run "
                          "the migrations as the tables' owner.".format(
                              len(missing), len(wanted), ", ".join(missing[:5]) + (", ..." if len(missing) > 5 else ""))}
    bypasses, role_exists = conn.exec_driver_sql(
        "SELECT r.rolsuper OR r.rolbypassrls, EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s) "
        "FROM pg_roles r WHERE r.rolname = session_user", (ROLE,)).fetchone()
    if bypasses and not role_exists:
        return {"row_level_security": False,
                "detail": "The database user is a superuser or BYPASSRLS, which row-level security "
                          "never applies to, and the {0} role does not exist to switch to. Run the "
                          "migrations as a user with CREATEROLE.".format(ROLE)}
    return {"row_level_security": True,
            "detail": "Enforced by PostgreSQL row-level security on {0} workspace tables; workspace "
                      "transactions run as {1}.".format(len(wanted), ROLE if bypasses else "the connecting role")}


def needs_role_switch(conn: Any) -> bool:
    """Whether the connecting role would bypass the policies: a superuser, or BYPASSRLS."""
    if conn.dialect.name != "postgresql":
        return False
    row = conn.exec_driver_sql(
        "SELECT r.rolsuper OR r.rolbypassrls, EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s) "
        "FROM pg_roles r WHERE r.rolname = session_user", (ROLE,)).fetchone()
    return bool(row and row[0] and row[1])


def scope_transaction(connection: Any, tenant_id: Optional[str], switch_role: bool) -> None:
    """Bind one transaction to one workspace. Transaction-local, so the pooled connection comes
    back clean at COMMIT or ROLLBACK and the next transaction starts in system scope."""
    if not tenant_id:
        return
    if switch_role:
        connection.exec_driver_sql("SELECT set_config(%s, %s, true), set_config('role', %s, true)",
                                   (SETTING, str(tenant_id), ROLE))
    else:
        connection.exec_driver_sql("SELECT set_config(%s, %s, true)", (SETTING, str(tenant_id)))
