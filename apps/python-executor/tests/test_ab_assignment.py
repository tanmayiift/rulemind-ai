"""A/B experiment assignment on the public decision API — POST /api/v1/decide.

A running champion/challenger experiment assigns each decision to a variant by hashing a
stable user id. Before this fix /api/v1/decide neither accepted nor threaded a user_id, so no
decision ever carried an experiment_variant and the A/B analytics stayed empty. These tests
prove a user_id now routes to a variant and that the experiment results populate.
"""
from __future__ import annotations

import os
import sys
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
from app.experiments import assign_variant  # noqa: E402


class AbAssignmentTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}
        self.tenant_id = app_main.storage.default_tenant_id
        self.policy = app_main.storage.list_policies(status="prod", tenant_id=self.tenant_id)[0]
        self.experiment = app_main.storage.create_or_update_experiment(
            {
                "id": "test_ab_exp",
                "name": "Test A/B",
                "status": "running",
                "target_policy_id": self.policy["id"],
                "hash_key": "user_id",
                "variants": [
                    {"id": "champion", "role": "champion", "weight": 50},
                    {"id": "challenger", "role": "challenger", "weight": 50},
                ],
            },
            tenant_id=self.tenant_id,
        )

    def tearDown(self):
        app_main.storage.create_or_update_experiment(
            {**self.experiment, "status": "completed"}, tenant_id=self.tenant_id
        )

    def _decide(self, user_id):
        return self.client.post(
            "/api/v1/decide",
            headers=self.headers,
            json={"policyId": self.policy["id"], "payload": {"amount": 5000, "bureau_score": 720}, "userId": user_id},
        )

    def test_decide_accepts_user_id_and_assigns_a_variant(self):
        for user in ["alice", "bob", "carol", "dave", "erin", "frank", "grace", "heidi"]:
            self.assertEqual(self._decide(user).status_code, 200)
        # The decision log should now carry variant tags for this experiment's variants.
        decisions = app_main.storage.list_decisions(tenant_id=self.tenant_id)
        tagged = [d for d in decisions if d.get("experiment_variant") in ("champion", "challenger")]
        self.assertGreater(len(tagged), 0, "decisions must carry an experiment_variant once a user_id is supplied")

    def test_assignment_is_stable_per_user(self):
        # The same user always maps to the same variant (hash is deterministic).
        first = assign_variant("stable-user", self.experiment["id"], self.experiment["variants"])
        second = assign_variant("stable-user", self.experiment["id"], self.experiment["variants"])
        self.assertIsNotNone(first)
        self.assertEqual(first["id"], second["id"])

    def test_results_endpoint_populates_after_decisions(self):
        for user in [f"user-{i}" for i in range(12)]:
            self._decide(user)
        results = self.client.get(
            "/api/v1/experiments/{0}/results".format(self.experiment["id"]), headers=self.headers
        ).json()
        total_users = sum(v["users"] for v in results["variants"])
        self.assertGreater(total_users, 0, "experiment analytics should show assigned users")


if __name__ == "__main__":
    unittest.main()
