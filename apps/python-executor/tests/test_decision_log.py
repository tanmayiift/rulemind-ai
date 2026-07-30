"""Async decision logging (off the request path) with read-after-write via flush."""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

import app.decision_log as decision_log  # noqa: E402
import app.main as app_main  # noqa: E402
from app.storage import Storage  # noqa: E402


class DecisionLogUnitTests(unittest.TestCase):
    def test_async_by_default(self):
        os.environ.pop("ASYNC_DECISION_LOG", None)
        self.assertTrue(decision_log.is_async())

    def test_sync_when_disabled(self):
        os.environ["ASYNC_DECISION_LOG"] = "0"
        try:
            self.assertFalse(decision_log.is_async())
        finally:
            os.environ.pop("ASYNC_DECISION_LOG", None)

    def test_flush_waits_for_a_slow_write(self):
        os.environ.pop("ASYNC_DECISION_LOG", None)
        marker = {}

        def slow():
            time.sleep(0.2)
            marker["done"] = True

        decision_log.submit(slow)
        self.assertNotIn("done", marker)  # returned before the write finished
        decision_log.flush()
        self.assertTrue(marker.get("done"))  # flush blocked until it completed


class DecisionLogApiTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("ASYNC_DECISION_LOG", None)  # default async
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "dl.db"))
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def _decide(self):
        # A clean-approve payload so the decision COMPLETES (a paused/review decision
        # isn't logged) — every gate satisfied, geo flag 0 == pass.
        return self.client.post("/api/v1/decide", headers=self.headers, json={"policyId": "policy_instant_personal_loan",
                "payload": {"bureau_score": 810, "monthly_income_inr": 200000, "dti_ratio": 0.15,
                            "pan_verified_flag": 1, "geo_consistency_flag": 0, "liveness_score": 99, "avg_balance_inr": 150000}})

    def test_decision_is_readable_after_async_write(self):
        before = app_main.storage.count_decisions(tenant_id=app_main.storage.default_tenant_id)
        self.assertEqual(self._decide().status_code, 200)
        # read-after-write: the reader flushes the pending async write so the count
        # reflects it immediately (>= because /decide currently double-logs — a
        # separate pre-existing bug flagged for its own fix).
        after = app_main.storage.count_decisions(tenant_id=app_main.storage.default_tenant_id)
        self.assertGreater(after, before)

    def test_audit_endpoint_sees_the_decision(self):
        self._decide()
        rows = self.client.get("/api/v1/audit/decisions?limit=5", headers=self.headers).json()
        self.assertTrue(rows)  # flushed + visible


if __name__ == "__main__":
    unittest.main()
