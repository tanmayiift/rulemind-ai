"""Runs LAST -- unittest discovers modules in name order -- and fails if the run changed the lane.

The Postgres lane is `DATABASE_URL` + `RULEMIND_TEST_PG_SCHEMA_PER_PATH=1`. Four tests set
`os.environ["DATABASE_URL"]` to a SQLite file for an alembic run and then POPPED it rather than
restoring it. From the first of them (test_migrations) onward, `Storage(path=...)` resolved to
SQLite, so 99 of the 204 modules in every full Postgres run were a second SQLite run -- and the
lane reported green. Found only because tests/test_rls.py decides whether to skip at IMPORT,
while the variable was still set, and then got a SQLite database at run time.

This module's import happens during discovery, before any test runs, so what it captures is the
environment the lane was started with.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

# The sandbox pair too: a leaked SANDBOX_MODE=inline would quietly turn every later test of the
# pool -- the one-trip variable group included -- into a test of the inline path.
LANE_VARIABLES = ("DATABASE_URL", "RULEMIND_TEST_PG_SCHEMA_PER_PATH", "SANDBOX_MODE", "SANDBOX_BATCH")
AT_START = {name: os.environ.get(name) for name in LANE_VARIABLES}


class LaneIntactTests(unittest.TestCase):
    def test_the_lane_environment_survived_the_run(self):
        changed = {name: (AT_START[name], os.environ.get(name)) for name in LANE_VARIABLES
                   if os.environ.get(name) != AT_START[name]}
        self.assertEqual(changed, {}, "a test changed the lane's environment and did not restore it; "
                                      "every module after it ran against a different database")

    @unittest.skipUnless((AT_START["DATABASE_URL"] or "").startswith("postgresql")
                         and AT_START["RULEMIND_TEST_PG_SCHEMA_PER_PATH"] == "1",
                         "the SQLite lane has no second database to lose")
    def test_the_last_module_still_gets_postgres(self):
        from app.storage import Storage

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            storage = Storage(path=os.path.join(tmp, "lane.db"))
            self.assertEqual(storage.engine.dialect.name, "postgresql")


if __name__ == "__main__":
    unittest.main()
