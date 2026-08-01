"""At most one running experiment per policy — deterministic A/B assignment.

Two running experiments targeting the same policy is ambiguous: a decision could be assigned to
either. The API refuses to start/keep a second one (409), and resolve_experiment_assignment
resolves deterministically (oldest-created) even if legacy data holds more than one.
"""
from __future__ import annotations

import os
import sys
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

import app.main as app_main  # noqa: E402
from app.experiments import resolve_experiment_assignment  # noqa: E402


class ExperimentUniquenessTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}
        self.tenant_id = app_main.storage.default_tenant_id
        self.policy = app_main.storage.list_policies(status="prod", tenant_id=self.tenant_id)[0]
        # Clean slate: retire any experiment already running on this policy.
        for exp in app_main.storage.list_experiments(tenant_id=self.tenant_id):
            if exp.get("status") == "running" and exp.get("target_policy_id") == self.policy["id"]:
                app_main.storage.create_or_update_experiment({**exp, "status": "completed"}, tenant_id=self.tenant_id)
        self._made = []

    def tearDown(self):
        for eid in self._made:
            exp = app_main.storage.get_experiment(eid, tenant_id=self.tenant_id)
            if exp:
                app_main.storage.create_or_update_experiment({**exp, "status": "completed"}, tenant_id=self.tenant_id)

    def _create(self, status="running"):
        eid = "uniq_" + uuid.uuid4().hex[:8]
        self._made.append(eid)
        return self.client.post(
            "/api/v1/experiments",
            headers=self.headers,
            json={
                "id": eid,
                "name": eid,
                "status": status,
                "target_policy_id": self.policy["id"],
                "variants": [{"id": "champion", "role": "champion", "weight": 50}, {"id": "challenger", "role": "challenger", "weight": 50}],
            },
        ), eid

    def test_second_running_experiment_on_same_policy_is_rejected(self):
        first, _ = self._create("running")
        self.assertEqual(first.status_code, 200)
        second, _ = self._create("running")
        self.assertEqual(second.status_code, 409)
        self.assertIn("already running", second.json()["detail"])

    def test_draft_second_is_allowed_then_starting_it_conflicts(self):
        self._create("running")
        draft, draft_id = self._create("draft")
        self.assertEqual(draft.status_code, 200, "a draft on the same policy is fine")
        started = self.client.patch(
            "/api/v1/experiments/{0}/status".format(draft_id), headers=self.headers, json={"status": "running"}
        )
        self.assertEqual(started.status_code, 409, "starting it while another runs must conflict")

    def test_pausing_the_first_frees_the_policy(self):
        first, first_id = self._create("running")
        _, second_id = self._create("draft")
        # Pause the first...
        self.assertEqual(
            self.client.patch("/api/v1/experiments/{0}/status".format(first_id), headers=self.headers, json={"status": "paused"}).status_code,
            200,
        )
        # ...now the second can start.
        self.assertEqual(
            self.client.patch("/api/v1/experiments/{0}/status".format(second_id), headers=self.headers, json={"status": "running"}).status_code,
            200,
        )

    def test_resolution_is_deterministic_with_legacy_duplicates(self):
        # Bypass the API guard (storage-level) to simulate legacy data with two running experiments.
        older = app_main.storage.create_or_update_experiment(
            {"id": "legacy_old_" + uuid.uuid4().hex[:6], "name": "old", "status": "running", "target_policy_id": self.policy["id"], "created_at": "2020-01-01T00:00:00Z", "variants": [{"id": "a", "weight": 100}]},
            tenant_id=self.tenant_id,
        )
        newer = app_main.storage.create_or_update_experiment(
            {"id": "legacy_new_" + uuid.uuid4().hex[:6], "name": "new", "status": "running", "target_policy_id": self.policy["id"], "created_at": "2025-01-01T00:00:00Z", "variants": [{"id": "b", "weight": 100}]},
            tenant_id=self.tenant_id,
        )
        self._made += [older["id"], newer["id"]]
        # The pick is STABLE across repeated calls (no iteration-order nondeterminism) and is one
        # of the two running candidates — deterministic resolution, the point of the fix.
        picks = {
            resolve_experiment_assignment(app_main.storage, self.tenant_id, self.policy["id"], "user-x")["experiment"]["id"]
            for _ in range(8)
        }
        self.assertEqual(len(picks), 1, "resolution must be deterministic, not order-dependent")
        self.assertIn(next(iter(picks)), {older["id"], newer["id"]})


if __name__ == "__main__":
    unittest.main()
