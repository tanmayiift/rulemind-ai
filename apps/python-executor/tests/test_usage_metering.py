"""Usage metering and quota (Track B4).

Before this there was no way to bill a workspace: decision volume was not counted, and the
only usage figures that existed (AI tokens) were lifetime totals in Tenant.config with no time
dimension -- so "what did this customer use in August" had no answer at all.

The tests worth having here are the ones where being wrong costs real money or real trust:

  * a device replaying an unacked batch must not be billed twice (over-billing a customer for
    their own bad network);
  * concurrent increments must not clobber each other (silent UNDER-billing, the failure that
    loses revenue without ever erroring);
  * month boundaries must be exact, including across a year rollover;
  * enforcement must stay OFF unless explicitly enabled (an upgrade must not start refusing
    traffic against a limit nobody chose);
  * simulation endpoints must not consume quota, because they are not billed.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.pop("AUTH_MODE", None)

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app import quota  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import preserve_storage_global  # noqa: E402


class QuotaPolicyTest(unittest.TestCase):
    """quota.py is pure -- no I/O -- so the arithmetic is tested directly."""

    def tearDown(self):
        for key in list(os.environ):
            if key.startswith("QUOTA_"):
                del os.environ[key]

    def test_period_is_the_calendar_month(self):
        self.assertEqual(quota.period_bounds(date(2026, 9, 3)), (date(2026, 9, 1), date(2026, 10, 1)))

    def test_period_handles_the_year_rollover(self):
        """December must roll to the next YEAR, not to month 13."""
        self.assertEqual(quota.period_bounds(date(2026, 12, 31)), (date(2026, 12, 1), date(2027, 1, 1)))

    def test_resolution_order_tenant_beats_plan_beats_env_beats_builtin(self):
        self.assertEqual(quota.monthly_limit({"plan": "free"}), 10_000)
        os.environ["QUOTA_DECISIONS_DEFAULT"] = "777"
        self.assertEqual(quota.monthly_limit({"plan": "unknown-plan"}), 777)
        os.environ["QUOTA_DECISIONS_FREE"] = "42"
        self.assertEqual(quota.monthly_limit({"plan": "free"}), 42)
        self.assertEqual(
            quota.monthly_limit({"plan": "free", "config": {"quota": {"decisions_per_month": 5}}}), 5,
            "a per-tenant override is the most specific and must win",
        )

    def test_a_malformed_override_falls_through_rather_than_failing(self):
        """A typo in one workspace's config must not take that workspace offline."""
        self.assertEqual(
            quota.monthly_limit({"plan": "free", "config": {"quota": {"decisions_per_month": "lots"}}}),
            10_000,
        )

    def test_zero_means_unlimited(self):
        status = quota.evaluate({"plan": "enterprise"}, used=10 ** 9)
        self.assertTrue(status["unlimited"])
        self.assertFalse(status["exceeded"])
        self.assertIsNone(status["remaining"])

    def test_exceeded_is_inclusive_at_the_limit(self):
        """Used == limit is exhausted, not "one more allowed" -- an off-by-one here bills a
        customer for a decision they were told they could not have."""
        tenant = {"plan": "free", "config": {"quota": {"decisions_per_month": 10}}}
        self.assertFalse(quota.evaluate(tenant, used=9)["exceeded"])
        self.assertTrue(quota.evaluate(tenant, used=10)["exceeded"])

    def test_enforcement_is_off_by_default(self):
        self.assertFalse(quota.enforcement_enabled(env={}))
        self.assertTrue(quota.enforcement_enabled(env={"QUOTA_ENFORCE": "1"}))
        self.assertFalse(quota.enforcement_enabled(env={"QUOTA_ENFORCE": "0"}))


class UsageMeteringTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.storage = Storage(path=os.path.join(self.tempdir.name, "usage.db"))
        self.tenant = self.storage.default_tenant_id

    def tearDown(self):
        self.tempdir.cleanup()

    def _decisions(self):
        return self.storage.get_usage_total(quota.METRIC_DECISIONS, tenant_id=self.tenant)

    def test_a_server_decision_is_billed_once(self):
        self.storage.add_decision({"policy_id": "p", "outcome": "approve", "payload": {}}, tenant_id=self.tenant)
        self.assertEqual(self._decisions(), 1)

    def test_a_replayed_device_batch_is_not_billed_twice(self):
        """The one that would over-bill. A device that never received an ack re-sends the same
        batch; those rows are deduped on (tenant_id, client_id), so billing must follow
        `inserted`, not `received`."""
        records = [{"id": f"dev-{i}", "policy_id": "p", "outcome": "approve", "payload": {}} for i in range(3)]
        first = self.storage.add_decisions_batch(records, tenant_id=self.tenant)
        self.assertEqual(first["inserted"], 3)
        self.assertEqual(self._decisions(), 3)

        replay = self.storage.add_decisions_batch(records, tenant_id=self.tenant)
        self.assertEqual(replay["duplicates"], 3)
        self.assertEqual(replay["inserted"], 0)
        self.assertEqual(self._decisions(), 3, "a retry must cost the customer nothing")

    def test_concurrent_increments_do_not_clobber(self):
        """The one that would under-bill. record_usage uses an atomic UPDATE rather than a
        read-modify-write; with the latter, parallel writers overwrite each other's counts and
        revenue is lost silently."""
        threads = [threading.Thread(target=lambda: self.storage.record_usage("decisions", 1, tenant_id=self.tenant))
                   for _ in range(25)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self._decisions(), 25)

    def test_buckets_are_per_day_and_summed_over_a_period(self):
        today = date.today()
        for offset, count in ((0, 5), (1, 7), (40, 999)):
            self.storage.record_usage("decisions", count, tenant_id=self.tenant, day=today - timedelta(days=offset))
        window_start = today - timedelta(days=2)
        self.assertEqual(
            self.storage.get_usage_total("decisions", tenant_id=self.tenant,
                                         start=window_start, end=today + timedelta(days=1)),
            12,
            "the 40-day-old bucket is outside the window and must not be billed to it",
        )

    def test_the_period_end_is_exclusive(self):
        """get_usage_daily/total filter `day < end`, matching quota.period_bounds. If end were
        inclusive, the first day of the next month would be billed to both months."""
        today = date.today()
        self.storage.record_usage("decisions", 3, tenant_id=self.tenant, day=today)
        self.assertEqual(self.storage.get_usage_total("decisions", tenant_id=self.tenant,
                                                      start=today, end=today), 0)
        self.assertEqual(self.storage.get_usage_total("decisions", tenant_id=self.tenant,
                                                      start=today, end=today + timedelta(days=1)), 3)

    def test_metrics_are_counted_separately(self):
        self.storage.record_usage("decisions", 2, tenant_id=self.tenant)
        self.storage.record_usage("ai_calls", 9, tenant_id=self.tenant)
        self.assertEqual(self._decisions(), 2)
        self.assertEqual(self.storage.get_usage_total("ai_calls", tenant_id=self.tenant), 9)

    def test_usage_is_per_tenant(self):
        other = self.storage.create_tenant("Other Co")["id"]
        self.storage.record_usage("decisions", 4, tenant_id=self.tenant)
        self.storage.record_usage("decisions", 100, tenant_id=other)
        self.assertEqual(self._decisions(), 4)
        self.assertEqual(self.storage.get_usage_total("decisions", tenant_id=other), 100)

    def test_metering_never_raises(self):
        """Best-effort by contract: a broken meter must not fail a decision the customer has
        already been served."""
        self.storage.record_usage("decisions", 0, tenant_id=self.tenant)   # no-op
        self.storage.record_usage("decisions", -5, tenant_id=self.tenant)  # nonsense, ignored
        self.assertEqual(self._decisions(), 0)


class QuotaEnforcementTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        preserve_storage_global(self)
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "enforce.db"))
        self.client = TestClient(app_main.app)
        self.tenant = app_main.storage.default_tenant_id
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}
        app_main.storage.update_tenant(self.tenant, {"config": {"quota": {"decisions_per_month": 1}}})

    def tearDown(self):
        os.environ.pop("QUOTA_ENFORCE", None)
        self.client.close()
        self.tempdir.cleanup()

    def _exhaust(self):
        app_main.storage.record_usage(quota.METRIC_DECISIONS, 50, tenant_id=self.tenant)

    def test_usage_endpoint_reports_the_quota_position(self):
        app_main.storage.record_usage(quota.METRIC_DECISIONS, 1, tenant_id=self.tenant)
        body = self.client.get("/api/v1/usage", headers=self.headers).json()
        self.assertEqual(body["quota"]["limit"], 1)
        self.assertEqual(body["quota"]["used"], 1)
        self.assertTrue(body["quota"]["exceeded"])
        self.assertFalse(body["quota"]["enforced"], "reporting exceeded must not imply rejecting")
        self.assertEqual(len(body["daily"]), 1)

    def test_usage_endpoint_requires_auth(self):
        self.assertEqual(self.client.get("/api/v1/usage").status_code, 401)

    def test_over_quota_is_served_when_enforcement_is_off(self):
        """The default. An operator upgrading to this version must not suddenly start
        refusing traffic against a limit they never set."""
        self._exhaust()
        res = self.client.post("/api/v1/decide",
                               headers=self.headers,
                               json={"policy_id": "does-not-exist", "payload": {}})
        self.assertNotEqual(res.status_code, 402)

    def test_over_quota_is_402_when_enforcement_is_on(self):
        """402, not 429: no amount of backing off restores the allowance before the period
        rolls over, so a retry-triggering status would be both useless and misleading."""
        os.environ["QUOTA_ENFORCE"] = "1"
        self._exhaust()
        res = self.client.post("/api/v1/decide",
                               headers=self.headers,
                               json={"policy_id": "does-not-exist", "payload": {}})
        self.assertEqual(res.status_code, 402)
        detail = res.json()["detail"]
        self.assertEqual(detail["error"], "quota_exceeded")
        self.assertEqual(detail["limit"], 1)
        self.assertIn("period_start", detail)

    def test_the_gate_precedes_policy_lookup(self):
        """Asserted via a policy id that does not exist: without enforcement that is a 404,
        with it a 402. If the gate ran after the lookup, an over-quota workspace would still
        pay the cost of every request and would see the wrong error."""
        os.environ["QUOTA_ENFORCE"] = "1"
        self._exhaust()
        self.assertEqual(
            self.client.post("/api/v1/decide", headers=self.headers,
                             json={"policy_id": "does-not-exist", "payload": {}}).status_code, 402)

    def test_simulation_is_not_gated(self):
        """/decide/batch logs no Decision row, so it is not billable and must stay available --
        charging quota against what-if runs a customer is not billed for would be indefensible."""
        os.environ["QUOTA_ENFORCE"] = "1"
        self._exhaust()
        res = self.client.post("/api/v1/decide/batch", headers=self.headers,
                               json={"targetId": "does-not-exist", "cases": [{"payload": {}}]})
        self.assertNotEqual(res.status_code, 402)


if __name__ == "__main__":
    unittest.main()
