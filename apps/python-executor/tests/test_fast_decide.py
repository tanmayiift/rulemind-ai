"""Cached-bundle fast decide path: correctness parity + servability."""
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

import app.main as app_main  # noqa: E402
from app.fast_decide import invalidate, is_fast_servable  # noqa: E402
from app.storage import Storage  # noqa: E402


class FastDecideTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "fd.db"))
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}
        invalidate()
        # Build a pure-compute policy (rule + outcome) over a seeded rule.
        rules = self.client.get("/api/v1/rules", headers=self.headers).json()
        self.rule_id = rules[0]["id"]
        created = self.client.post(
            "/api/v1/policies",
            json={
                "name": "Fast Path Policy",
                "steps": [
                    {"type": "rule", "ref_id": self.rule_id},
                    {"type": "outcome", "ref_id": "approve"},
                ],
            },
            headers=self.headers,
        )
        self.assertEqual(created.status_code, 200, created.text)
        self.policy_id = created.json()["id"]

    def tearDown(self):
        os.environ.pop("FAST_DECIDE", None)
        self.client.close()
        self.tempdir.cleanup()

    def _decide(self, payload):
        return self.client.post(
            "/api/v1/decide", json={"policy_id": self.policy_id, "payload": payload}, headers=self.headers
        ).json()

    def test_policy_is_fast_servable(self):
        policy = app_main.storage.get_policy(self.policy_id)
        self.assertTrue(is_fast_servable(policy))

    def test_fast_matches_standard_outcome(self):
        payloads = [{"user_id": f"u{i}"} for i in range(8)]
        os.environ.pop("FAST_DECIDE", None)
        standard = [self._decide(p)["outcome"] for p in payloads]
        os.environ["FAST_DECIDE"] = "1"
        invalidate()
        fast = [self._decide(p)["outcome"] for p in payloads]
        self.assertEqual(standard, fast, "fast path diverged from standard path")

    def test_io_policy_falls_back(self):
        # A policy with a review_gate is not fast-servable.
        io_policy = {"id": "io_pol", "steps": [{"type": "review_gate", "config": {}}, {"type": "outcome", "ref_id": "review"}]}
        self.assertFalse(is_fast_servable(io_policy))


if __name__ == "__main__":
    unittest.main()
