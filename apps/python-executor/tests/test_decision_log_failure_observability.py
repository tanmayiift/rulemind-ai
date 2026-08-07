"""A failed decision-log write must be OBSERVABLE, not silently swallowed.

Before this fix, if the async Decision write raised (DB down, schema drift, constraint), the
exception was caught and dropped: the decision was returned (200) but never persisted, with no metric
and no error event — decisions could vanish invisibly. Now the failure increments a metric and writes
an error_event.
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
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")

import app.decision_log as decision_log  # noqa: E402
import app.main as app_main  # noqa: E402
from app.fast_decide import _safe_add_decision  # noqa: E402
from app.storage import Storage  # noqa: E402


def _failures_count(source: str) -> float:
    return decision_log.DECISION_LOG_FAILURES.labels(source=source)._value.get()


class DecisionLogFailureObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "obs.db"))
        self.tenant = app_main.storage.default_tenant_id

    def tearDown(self):
        self.tempdir.cleanup()

    def test_failed_write_increments_metric_and_writes_error_event(self):
        before = _failures_count("api_fast")
        errs_before = len(app_main.storage.list_error_events(tenant_id=self.tenant))

        class _BrokenStorage:
            def add_decision(self, *a, **k):
                raise RuntimeError("db is down")
            # error_event still lands in the real store so the drop is inspectable
            def add_error_event(self, payload, tenant_id=None):
                return app_main.storage.add_error_event(payload, tenant_id=tenant_id)

        record = {"id": "d1", "policy_id": "p1", "outcome": "approve", "payload": {}, "source": "api_fast"}
        # Must NOT raise — a logging failure never breaks the decision.
        _safe_add_decision(_BrokenStorage(), record, self.tenant)

        self.assertEqual(_failures_count("api_fast"), before + 1, "failure metric did not increment")
        errs = app_main.storage.list_error_events(tenant_id=self.tenant)
        self.assertEqual(len(errs), errs_before + 1, "no error_event written for the dropped decision")
        self.assertEqual(errs[0]["stage"], "decision_log_write")
        self.assertIn("not persisted", errs[0]["message"].lower())

    def test_recorder_never_raises_even_if_error_event_also_fails(self):
        class _AllBroken:
            def add_error_event(self, *a, **k):
                raise RuntimeError("db really down")
        # Both the metric and the error_event paths are guarded — this must return quietly.
        decision_log.record_write_failure(_AllBroken(), self.tenant, {"source": "api"}, RuntimeError("boom"))


if __name__ == "__main__":
    unittest.main()
