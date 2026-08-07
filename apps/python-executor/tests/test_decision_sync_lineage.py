"""On-device decision sync: lineage (device_id, bundle_hash), server receipt time, and
race/collision-safe idempotency.

Extends the existing idempotent-batch guarantee with: a trustworthy server received_at that is
independent of a drifted device clock, device + bundle lineage on each on-device decision, tenant
scoping of the dedupe (one tenant's id can never shadow another's), and per-row savepoint handling so
a PK clash under concurrent ingest degrades to a duplicate instead of failing the whole batch.
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

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")

import app.main as app_main  # noqa: E402
from app.storage import Storage  # noqa: E402


def _decision(did, **extra):
    base = {"id": did, "policy_id": "policy_instant_personal_loan", "outcome": "approve"}
    base.update(extra)
    return base


class DecisionSyncLineageTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "sync.db"))
        self.tenant = app_main.storage.default_tenant_id

    def tearDown(self):
        self.tempdir.cleanup()

    def _row(self, client_id, tenant=None):
        rows = app_main.storage.list_decisions(tenant_id=tenant or self.tenant, limit=50)
        return next(r for r in rows if r["client_id"] == client_id)

    def test_lineage_and_server_time_recorded(self):
        app_main.storage.add_decisions_batch(
            [_decision("d1", device_id="device-abc", bundle_hash="bundle-v7",
                       created_at="2026-07-31T10:00:00Z")],
            tenant_id=self.tenant,
        )
        row = self._row("d1")
        self.assertEqual(row["device_id"], "device-abc")
        self.assertEqual(row["bundle_hash"], "bundle-v7")
        self.assertTrue(str(row["created_at"]).startswith("2026-07-31"))  # device clock preserved
        self.assertIsNotNone(row["received_at"])                          # server receipt time set
        self.assertFalse(str(row["received_at"]).startswith("2026-07-31"))  # and it is NOT the device time

    def test_idempotent_retry_still_holds(self):
        batch = [_decision(f"x{i}", device_id="dev") for i in range(4)]
        first = app_main.storage.add_decisions_batch(batch, tenant_id=self.tenant)
        self.assertEqual(first["inserted"], 4)
        retry = app_main.storage.add_decisions_batch(batch, tenant_id=self.tenant)
        self.assertEqual(retry["inserted"], 0)
        self.assertEqual(retry["duplicates"], 4)

    def test_dedupe_is_tenant_scoped(self):
        # A second tenant reusing the same client id must NOT be treated as a duplicate of tenant 1.
        other = app_main.storage.create_tenant("Second Co")
        app_main.storage.add_decisions_batch([_decision("shared-id")], tenant_id=self.tenant)
        res = app_main.storage.add_decisions_batch([_decision("shared-id")], tenant_id=other["id"])
        self.assertEqual(res["inserted"], 1, "same id in a different tenant must insert, not dedupe")
        self.assertEqual(res["duplicates"], 0)

    def test_api_decision_has_null_lineage_but_server_time(self):
        app_main.storage.add_decision(
            {"policy_id": "p", "outcome": "approve", "payload": {}, "id": "api-1"}, tenant_id=self.tenant
        )
        rows = app_main.storage.list_decisions(tenant_id=self.tenant, limit=50)
        row = next(r for r in rows if r["id"] == "api-1")
        self.assertIsNone(row["client_id"])
        self.assertIsNone(row["device_id"])
        self.assertIsNone(row["bundle_hash"])
        self.assertIsNotNone(row["received_at"])


if __name__ == "__main__":
    unittest.main()
