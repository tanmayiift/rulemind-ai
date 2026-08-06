"""Policy backtesting — replay a sample of a policy's recorded decisions through a compiled
bundle and report the aggregate outcome impact (change rate + transition matrix)."""
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
from app import backtest as backtest_mod  # noqa: E402


class _FakeStorage:
    """Minimal Storage stand-in for backtest_policy — a fixed bundle + scripted decisions."""

    def __init__(self, decisions, bundle_content=None):
        self._decisions = decisions
        self._bundle = {"version": 7, "content": bundle_content or {"policies": {}}}

    def latest_bundle(self, tenant_id=None):
        return self._bundle

    def get_bundle(self, version, tenant_id=None):
        return self._bundle if version == self._bundle["version"] else None

    def sample_policy_decisions(self, policy_id, tenant_id=None, limit=200):
        return [d for d in self._decisions if d["policy_id"] == policy_id][:limit]


def _decision(policy_id, outcome):
    return {"id": "d-" + uuid.uuid4().hex, "policy_id": policy_id, "outcome": outcome,
            "payload": {"amount": 100}, "computed_variables": {"score": 700}, "created_at": "2026-08-06T00:00:00Z"}


class BacktestUnitTests(unittest.TestCase):
    def test_flips_are_counted(self):
        # Patch the engine so every replay returns "reject" — decisions recorded as "approve"
        # must all show as changed, with an approve→reject transition.
        rows = [_decision("p1", "approve") for _ in range(5)]
        import app.core.engine as engine
        saved = engine.decide
        engine.decide = lambda content, payload, ctx: {"outcome": "reject"}
        try:
            report = backtest_mod.backtest_policy(_FakeStorage(rows), "p1")
        finally:
            engine.decide = saved
        self.assertEqual(report["sample"], 5)
        self.assertEqual(report["changed"], 5)
        self.assertEqual(report["change_rate_pct"], 100.0)
        matrix = {(t["from"], t["to"]): t["count"] for t in report["transition_matrix"]}
        self.assertEqual(matrix[("approve", "reject")], 5)

    def test_no_change_when_outcome_matches(self):
        rows = [_decision("p1", "approve") for _ in range(3)]
        import app.core.engine as engine
        saved = engine.decide
        engine.decide = lambda content, payload, ctx: {"outcome": "approve"}
        try:
            report = backtest_mod.backtest_policy(_FakeStorage(rows), "p1")
        finally:
            engine.decide = saved
        self.assertEqual(report["changed"], 0)
        self.assertEqual(report["change_rate_pct"], 0.0)

    def test_engine_error_counted_not_raised(self):
        rows = [_decision("p1", "approve")]
        import app.core.engine as engine
        saved = engine.decide

        def boom(content, payload, ctx):
            raise RuntimeError("bad candidate")

        engine.decide = boom
        try:
            report = backtest_mod.backtest_policy(_FakeStorage(rows), "p1")
        finally:
            engine.decide = saved
        self.assertEqual(report["errors"], 1)
        self.assertEqual(report["changed"], 1)  # approve -> error is a change

    def test_missing_bundle_version_raises(self):
        with self.assertRaises(ValueError):
            backtest_mod.backtest_policy(_FakeStorage([]), "p1", bundle_version=999)


class BacktestEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def test_unknown_policy_404(self):
        r = self.client.post("/api/v1/policies/does-not-exist/backtest", headers=self.headers)
        self.assertEqual(r.status_code, 404)

    def test_backtest_seeded_policy(self):
        # Use whatever seeded policy exists; endpoint should return a well-formed summary.
        policies = self.client.get("/api/v1/policies", headers=self.headers).json()
        if not policies:
            self.skipTest("no seeded policy")
        policy_id = policies[0]["id"]
        r = self.client.post("/api/v1/policies/" + policy_id + "/backtest?sample=50", headers=self.headers)
        # Either a bundle exists (200 + shape) or none is compiled yet (404) — both are valid.
        if r.status_code == 404:
            self.skipTest("no compiled bundle for seeded tenant")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        for key in ("sample", "changed", "change_rate_pct", "transition_matrix", "original_distribution"):
            self.assertIn(key, body)


if __name__ == "__main__":
    unittest.main()
