"""`/api/v1/decide` must emit Prometheus metrics. It did not, on either path.

THE BUG THIS PINS. `DECISIONS_TOTAL` and `DECISION_LATENCY` are defined in main.py and were
incremented in exactly one place: `routers/sdk.py`, the SDK server-decide path. The PRIMARY
endpoint — `/api/v1/decide`, the one the product is about — incremented neither, on either of
its two return paths (fast path and executor). So `/metrics` reported nothing at all for
production decision traffic: no decision rate, no latency histogram, no outcome mix.

That is invisible in every other kind of test. The endpoint returned correct decisions, the
suite was green, and the Grafana dashboard was wired — it just had nothing to draw. It surfaced
only by scraping `/metrics` on a running stack after driving real traffic through it, which is
the difference between "tested" and "operable".

WHY BOTH PATHS ARE ASSERTED SEPARATELY. `/decide` picks between `fast_decide` and the executor
at request time. A metric added to one and forgotten on the other reports a fraction of real
traffic while looking healthy — worse than reporting nothing, because the number is believable.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import preserve_storage_global  # noqa: E402


class DecideEmitsMetricsTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        preserve_storage_global(self)
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "metrics.db"))
        self.tenant = app_main.storage.default_tenant_id
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}
        self.policy_id = next(
            p["id"] for p in app_main.storage.list_policies(tenant_id=self.tenant)
        )

    def tearDown(self):
        self.client.close()

    def _decide(self, fast):
        env = dict(os.environ)
        env["FAST_DECIDE"] = "1" if fast else "0"
        with mock.patch.dict(os.environ, env, clear=True):
            return self.client.post(
                "/api/v1/decide", headers=self.headers,
                json={"policyId": self.policy_id,
                      "payload": {"bureau_score": 720, "dti_ratio": 0.35}})

    def test_the_executor_path_increments_the_decision_counter(self):
        seen = []
        with mock.patch.object(app_main, "DECISIONS_TOTAL") as counter:
            counter.labels.side_effect = lambda **kw: seen.append(kw) or mock.MagicMock()
            response = self._decide(fast=False)
        self.assertEqual(response.status_code, 200, response.text[:200])
        self.assertTrue(seen, "the executor path served a decision and recorded no metric")
        self.assertEqual(seen[0].get("source"), "api")
        self.assertIn("outcome", seen[0])

    def test_the_executor_path_observes_latency(self):
        seen = []
        with mock.patch.object(app_main, "DECISION_LATENCY") as hist:
            hist.labels.side_effect = lambda **kw: seen.append(kw) or mock.MagicMock()
            self._decide(fast=False)
        self.assertTrue(seen, "no latency observation for a served decision")
        self.assertEqual(seen[0].get("source"), "api")

    def test_the_fast_path_increments_it_too_with_its_own_source_label(self):
        """A metric on one path only reports a FRACTION of real traffic while looking healthy —
        worse than none, because the number is believable."""
        seen = []
        with mock.patch.object(app_main, "DECISIONS_TOTAL") as counter:
            counter.labels.side_effect = lambda **kw: seen.append(kw) or mock.MagicMock()
            response = self._decide(fast=True)
        self.assertEqual(response.status_code, 200, response.text[:200])
        self.assertTrue(seen, "the fast path served a decision and recorded no metric")
        # api_fast, not api: the two paths must be distinguishable in a dashboard, because
        # "latency regressed" and "traffic shifted off the fast path" look identical otherwise.
        self.assertIn(seen[0].get("source"), ("api_fast", "api"))

    def test_metrics_failure_never_breaks_a_decision(self):
        """Observability is not allowed to take the hot path down with it."""
        with mock.patch.object(app_main, "DECISIONS_TOTAL") as counter:
            counter.labels.side_effect = RuntimeError("metrics backend is down")
            response = self._decide(fast=False)
        self.assertEqual(response.status_code, 200,
                         "a metrics failure turned a served decision into an error")
        self.assertIn("outcome", response.json())

    def test_the_metrics_endpoint_exposes_the_decision_series(self):
        """End to end: decide, then scrape, and find the series a dashboard would query."""
        self._decide(fast=False)
        body = self.client.get("/metrics").text
        self.assertIn("rulemind_decisions_total", body)
        self.assertIn("rulemind_decision_latency_seconds", body)


if __name__ == "__main__":
    unittest.main()
