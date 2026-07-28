"""Workflow engine Phase 1: multi-branch routing + sub-workflow composition."""
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
from app.storage import Storage  # noqa: E402


class WorkflowEngineTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "wf.db"))
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def _create_policy(self, name, steps):
        r = self.client.post("/api/v1/policies", json={"name": name, "steps": steps}, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["id"]

    def _decide(self, policy_id, payload):
        return self.client.post(
            "/api/v1/decide", json={"policy_id": policy_id, "payload": payload}, headers=self.headers
        ).json()

    def test_branch_routes_by_condition(self):
        pid = self._create_policy(
            "Branching",
            [
                {
                    "type": "branch",
                    "config": {
                        "branches": [
                            {
                                "label": "high-risk",
                                "condition": "payload['custom']['amount'] > 1000",
                                "steps": [{"type": "outcome", "ref_id": "reject"}],
                            }
                        ],
                        "default": [{"type": "outcome", "ref_id": "approve"}],
                    },
                }
            ],
        )
        self.assertEqual(self._decide(pid, {"amount": 5000})["outcome"], "reject")
        self.assertEqual(self._decide(pid, {"amount": 100})["outcome"], "approve")

    def test_subworkflow_composition(self):
        sub = self._create_policy("Fraud sub-workflow", [{"type": "outcome", "ref_id": "reject"}])
        parent = self._create_policy(
            "Parent",
            [
                {"type": "outcome", "ref_id": "approve"},
                {"type": "workflow", "ref_id": sub},  # sub rejects -> reject wins by precedence
            ],
        )
        result = self._decide(parent, {"amount": 1})
        self.assertEqual(result["outcome"], "reject")
        self.assertTrue(any(entry.get("step", {}).get("type") == "workflow" for entry in result["trace"]))

    def test_subworkflow_cycle_is_guarded(self):
        # A references B, B references A -> the second hop is a cycle and is caught.
        a = self._create_policy("A", [{"type": "outcome", "ref_id": "approve"}])
        b = self._create_policy("B", [{"type": "workflow", "ref_id": a}])
        # point A at B to form the cycle
        self.client.put(
            f"/api/v1/policies/{a}",
            json={"name": "A", "steps": [{"type": "workflow", "ref_id": b}]},
            headers=self.headers,
        )
        result = self._decide(a, {})
        # cycle error is captured in the trace (non-aborting), decision still returns
        self.assertIn("outcome", result)
        errors = [e.get("error") for e in result["trace"] if e.get("error")]
        self.assertTrue(any("cycle" in str(e).lower() for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
