"""Decision replay against the current — or a historical — bundle version.

"Did my policy change flip decisions that already went out?" Re-runs a past decision's stored
inputs (recorded computed variables, so payload redaction doesn't matter) through the compiled
bundle and reports whether the outcome changed.
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


class DecisionReplayTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}
        self.tenant_id = app_main.storage.default_tenant_id
        self.policy = app_main.storage.list_policies(status="prod", tenant_id=self.tenant_id)[0]
        # Ensure a compiled bundle exists to replay against.
        self.client.get("/sdk/v1/bundle", headers=self.headers)

    def _make_decision(self, outcome, variables):
        return app_main.storage.add_decision(
            {"policy_id": self.policy["id"], "outcome": outcome, "payload": {"amount": 1000},
             "computed_variables": variables},
            tenant_id=self.tenant_id,
        )

    def test_replay_returns_shape_and_is_deterministic(self):
        d = self._make_decision("approve", {"loan_bureau_score": 720, "dti": 0.3})
        r1 = self.client.post("/api/v1/decisions/{0}/replay".format(d["id"]), headers=self.headers)
        self.assertEqual(r1.status_code, 200, r1.text)
        body = r1.json()
        self.assertEqual(body["decision_id"], d["id"])
        self.assertEqual(body["original_outcome"], "approve")
        self.assertIn(body["replayed_outcome"], ("approve", "review", "reject", "pending"))
        self.assertIsInstance(body["changed"], bool)
        self.assertIsNotNone(body["bundle_version"])
        # Deterministic: replaying again yields the same outcome.
        r2 = self.client.post("/api/v1/decisions/{0}/replay".format(d["id"]), headers=self.headers)
        self.assertEqual(r2.json()["replayed_outcome"], body["replayed_outcome"])

    def test_replay_detects_a_changed_outcome(self):
        # Store a decision whose recorded outcome disagrees with what the current rules produce.
        replayable = self.client.post(
            "/api/v1/decisions/{0}/replay".format(self._make_decision("approve", {"loan_bureau_score": 720})["id"]),
            headers=self.headers,
        ).json()["replayed_outcome"]
        forced = "reject" if replayable != "reject" else "approve"
        d = self._make_decision(forced, {"loan_bureau_score": 720})
        body = self.client.post("/api/v1/decisions/{0}/replay".format(d["id"]), headers=self.headers).json()
        self.assertTrue(body["changed"], "outcome differing from the recorded one must be flagged changed")

    def test_replay_against_specific_bundle_version(self):
        versions = self.client.get("/api/v1/bundles/versions", headers=self.headers).json()
        self.assertTrue(versions)
        v = versions[0]["version"]
        d = self._make_decision("approve", {"loan_bureau_score": 720})
        r = self.client.post("/api/v1/decisions/{0}/replay?bundleVersion={1}".format(d["id"], v), headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["bundle_version"], v)

    def test_unknown_decision_and_bundle_are_404(self):
        self.assertEqual(self.client.post("/api/v1/decisions/nope/replay", headers=self.headers).status_code, 404)
        d = self._make_decision("approve", {"loan_bureau_score": 720})
        self.assertEqual(
            self.client.post("/api/v1/decisions/{0}/replay?bundleVersion=999999".format(d["id"]), headers=self.headers).status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()
