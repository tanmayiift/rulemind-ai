"""Postgres row-level security keeps each workspace's rows to itself (plan 5.5).

Every assertion goes around the application's own tenant predicates -- raw `select(Decision)`
with no WHERE, an UPDATE with no WHERE, an INSERT naming another workspace -- because a guard is
only shown to work by a query that would have leaked without it.

Postgres only: SQLite has no row security. In the SQLite lane these skip with that reason; the
Postgres lane (DATABASE_URL + RULEMIND_TEST_PG_SCHEMA_PER_PATH=1) runs them.
"""
from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select, text, update  # noqa: E402
from sqlalchemy.exc import DBAPIError  # noqa: E402

import app.main as app_main  # noqa: E402
from app import rls  # noqa: E402
from app.context import reset_current_tenant_id, set_current_tenant_id  # noqa: E402
from app.models import Base, Decision  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import preserve_storage_global  # noqa: E402


def _postgres() -> bool:
    return (os.environ.get("DATABASE_URL", "").startswith("postgresql")
            and os.environ.get("RULEMIND_TEST_PG_SCHEMA_PER_PATH") == "1")


@contextlib.contextmanager
def acting_for(tenant_id):
    token = set_current_tenant_id(tenant_id)
    try:
        yield
    finally:
        reset_current_tenant_id(token)


@unittest.skipUnless(_postgres(), "row-level security exists only on Postgres; the Postgres lane runs this")
class RowLevelSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tempdir.cleanup)
        preserve_storage_global(self)
        self.storage = Storage(path=os.path.join(self.tempdir.name, "rls.db"))
        app_main.storage = self.storage
        self.a = self.storage.default_tenant_id
        self.b = self.storage.create_tenant("Other workspace")["id"]
        self.storage.add_decision({"id": "d-a", "policy_id": "p", "outcome": "approve"}, tenant_id=self.a)
        self.storage.add_decision({"id": "d-b", "policy_id": "p", "outcome": "reject"}, tenant_id=self.b)

    def _rows(self):
        with self.storage.connect() as session:
            return {(row.id, row.tenant_id, row.outcome) for row in session.scalars(select(Decision))}

    def test_a_query_without_a_tenant_predicate_sees_only_its_workspace(self):
        with acting_for(self.a):
            seen = self._rows()
        self.assertEqual({row[0] for row in seen}, {"d-a"}, "a predicate-free SELECT crossed workspaces")

    def test_system_scope_sees_every_workspace(self):
        self.assertTrue({"d-a", "d-b"} <= {row[0] for row in self._rows()})

    def test_an_update_without_a_predicate_cannot_reach_another_workspace(self):
        with acting_for(self.a), self.storage.connect() as session:
            session.execute(update(Decision).values(outcome="overwritten"))
        outcomes = {row[0]: row[2] for row in self._rows()}
        self.assertEqual(outcomes["d-a"], "overwritten")
        self.assertEqual(outcomes["d-b"], "reject", "an UPDATE for one workspace rewrote another's row")

    def test_a_row_for_another_workspace_cannot_be_written(self):
        with self.assertRaises(DBAPIError) as caught, acting_for(self.a), self.storage.connect() as session:
            session.add(Decision(id="d-forged", tenant_id=self.b, policy_id="p", outcome="approve"))
            session.flush()
        self.assertIn("row-level security", str(caught.exception))

    def test_the_transaction_runs_as_a_role_the_policies_bind(self):
        """Policies never apply to a superuser or BYPASSRLS role, FORCE or not -- and the compose
        database user is a superuser. Without the role switch every assertion above would
        pass only on a database whose app user happened to be unprivileged."""
        with acting_for(self.a), self.storage.connect() as session:
            bypasses = session.execute(text(
                "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")).scalar()
        self.assertFalse(bypasses, "the tenant transaction ran as a role that ignores row security")

    def test_the_scope_ends_with_the_transaction(self):
        """Transaction-local, so a pooled connection handed to the next caller is clean."""
        with self.storage.engine.connect() as conn:
            with conn.begin():
                rls.scope_transaction(conn, self.a, self.storage._rls_switch_role)
                self.assertEqual(conn.execute(text(f"SELECT current_setting('{rls.SETTING}', true)")).scalar(), self.a)
            with conn.begin():
                self.assertIn(conn.execute(text(f"SELECT current_setting('{rls.SETTING}', true)")).scalar(), ("", None))
                self.assertEqual(conn.execute(text("SELECT current_user")).scalar(),
                                 conn.execute(text("SELECT session_user")).scalar())

    def test_every_tenant_table_is_covered_and_install_is_idempotent(self):
        tables = rls.tenant_tables(Base.metadata)
        with self.storage.engine.begin() as conn:
            self.assertEqual(rls.install(conn, tables), [], "an up-to-date schema was altered again")
            with_column = {row[0] for row in conn.execute(text(
                "SELECT table_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND column_name = 'tenant_id'"))}
            covered = {row[0] for row in conn.execute(text(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "JOIN pg_policies p ON p.schemaname = n.nspname AND p.tablename = c.relname "
                "WHERE n.nspname = current_schema() AND c.relrowsecurity AND c.relforcerowsecurity "
                "AND p.policyname = :policy"), {"policy": rls.POLICY})}
        self.assertEqual(with_column - covered, set(), "tenant tables without row security")

    def test_the_install_reports_that_it_is_enforced(self):
        self.assertTrue(self.storage.row_security["row_level_security"], self.storage.row_security["detail"])

    def test_a_table_without_its_policy_is_reported_by_name(self):
        """Enforcement is read from the catalogue, not assumed from having run the installer."""
        with self.storage.engine.begin() as conn:
            conn.execute(text(f"DROP POLICY {rls.POLICY} ON decisions"))
            status = rls.enforcement(conn, rls.tenant_tables(Base.metadata))
            rls.install(conn, rls.tenant_tables(Base.metadata))
        self.assertFalse(status["row_level_security"])
        self.assertIn("decisions", status["detail"])

    def test_a_route_that_forgets_its_predicate_still_cannot_leak(self):
        """The failure this exists for, through the API: a storage read that ignores the tenant
        it is given. The application layer would hand back workspace B's decision; the database
        does not."""
        key_a = self.storage.generate_api_key_for_tenant(self.a, role="admin")["plaintext"]
        client = TestClient(app_main.app)
        self.addCleanup(client.close)

        def careless_get_decision(decision_id, tenant_id=None):
            with self.storage.connect() as session:
                row = session.get(Decision, decision_id)
                return {"id": row.id, "outcome": row.outcome} if row else None

        with mock.patch.object(self.storage, "get_decision", careless_get_decision):
            self.assertEqual(client.get("/api/v1/audit/decisions/d-a", headers={"x-api-key": key_a}).status_code, 200)
            self.assertEqual(client.get("/api/v1/audit/decisions/d-b", headers={"x-api-key": key_a}).status_code, 404,
                             "a route that dropped its tenant predicate returned another workspace's decision")


if __name__ == "__main__":
    unittest.main()
