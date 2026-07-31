"""Regression: exactly ONE Decision row per /decide, on both the heavy and fast paths.

Previously the heavy path double-logged (the executor logged, and the /decide
endpoint's test_policy_entity logged again), inflating reports, onboarding activation
counts, and per-tenant usage metering. This guards the single-log invariant.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

import app.main as app_main  # noqa: E402
from app.storage import Storage  # noqa: E402

_PAYLOAD = {"bureau_score": 810, "monthly_income_inr": 200000, "dti_ratio": 0.15,
            "pan_verified_flag": 1, "geo_consistency_flag": 0, "liveness_score": 99, "avg_balance_inr": 150000}


class SingleDecisionLogTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("FAST_DECIDE", None)
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "sl.db"))
        self.client = TestClient(app_main.app)
        self.tenant = app_main.storage.default_tenant_id
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def tearDown(self):
        os.environ.pop("FAST_DECIDE", None)
        self.client.close()
        self.tempdir.cleanup()

    def _count(self):
        return app_main.storage.count_decisions(tenant_id=self.tenant)

    def _decide(self):
        return self.client.post("/api/v1/decide", headers=self.headers,
                                json={"policyId": "policy_instant_personal_loan", "payload": _PAYLOAD})

    def test_heavy_path_logs_exactly_one(self):
        before = self._count()
        self.assertEqual(self._decide().status_code, 200)
        self.assertEqual(self._count(), before + 1)

    def test_heavy_path_row_is_source_api(self):
        self._decide()
        rows = app_main.storage.list_decisions(tenant_id=self.tenant, limit=10)
        api_rows = [r for r in rows if r.get("source") == "api"]
        self.assertEqual(len(api_rows), 1)  # one production row, tagged source=api

    def test_three_decides_add_three_rows(self):
        before = self._count()
        for _ in range(3):
            self.assertEqual(self._decide().status_code, 200)
        self.assertEqual(self._count(), before + 3)  # was +6 before the fix

    def test_fast_path_logs_exactly_one(self):
        os.environ["FAST_DECIDE"] = "1"
        before = self._count()
        self.assertEqual(self._decide().status_code, 200)
        self.assertEqual(self._count(), before + 1)

    def test_test_console_does_not_double_log(self):
        # The test-console endpoint runs the executor once -> at most one row, tagged
        # source=test_console (never a second, api-sourced duplicate).
        before = self._count()
        r = self.client.post("/api/v1/policies/policy_instant_personal_loan/execute",
                             headers=self.headers, json={"payload": _PAYLOAD})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._count(), before + 1)
        rows = app_main.storage.list_decisions(tenant_id=self.tenant, limit=10)
        self.assertEqual(len([x for x in rows if x.get("source") == "test_console"]), 1)


if __name__ == "__main__":
    unittest.main()
