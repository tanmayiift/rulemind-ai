"""Analytics aggregate over the full window, not a 200-row recency sample.

Experiment (A/B) and dashboard analytics previously called storage.list_decisions(), capped at
200 rows, so lift/significance/approval-rate were computed on a tiny biased sample. These tests
seed well over 200 decisions and assert the aggregates reflect all of them.
"""
from __future__ import annotations

import os
import sys
import unittest
import uuid
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

import app.main as app_main  # noqa: E402
from app.analytics import decision_analytics, experiment_analytics  # noqa: E402


class AnalyticsAggregationTests(unittest.TestCase):
    def setUp(self):
        self.storage = app_main.storage
        self.tenant_id = self.storage.default_tenant_id
        self.policy = self.storage.list_policies(status="prod", tenant_id=self.tenant_id)[0]
        self.exp_id = "agg_exp_" + uuid.uuid4().hex[:8]
        self.experiment = self.storage.create_or_update_experiment(
            {
                "id": self.exp_id,
                "name": "Aggregation",
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
        # Seed 600 experiment decisions (>> the old 200 cap), 300 per variant.
        self.total = 600
        for i in range(self.total):
            variant = "champion" if i % 2 == 0 else "challenger"
            self.storage.add_decision(
                {
                    "policy_id": self.policy["id"],
                    "outcome": "approve" if i % 3 == 0 else "reject",
                    "latency_ms": 10,
                    "source": "api",
                    "experiment_id": self.experiment["id"],
                    "experiment_variant": variant,
                    "payload_preview": {},
                },
                tenant_id=self.tenant_id,
            )

    def tearDown(self):
        self.storage.create_or_update_experiment(
            {**self.experiment, "status": "completed"}, tenant_id=self.tenant_id
        )

    def test_experiment_rollup_counts_all_decisions_not_just_200(self):
        rollup = self.storage.experiment_variant_rollup(self.tenant_id, self.experiment["id"])
        total_users = sum(v["users"] for v in rollup.values())
        self.assertEqual(total_users, self.total, "rollup must count all seeded decisions for THIS experiment")
        self.assertEqual(rollup["champion"]["users"], 300)
        self.assertEqual(rollup["challenger"]["users"], 300)

    def test_rollup_is_scoped_to_the_experiment(self):
        # A second experiment reusing the same variant ids must not cross-count.
        other = self.storage.create_or_update_experiment(
            {
                "id": "agg_exp_other_" + uuid.uuid4().hex[:8],
                "name": "Other",
                "status": "running",
                "target_policy_id": self.policy["id"],
                "variants": [{"id": "champion", "role": "champion", "weight": 100}],
            },
            tenant_id=self.tenant_id,
        )
        self.storage.add_decision(
            {"policy_id": self.policy["id"], "outcome": "approve", "experiment_id": other["id"], "experiment_variant": "champion", "payload_preview": {}},
            tenant_id=self.tenant_id,
        )
        mine = self.storage.experiment_variant_rollup(self.tenant_id, self.experiment["id"])
        theirs = self.storage.experiment_variant_rollup(self.tenant_id, other["id"])
        self.assertEqual(mine["champion"]["users"], 300, "the other experiment's champion must not leak in")
        self.assertEqual(theirs["champion"]["users"], 1)
        self.storage.create_or_update_experiment({**other, "status": "completed"}, tenant_id=self.tenant_id)

    def test_experiment_analytics_uses_full_aggregate(self):
        result = experiment_analytics(self.storage, self.tenant_id, self.experiment["id"])
        users = sum(v["users"] for v in result["variants"])
        self.assertGreaterEqual(users, self.total)

    def test_decision_facts_scans_beyond_200(self):
        facts = self.storage.decision_facts(tenant_id=self.tenant_id)
        self.assertGreater(len(facts), 200, "decision_facts must not be capped at 200")

    def test_decision_facts_is_column_projected(self):
        facts = self.storage.decision_facts(tenant_id=self.tenant_id)
        self.assertTrue(facts)
        # Lightweight projection — no heavy payload/trace columns.
        self.assertNotIn("payload", facts[0])
        self.assertNotIn("trace", facts[0])
        self.assertIn("outcome", facts[0])


if __name__ == "__main__":
    unittest.main()
