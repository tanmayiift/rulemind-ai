"""Sweep: the insights/audit endpoints that no test mentioned.

`app/routers/insights.py` backs /decision-explorer and the audit UI. Six of its twelve routes
were reachable only through the browser: /api/v1/analytics/{decisions,latency,sdk},
/api/v1/audit/{errors,promotions,decisions/count}. Nothing asserted what they answer when the
input is malformed, when the caller is a different tenant, or when a device has pushed a
malformed telemetry payload.

What this file pins down:

* **/api/v1/audit takes limit/offset straight to SQL.** Every sibling read path caps them in
  storage (`max(1, min(limit, 1000))`), but `list_audit_events` applies `limit`/`offset` raw
  because its internal callers need "no limit" to mean *all matching rows*. The HTTP route
  in front of it declares them as bare `int` with no bounds, so `?limit=-1` reaches Postgres
  as `LIMIT -1` -> `InvalidRowCountInLimitClause` -> 500 on the one endpoint a compliance
  auditor uses. On SQLite the same request is worse-but-quieter: `LIMIT -1` means *no limit*
  there, so the paging guard the docstring was added for silently disappears.

* **Tenant isolation on the five routes that never pass a tenant down.** They rely entirely on
  the storage fallback chain (`tenant_id or contextvar or default_tenant_id`), whose last link
  is the FIRST tenant -- the exact fail-open `/api/v1/audit`'s own docstring says it declined to
  depend on. The middleware does set the contextvar today, so these pass; they are here so that
  a future route registered outside the middleware, or a helper called off-request, fails on a
  named test instead of serving one workspace's decision log to another.

* **Analytics trusts device-supplied telemetry shapes.** `/sdk/v1/events` stores whatever JSON a
  device sends (`events: List[Dict[str, Any]]`, no schema). `sdk_analytics` then does
  `payload["data"].get(...)` and `float(data["latencyMs"])` on it. One device posting
  `{"type": "bundle.synced", "data": "x"}` makes /api/v1/analytics/sdk return 500 for that
  workspace *from then on* -- a stored poison pill, not a transient bad request.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.pop("AUTH_MODE", None)

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import preserve_storage_global  # noqa: E402


class _TwoTenantCase(unittest.TestCase):
    """Tenant A (the default, seeded) plus a second tenant B with its own owner key."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        preserve_storage_global(self)
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "insights.db"))
        self.client = TestClient(app_main.app)
        self.tenant_a = app_main.storage.default_tenant_id
        self.headers_a = {"x-api-key": app_main.storage.default_api_key or ""}
        self.tenant_b = app_main.storage.create_tenant("Sweep Insights Co")["id"]
        self.headers_b = {
            "x-api-key": app_main.storage.generate_api_key_for_tenant(self.tenant_b)["plaintext"]
        }

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def _decision(self, tenant_id: str, outcome: str = "reject", policy_id: str = "p-sweep"):
        return app_main.storage.add_decision(
            {
                "policy_id": policy_id,
                "outcome": outcome,
                "latency_ms": 11,
                "payload": {"who": tenant_id},
                "source": "api",
            },
            tenant_id=tenant_id,
        )


class AuditPagingBoundsTest(_TwoTenantCase):
    """GET /api/v1/audit -- the compliance read path -- validates its own paging."""

    def test_negative_limit_is_rejected_not_a_500(self):
        """`?limit=-1` reaches Postgres as `LIMIT -1`: InvalidRowCountInLimitClause, 500.

        A 500 on the authoring audit trail is the worst place for one: it is the endpoint an
        auditor's tooling drives, and a 500 reads as "the service is broken", not "your query
        was". Every sibling (audit/decisions, audit/errors) clamps in storage and answers 200.
        """
        res = self.client.get("/api/v1/audit", params={"limit": -1}, headers=self.headers_a)
        self.assertIn(res.status_code, (400, 422),
                      "limit=-1 answered {0}, not a client error".format(res.status_code))

    def test_negative_offset_is_rejected_not_a_500(self):
        res = self.client.get("/api/v1/audit", params={"offset": -5}, headers=self.headers_a)
        self.assertIn(res.status_code, (400, 422),
                      "offset=-5 answered {0}, not a client error".format(res.status_code))

    def test_limit_is_capped_so_the_page_stays_bounded(self):
        """The paging arguments exist because the table is append-only and unbounded. A limit
        the caller picks with no ceiling is the unpaged fetch the docstring set out to prevent
        -- and on SQLite a negative one disables the LIMIT entirely rather than erroring, so
        the guard is gone with no signal at all."""
        res = self.client.get("/api/v1/audit", params={"limit": 10 ** 9}, headers=self.headers_a)
        self.assertIn(res.status_code, (400, 422),
                      "an arbitrarily large limit was accepted ({0})".format(res.status_code))

    def test_a_sane_page_still_works(self):
        """Regression guard: bounding the inputs must not break the normal read."""
        res = self.client.get("/api/v1/audit", params={"limit": 5, "offset": 0}, headers=self.headers_a)
        self.assertEqual(res.status_code, 200)
        self.assertIsInstance(res.json(), list)


class InsightsTenantIsolationTest(_TwoTenantCase):
    """The five insights routes that pass no tenant_id down must still answer per-tenant."""

    def setUp(self):
        super().setUp()
        for _ in range(3):
            self._decision(self.tenant_a, outcome="reject", policy_id="p-a")
        self._decision(self.tenant_b, outcome="approve", policy_id="p-b")

    def test_audit_decisions_does_not_leak_across_tenants(self):
        rows = self.client.get("/api/v1/audit/decisions", headers=self.headers_b).json()
        self.assertEqual([r["policy_id"] for r in rows], ["p-b"],
                         "tenant B was served tenant A's decision log")

    def test_audit_decisions_count_is_per_tenant(self):
        self.assertEqual(self.client.get("/api/v1/audit/decisions/count", headers=self.headers_a).json(),
                         {"total": 3})
        self.assertEqual(self.client.get("/api/v1/audit/decisions/count", headers=self.headers_b).json(),
                         {"total": 1})

    def test_audit_promotions_is_per_tenant(self):
        app_main.storage.add_promotion("policy", "p-a", "dev", "uat", "alice", "why", tenant_id=self.tenant_a)
        self.assertEqual(len(self.client.get("/api/v1/audit/promotions", headers=self.headers_a).json()), 1)
        self.assertEqual(self.client.get("/api/v1/audit/promotions", headers=self.headers_b).json(), [],
                         "tenant B was served tenant A's promotion ledger")

    def test_audit_errors_is_per_tenant(self):
        app_main.storage.add_error_event(
            {"scope": "variables", "operation": "compute", "message": "boom"}, tenant_id=self.tenant_a)
        self.assertEqual(len(self.client.get("/api/v1/audit/errors", headers=self.headers_a).json()), 1)
        self.assertEqual(self.client.get("/api/v1/audit/errors", headers=self.headers_b).json(), [],
                         "tenant B was served tenant A's error log")

    def test_rejection_drivers_is_per_tenant(self):
        """POST /api/v1/analytics/rejection-drivers takes no Request at all, so it cannot even
        reach `active_tenant_id` -- it is the route most exposed to the fallback chain."""
        body = self.client.post("/api/v1/analytics/rejection-drivers",
                                headers=self.headers_b, json={"limit": 100}).json()
        self.assertEqual(body["analyzed"], 1,
                         "tenant B's rejection drivers were computed over {0} decisions".format(
                             body["analyzed"]))

    def test_decision_analytics_is_per_tenant(self):
        summary = self.client.get("/api/v1/analytics/decisions", headers=self.headers_b).json()["summary"]
        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["approved"], 1)

    def test_a_decision_id_from_another_tenant_is_404_not_a_read(self):
        theirs = self._decision(self.tenant_a, policy_id="p-secret")
        res = self.client.get("/api/v1/audit/decisions/{0}".format(theirs["id"]), headers=self.headers_b)
        self.assertEqual(res.status_code, 404,
                         "tenant B read tenant A's decision by id")


class AnalyticsShapeTest(_TwoTenantCase):
    """Analytics must survive the telemetry a device actually sends."""

    def test_a_malformed_sdk_event_does_not_poison_analytics_sdk(self):
        """`/sdk/v1/events` stores unvalidated JSON; sdk_analytics then calls `.get` on it.

        The failure is persistent, not transient: the bad row stays in sdk_events, so every
        later GET /api/v1/analytics/sdk for that workspace is a 500. One misbehaving device
        takes the SDK-health dashboard down for the whole tenant.
        """
        ingest = self.client.post("/sdk/v1/events", headers=self.headers_a,
                                  json={"events": [{"type": "bundle.synced", "data": "not-an-object"}]})
        self.assertEqual(ingest.status_code, 200, "precondition: the event is accepted as-is")
        res = self.client.get("/api/v1/analytics/sdk", headers=self.headers_a)
        self.assertEqual(res.status_code, 200,
                         "a stored SDK event with a non-object `data` 500s the analytics read")

    def test_a_non_numeric_latency_does_not_poison_analytics_sdk(self):
        self.client.post("/sdk/v1/events", headers=self.headers_a,
                         json={"events": [{"type": "bundle.synced", "data": {"latencyMs": "fast"}}]})
        res = self.client.get("/api/v1/analytics/sdk", headers=self.headers_a)
        self.assertEqual(res.status_code, 200,
                         "a stored SDK event with a non-numeric latencyMs 500s the analytics read")

    def test_analytics_sdk_still_reports_a_well_formed_event(self):
        """Regression guard for the two above: hardening must not swallow real telemetry."""
        self.client.post("/sdk/v1/events", headers=self.headers_a, json={"events": [
            {"type": "bundle.synced", "data": {"sdkVersion": "1.2.3", "latencyMs": 40}}]})
        body = self.client.get("/api/v1/analytics/sdk", headers=self.headers_a).json()
        self.assertEqual(body["activeSdkVersions"], [{"version": "1.2.3", "count": 1}])
        self.assertEqual(body["bundleSyncHealth"]["successRate"], 100.0)
        self.assertEqual(body["bundleSyncHealth"]["avgLatencyMs"], 40.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
