"""Promoting a variant must leave a record of WHAT was promoted and BY WHOM.

`promote_experiment` writes `promoted_variant_id`, `promoted_by` and `promoted_at`. Until
2026-09-16 the Experiment model had no columns for them and `create_or_update_experiment` copies
only known fields, so all three were dropped: the endpoint returned 200, the status moved to
"completed", and a subsequent GET reported `promoted_variant_id: None`.

Found by the evaluations sweep, by promoting through the API and reading the experiment back --
not by reading the code, which looks correct at the call site.
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
os.environ.pop("AUTH_MODE", None)

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import preserve_storage_global  # noqa: E402

EXP = "sweep-promo-record"


class ExperimentPromotionRecordTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        preserve_storage_global(self)
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "exp.db"))
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}
        self.client.post("/api/v1/experiments", headers=self.headers, json={
            "id": EXP, "name": EXP, "status": "running", "hash_key": "user_id",
            "variants": [
                {"id": "champion", "role": "champion", "weight": 50},
                {"id": "challenger", "role": "challenger", "weight": 50},
            ],
        })

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def test_storage_round_trips_the_promotion_fields(self):
        """The unit-level statement, at the layer that was dropping them."""
        app_main.storage.create_or_update_experiment({
            "id": EXP, "name": EXP, "status": "completed",
            "variants": [{"id": "challenger", "role": "challenger", "weight": 100}],
            "promoted_variant_id": "challenger",
            "promoted_by": "alice@example.invalid",
            "promoted_at": "2026-09-16T12:00:00Z",
        })
        stored = app_main.storage.get_experiment(EXP)
        self.assertEqual(stored["promoted_variant_id"], "challenger")
        self.assertEqual(stored["promoted_by"], "alice@example.invalid")
        self.assertTrue(
            stored["promoted_at"],
            "promoted_at must survive the round trip; it is written as an ISO STRING into a "
            "DateTime column, which is the half of this that SQLite would accept silently")
        self.assertIn("2026-09-16", str(stored["promoted_at"]))

    def test_promoting_through_the_api_leaves_the_record_on_the_entity(self):
        """The end-to-end statement: promote, then read it back the way a caller would."""
        promoted = self.client.post(
            f"/api/v1/experiments/{EXP}/promote", headers=self.headers,
            json={"variant_id": "challenger", "force": True, "promoted_by": "bob@example.invalid"},
        )
        self.assertEqual(promoted.status_code, 200, promoted.text)

        after = self.client.get(f"/api/v1/experiments/{EXP}", headers=self.headers).json()
        self.assertEqual(after["status"], "completed")
        self.assertEqual(
            after["promoted_variant_id"], "challenger",
            "the promotion reported success -- the entity must say which variant won, or the "
            "only record is an audit row nothing reads")
        self.assertEqual(after["promoted_by"], "bob@example.invalid")
        self.assertTrue(after["promoted_at"])

    def test_an_unpromoted_experiment_reports_no_provenance(self):
        """The control. Without it, a serializer hardcoding a value would pass the two above."""
        fresh = self.client.get(f"/api/v1/experiments/{EXP}", headers=self.headers).json()
        self.assertIsNone(fresh["promoted_variant_id"])
        self.assertIsNone(fresh["promoted_by"])
        self.assertIsNone(fresh["promoted_at"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
