"""Per-workspace SLOs + outcome-drift detection.

Covers the pure evaluation (latency / error-rate / approval-rate / drift breaches over a
synthetic decision stream), the config GET/PUT endpoints, and the live status endpoint.
"""
from __future__ import annotations

import os
import sys
import unittest
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

import app.main as app_main  # noqa: E402
from app import slo  # noqa: E402


class _FakeStorage:
    """Minimal Storage stand-in for evaluate_slo — returns a scripted decision_facts set and
    an empty settings/engine_config so config falls back to defaults."""

    def __init__(self, rows):
        self._rows = rows

    def decision_facts(self, tenant_id=None, since=None, **_kw):
        if since is None:
            return list(self._rows)
        return [r for r in self._rows if slo._parse_ts(r.get("created_at")) >= since]

    def get_settings(self, tenant_id=None):
        return {"engine_config": {}}


def _row(outcome, latency, ts):
    return {"outcome": outcome, "latency_ms": latency, "created_at": ts.isoformat() + "Z",
            "source": "api", "policy_id": "p1", "experiment_variant": None}


class SloEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 6, 12, 0, 0)
        self.recent = self.now - timedelta(hours=1)
        self.old = self.now - timedelta(days=3)

    def test_healthy_when_within_objective(self):
        rows = [_row("approve", 20, self.recent) for _ in range(80)]
        report = slo.evaluate_slo(_FakeStorage(rows), config=slo.default_slo_config(), now=self.now)
        self.assertTrue(report["healthy"])
        self.assertEqual(report["breaches"], [])
        self.assertEqual(report["metrics"]["sample"], 80)

    def test_latency_breach(self):
        rows = [_row("approve", 500, self.recent) for _ in range(80)]
        report = slo.evaluate_slo(_FakeStorage(rows), config=slo.default_slo_config(), now=self.now)
        self.assertFalse(report["healthy"])
        self.assertIn("latency_p95", [b["type"] for b in report["breaches"]])

    def test_error_rate_breach(self):
        rows = [_row("approve", 20, self.recent) for _ in range(90)]
        rows += [_row("error", 20, self.recent) for _ in range(10)]  # 10% > 1% default
        report = slo.evaluate_slo(_FakeStorage(rows), config=slo.default_slo_config(), now=self.now)
        self.assertIn("error_rate", [b["type"] for b in report["breaches"]])

    def test_outcome_drift_breach(self):
        # Baseline: mostly approvals. Recent: mostly rejects -> large distribution shift.
        rows = [_row("approve", 20, self.old) for _ in range(100)]
        rows += [_row("reject", 20, self.recent) for _ in range(100)]
        report = slo.evaluate_slo(_FakeStorage(rows), config=slo.default_slo_config(), now=self.now)
        self.assertTrue(report["drift"]["measurable"])
        self.assertGreater(report["drift"]["distance"], 0.2)
        self.assertIn("outcome_drift", [b["type"] for b in report["breaches"]])

    def test_no_drift_alert_below_min_sample(self):
        rows = [_row("approve", 20, self.old) for _ in range(5)]
        rows += [_row("reject", 20, self.recent) for _ in range(5)]
        report = slo.evaluate_slo(_FakeStorage(rows), config=slo.default_slo_config(), now=self.now)
        self.assertFalse(report["drift"]["measurable"])
        self.assertEqual(report["breaches"], [])

    def test_approval_rate_bounds(self):
        cfg = slo.default_slo_config()
        cfg["min_approval_rate_pct"] = 80.0
        rows = [_row("reject", 20, self.recent) for _ in range(80)]
        report = slo.evaluate_slo(_FakeStorage(rows), config=cfg, now=self.now)
        self.assertIn("approval_rate_low", [b["type"] for b in report["breaches"]])

    def test_disabled_never_breaches(self):
        cfg = slo.default_slo_config()
        cfg["enabled"] = False
        rows = [_row("approve", 5000, self.recent) for _ in range(80)]
        report = slo.evaluate_slo(_FakeStorage(rows), config=cfg, now=self.now)
        self.assertTrue(report["healthy"])


class SloEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def test_get_returns_defaults(self):
        body = self.client.get("/api/v1/settings/slo", headers=self.headers).json()
        self.assertIn("latency_p95_ms", body)
        self.assertTrue(body["enabled"])

    def test_put_updates_and_persists(self):
        r = self.client.put("/api/v1/settings/slo", headers=self.headers,
                            json={"latency_p95_ms": 250, "min_approval_rate_pct": 40, "drift_threshold": 0.3})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["latency_p95_ms"], 250)
        self.assertEqual(body["min_approval_rate_pct"], 40)
        again = self.client.get("/api/v1/settings/slo", headers=self.headers).json()
        self.assertEqual(again["latency_p95_ms"], 250)
        self.assertEqual(again["drift_threshold"], 0.3)

    def test_status_endpoint_shape(self):
        body = self.client.get("/api/v1/slo/status", headers=self.headers).json()
        for key in ("healthy", "metrics", "drift", "objective", "breaches", "recent_events"):
            self.assertIn(key, body)


class SloSchedulerSweepTests(unittest.TestCase):
    def test_check_slos_records_breach_event_on_transition(self):
        from app import scheduler
        storage = app_main.storage
        tenant_id = storage.default_tenant_id
        # Tight objective so the seeded demo stream breaches; then run the sweep twice.
        storage.update_settings(
            {"engine_config": {**(storage.get_settings(tenant_id=tenant_id).get("engine_config", {}) or {}),
                               "slo": {"latency_p95_ms": 0.0, "min_sample": 1}}},
            tenant_id=tenant_id,
        )
        # Seed at least one decision so the window is non-empty.
        storage.add_decision({
            "id": "slo-" + uuid.uuid4().hex, "policy_id": "p1", "outcome": "approve",
            "latency_ms": 50, "source": "api", "payload": {}, "computed_variables": {},
        }, tenant_id=tenant_id)
        first = scheduler.check_slos(storage)
        self.assertGreaterEqual(first["checked"], 1)
        events = storage.list_audit_events(tenant_id=tenant_id, event_type="slo_breach")
        self.assertTrue(any("latency_p95" in (e.get("metadata", {}) or {}).get("breach_types", []) for e in events))
        # Second identical sweep must NOT append a duplicate (transition-only).
        before = len(events)
        scheduler.check_slos(storage)
        after = len(storage.list_audit_events(tenant_id=tenant_id, event_type="slo_breach"))
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
