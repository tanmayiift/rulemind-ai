"""Regression: simulation/decision accuracy.

Guards the bug where every simulated case was approved regardless of its inputs:
a flat top-level field (from synthetic/CSV/JSON) never reached the source's
variables, so the source silently kept its (approving) sample_payload. The fix
makes flat fields drive every source that declares them, so a low bureau_score
now correctly fails the >=700 gate.
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
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")  # tests use the sample lending inventory
os.environ.setdefault("RULEMIND_ADMIN_JWT_SECRET", "rulemind-test-admin-secret")
os.environ.pop("AUTH_MODE", None)

import app.main as app_main  # noqa: E402
from app.storage import Storage  # noqa: E402

POLICY = "policy_instant_personal_loan"


class SimulationAccuracyTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "sim.db"))
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def _decide(self, payload):
        return self.client.post("/api/v1/decide", headers=self.headers,
                                json={"policyId": POLICY, "payload": payload}).json()

    @staticmethod
    def _case(bureau):
        # A full, gate-satisfying loan case with only the bureau score varying —
        # mirrors what the policy-aware synthetic generator now produces.
        return {"bureau_score": bureau, "monthly_income_inr": 200000, "dti_ratio": 0.15,
                "pan_verified_flag": 1, "geo_consistency_flag": 0, "liveness_score": 99, "avg_balance_inr": 150000}

    def test_low_bureau_score_does_not_approve(self):
        # The gate is bureau_score >= 700 -> approve, else review. 590 must NOT approve.
        result = self._decide({"bureau_score": 590})
        self.assertNotEqual(result["outcome"], "approve", result)

    def test_high_bureau_score_can_approve(self):
        # A strong applicant (all gates satisfied) approves.
        result = self._decide(self._case(810))
        self.assertEqual(result["outcome"], "approve", result)

    def test_outcome_varies_with_score(self):
        # The whole point of a backtest: different inputs -> different outcomes.
        outcomes = {self._decide(self._case(s))["outcome"] for s in (520, 660, 705, 800)}
        self.assertGreater(len(outcomes), 1, "outcomes should vary with the bureau score")

    def test_flat_field_overrides_sample_payload(self):
        # Directly asserts the fix: a flat field beats the connector's sample value.
        low = self._decide({"bureau_score": 400})
        high = self._decide({"bureau_score": 400, "monthly_income_inr": 200000, "dti_ratio": 0.1,
                             "pan_verified_flag": 1, "geo_consistency_flag": 1, "liveness_score": 99})
        self.assertNotEqual(low["outcome"], "approve")
        # even with other gates satisfied, the sub-700 bureau score blocks approval
        self.assertNotEqual(high["outcome"], "approve")

    def test_input_schema_exposes_real_fields(self):
        schema = self.client.get(f"/api/v1/policies/{POLICY}/input-schema", headers=self.headers).json()
        names = {f["name"] for f in schema["fields"]}
        self.assertIn("bureau_score", names)
        self.assertTrue(any(s["source_id"] == "loan" for s in schema["sources"]))

    def test_batch_simulation_outcomes_vary(self):
        payloads = [self._case(s) for s in range(500, 860, 20)]
        res = self.client.post("/api/v1/decide/batch", headers=self.headers,
                               json={"targetType": "decide", "targetId": POLICY, "payloads": payloads}).json()
        outcomes = {(row.get("result") or {}).get("outcome") or row.get("outcome") for row in res["rows"]}
        self.assertGreater(len(outcomes), 1, res)

    def test_batch_reports_throughput_and_does_not_persist(self):
        # Simulation is a pure what-if: it returns real throughput and logs nothing.
        before = self.client.get("/api/v1/audit/decisions?limit=1", headers=self.headers).json()
        payloads = [self._case(700 + (i % 100)) for i in range(60)]
        res = self.client.post("/api/v1/decide/batch", headers=self.headers,
                               json={"targetType": "decide", "targetId": POLICY, "payloads": payloads}).json()
        perf = res["performance"]
        self.assertIn(perf["path"], {"fast", "full_executor"})
        self.assertIsNotNone(perf["throughput_tps"])
        self.assertGreaterEqual(perf["workers"], 4)
        # no simulated decision was written to the audit log
        after = self.client.get("/api/v1/audit/decisions?limit=1", headers=self.headers).json()
        self.assertEqual(len(before), len(after))

    def test_batch_rows_preserve_input_order(self):
        payloads = [self._case(s) for s in (520, 800, 610)]
        res = self.client.post("/api/v1/decide/batch", headers=self.headers,
                               json={"targetType": "decide", "targetId": POLICY, "payloads": payloads}).json()
        self.assertEqual([r["index"] for r in res["rows"]], [0, 1, 2])


class FastPathAccuracyTests(unittest.TestCase):
    def test_flat_field_overrides_sample_on_fast_path(self):
        # The scalable hot path (fast_decide) must apply the same field override.
        from app import fast_decide
        bundle = {
            "connectors": {"loan": {"bureau_score": 756}},
            "variables": [{"id": "loan_bureau_score", "source_id": "loan",
                           "code": "def run(payload, context):\n    return int(payload.get('bureau_score', 0))\n"}],
            "timeout_ms": 2000, "memory_mb": 128,
        }
        values, _ = fast_decide._compute_variables(bundle, {"bureau_score": 590})
        self.assertEqual(values["loan_bureau_score"], 590)  # not the sample 756


if __name__ == "__main__":
    unittest.main()
