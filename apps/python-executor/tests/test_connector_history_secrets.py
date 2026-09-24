"""Connector credentials must not sit in plaintext in entity_histories.

Found while building the connector templates (plan 6.1): `create_connector` snapshotted the whole
payload -- config included -- as plaintext JSON, one column away from the encrypted config. No
route serves connector history, so nothing leaked over the API; the exposure was the database
itself and every backup of it. Installing a template would have added provider credentials to it.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import sqlalchemy as sa

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from app.models import EntityHistory  # noqa: E402
from app.storage import Storage  # noqa: E402

SECRET = "PLAINTEXT-SECRET-123"
MIGRATION = APP_ROOT / "alembic" / "versions" / "20260924_0033_scrub_connector_history_secrets.py"


def _connector():
    return {"id": "bureau_x", "name": "Bureau", "schema_paths": [], "sample_payload": {},
            "config": {"auth_type": "api_key", "api_key": SECRET, "client_secret": SECRET,
                       "base_url": "https://bureau.example.com", "timeout_ms": 3000}}


class ConnectorHistorySecretsTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tempdir.cleanup)
        self.storage = Storage(path=os.path.join(self.tempdir.name, "history.db"))

    def _history_blob(self):
        return json.dumps([row["snapshot"] for row in self.storage.get_history("connector", "bureau_x")])

    def test_a_new_connector_history_row_holds_no_secret(self):
        self.storage.create_connector(_connector())
        blob = self._history_blob()
        self.assertNotIn(SECRET, blob)
        self.assertIn("https://bureau.example.com", blob, "the control: the rest of the snapshot is kept")

    def test_the_connector_itself_still_works(self):
        """Masking the history must not mask the live config."""
        self.storage.create_connector(_connector())
        live = self.storage.get_connector("bureau_x", include_secrets=True)
        self.assertEqual(live["config"]["api_key"], SECRET)

    def test_the_migration_scrubs_rows_written_before_the_fix(self):
        self.storage.create_connector(_connector())
        # Reproduce a pre-fix row: write the plaintext snapshot directly.
        with self.storage.connect() as session:
            row = session.scalar(sa.select(EntityHistory).where(EntityHistory.entity_id == "bureau_x"))
            row.snapshot = _connector()
        self.assertIn(SECRET, self._history_blob(), "the fixture must start with a plaintext row")

        spec = importlib.util.spec_from_file_location("scrub_0033", MIGRATION)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = self.storage.engine if hasattr(self.storage, "engine") else None
        self.assertIsNotNone(engine, "Storage exposes no engine to run the migration against")
        with engine.begin() as connection:
            from alembic.migration import MigrationContext
            from alembic.operations import Operations

            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            migration.upgrade()  # idempotent
        blob = self._history_blob()
        self.assertNotIn(SECRET, blob)
        self.assertIn("https://bureau.example.com", blob)


if __name__ == "__main__":
    unittest.main()
