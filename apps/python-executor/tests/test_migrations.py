"""Alembic migration chain — regression coverage.

Guards two things that have broken before:
* the chain is contiguous and applies cleanly to head on a fresh DB, and
* it is safe to run against a database that was bootstrapped by create_all
  (no alembic_version) — 0001 is create_all (idempotent), and the later guarded
  migrations still add any missing columns/tables.

This is the exact path the container entrypoint runs (`alembic -c alembic.ini
upgrade head`), so a broken down_revision or a missing alembic.ini fails here.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(APP_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(APP_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


# Derive the expected head from the migration scripts so this never needs a manual
# bump when a new migration is added (and a broken/branched chain fails loudly).
def _expected_head() -> str:
    return ScriptDirectory.from_config(_alembic_config("sqlite://")).get_current_head()


HEAD = _expected_head()


class MigrationChainTests(unittest.TestCase):
    def test_alembic_ini_exists(self):
        # The container entrypoint runs `alembic -c alembic.ini upgrade head`.
        self.assertTrue((APP_ROOT / "alembic.ini").exists(), "alembic.ini is required by entrypoint.sh")

    def test_upgrade_head_on_fresh_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "fresh.db")
            url = f"sqlite:///{db}"
            os.environ["DATABASE_URL"] = url
            try:
                command.upgrade(_alembic_config(url), "head")
            finally:
                os.environ.pop("DATABASE_URL", None)
            conn = sqlite3.connect(db)
            version = list(conn.execute("SELECT version_num FROM alembic_version"))
            self.assertEqual(version, [(HEAD,)])
            cols = {r[1] for r in conn.execute("PRAGMA table_info(api_keys)")}
            self.assertIn("environment", cols)
            self.assertIn("label", cols)
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("decision_tables", tables)
            conn.close()

    def test_upgrade_head_on_create_all_bootstrapped_db(self):
        # Simulates an existing prod DB that was created by create_all (the old
        # entrypoint fallback), which has no alembic_version stamp.
        from sqlalchemy import create_engine
        from app.models import Base
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "existing.db")
            url = f"sqlite:///{db}"
            Base.metadata.create_all(create_engine(url))
            os.environ["DATABASE_URL"] = url
            try:
                command.upgrade(_alembic_config(url), "head")  # must not raise "table already exists"
            finally:
                os.environ.pop("DATABASE_URL", None)
            conn = sqlite3.connect(db)
            self.assertEqual(list(conn.execute("SELECT version_num FROM alembic_version")), [(HEAD,)])
            conn.close()


if __name__ == "__main__":
    unittest.main()
