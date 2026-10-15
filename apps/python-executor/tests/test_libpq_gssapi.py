"""Every Postgres engine connects with GSSAPI encryption off unless the operator asks for it.

libpq's default `gssencmode=prefer` initialises Kerberos on every connect, and that path is not
thread-safe in the libpq psycopg2-binary bundles: concurrent connects segfaulted the Postgres
test lane (decision-log thread inside psycopg2.connect, per faulthandler). The same thread pool
runs in production. These tests pin the option on both engine kinds and the opt-out.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app import db  # noqa: E402

URL = "postgresql://u:p@db.example:5432/rulemind"


class GssapiOffTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(db._engine_cache.clear)
        db._engine_cache.clear()

    def _connect_args(self, url=URL, path=None, extra_env=None):
        env = {"DATABASE_URL": url, "RULEMIND_TEST_PG_SCHEMA_PER_PATH": "1" if path else "0", **(extra_env or {})}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(db, "create_engine") as create, \
                mock.patch.object(db, "_bind_test_schema"), \
                mock.patch.object(db, "_warn_if_sqlite_in_production"):
            if not extra_env:
                os.environ.pop("PGGSSENCMODE", None)
            db.engine_for(path)
        return create.call_args.kwargs.get("connect_args", {})

    def test_the_pooled_engine_disables_gssapi(self):
        self.assertEqual(self._connect_args().get("gssencmode"), "disable")

    def test_the_schema_per_path_engine_disables_gssapi_and_keeps_search_path(self):
        args = self._connect_args(path="/tmp/gss-test.db")
        self.assertEqual(args.get("gssencmode"), "disable")
        self.assertIn("search_path=rmtest_", args.get("options", ""))

    def test_an_operator_who_uses_kerberos_can_opt_back_in(self):
        self.assertNotIn("gssencmode", self._connect_args(url=URL + "?gssencmode=require"))
        self.assertNotIn("gssencmode", self._connect_args(extra_env={"PGGSSENCMODE": "prefer"}))


if __name__ == "__main__":
    unittest.main()
