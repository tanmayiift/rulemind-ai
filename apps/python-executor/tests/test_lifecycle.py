"""Policy lifecycle stage transitions."""
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
from app.lifecycle import allowed_transitions, can_transition  # noqa: E402
from app.storage import Storage  # noqa: E402


class LifecyclePureTests(unittest.TestCase):
    def test_valid_and_invalid(self):
        self.assertTrue(can_transition("draft", "in_review"))
        self.assertTrue(can_transition("in_review", "ready"))
        self.assertTrue(can_transition("ready", "live"))
        self.assertFalse(can_transition("draft", "live"))
        self.assertFalse(can_transition("archived", "draft"))
        self.assertEqual(allowed_transitions("in_review"), ["ready", "rejected", "draft"])


class LifecycleApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "lc.db"))
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}
        policies = self.client.get("/api/v1/policies", headers=self.headers).json()
        self.policy_id = policies[0]["id"]

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def test_default_stage_is_draft(self):
        body = self.client.get(f"/api/v1/policies/{self.policy_id}/lifecycle", headers=self.headers).json()
        self.assertEqual(body["stage"], "draft")
        self.assertEqual(body["allowedTransitions"], ["in_review"])

    def test_happy_path_to_live(self):
        for target in ["in_review", "ready", "live"]:
            r = self.client.post(f"/api/v1/policies/{self.policy_id}/lifecycle", json={"target": target, "actor": "t"}, headers=self.headers)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["stage"], target)

    def test_illegal_transition_blocked(self):
        r = self.client.post(f"/api/v1/policies/{self.policy_id}/lifecycle", json={"target": "live"}, headers=self.headers)
        self.assertEqual(r.status_code, 422, r.text)


if __name__ == "__main__":
    unittest.main()
