"""End-to-end smoke tests — a fast health check of the whole system.

Boots the FastAPI app against a fresh seeded DB and exercises the critical paths
a deploy must never break: health, decisioning, MECE analysis, the stateless
core service, and the champion/challenger promotion gate.
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
os.environ.setdefault("RULEMIND_ADMIN_JWT_SECRET", "rulemind-test-admin-secret")

import app.main as app_main  # noqa: E402
from app.core import service as core_service  # noqa: E402
from app.storage import Storage  # noqa: E402


class SmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.tempdir.name, "smoke.db")
        app_main.storage = Storage(path=db_path)
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def tearDown(self) -> None:
        self.client.close()
        self.tempdir.cleanup()

    def test_health_and_ready(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/ready").status_code, 200)

    def test_decide_on_seeded_policy(self):
        policies = self.client.get("/api/v1/policies", headers=self.headers).json()
        if not policies:
            self.skipTest("no seeded policies")
        policy_id = policies[0]["id"]
        resp = self.client.post(
            "/api/v1/decide",
            json={"policy_id": policy_id, "payload": {"user_id": "smoke-1"}},
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn(resp.json()["outcome"], {"approve", "review", "reject", "pending"})

    def test_mece_analysis(self):
        policies = self.client.get("/api/v1/policies", headers=self.headers).json()
        if not policies:
            self.skipTest("no seeded policies")
        policy_id = policies[0]["id"]
        resp = self.client.post(f"/api/v1/policies/{policy_id}/analyze-mece", headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertIn("isMutuallyExclusive", body)
        self.assertIn("isCollectivelyExhaustive", body)

    def test_stateless_core_service(self):
        core_service.set_bundle(
            {
                "policy": {"id": "p", "steps": [{"type": "rule", "ref_id": "r"}]},
                "rules": {
                    "r": {
                        "id": "r",
                        "rule_format": "v2",
                        "tree": {
                            "type": "group",
                            "logic": "AND",
                            "children": [{"type": "condition", "variable": "score", "operator": ">=", "value": 700}],
                            "onPass": "approve",
                            "onFail": "reject",
                        },
                    }
                },
            }
        )
        core_client = TestClient(core_service.app)
        self.assertEqual(core_client.get("/health").json()["bundle_loaded"], True)
        self.assertEqual(core_client.post("/decide", json={"payload": {"score": 800}}).json()["outcome"], "approve")
        self.assertEqual(core_client.post("/decide", json={"payload": {"score": 500}}).json()["outcome"], "reject")

    def test_champion_challenger_promotion_gate(self):
        create = self.client.post(
            "/api/v1/experiments",
            json={
                "name": "Smoke CC",
                "status": "running",
                "target_policy_id": "credit_v1",
                "variants": [
                    {"id": "champion", "role": "champion", "weight": 50},
                    {"id": "challenger", "role": "challenger", "weight": 50, "overrides": {}},
                ],
            },
            headers=self.headers,
        )
        self.assertEqual(create.status_code, 200, create.text)
        exp_id = create.json()["id"]

        # No decisions yet -> recommendation is "hold" -> promotion blocked (422).
        blocked = self.client.post(
            f"/api/v1/experiments/{exp_id}/promote",
            json={"variant_id": "challenger"},
            headers=self.headers,
        )
        self.assertEqual(blocked.status_code, 422, blocked.text)

        # force=true overrides the gate and completes the experiment.
        forced = self.client.post(
            f"/api/v1/experiments/{exp_id}/promote",
            json={"variant_id": "challenger", "force": True, "promoted_by": "smoke"},
            headers=self.headers,
        )
        self.assertEqual(forced.status_code, 200, forced.text)
        self.assertEqual(self.client.get(f"/api/v1/experiments/{exp_id}", headers=self.headers).json()["status"], "completed")


if __name__ == "__main__":
    unittest.main()
