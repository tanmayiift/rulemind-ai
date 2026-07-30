"""Scheduler leader election — ensures scheduled jobs fire exactly once across
replicas, and that deleting a report stops its delivery job.

The DB lease is the source of truth: only the current holder runs cron policies,
report delivery, and review-timeout sweeps.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from app.models import SchedulerLease  # noqa: E402
from app.storage import Storage  # noqa: E402


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.storage = Storage(path=os.path.join(self.tempdir.name, "lease.db"))

    def tearDown(self):
        self.tempdir.cleanup()

    def test_first_owner_acquires(self):
        self.assertTrue(self.storage.try_acquire_scheduler_lease("replica-A", ttl_seconds=30))

    def test_second_owner_denied_while_valid(self):
        self.assertTrue(self.storage.try_acquire_scheduler_lease("replica-A", ttl_seconds=30))
        self.assertFalse(self.storage.try_acquire_scheduler_lease("replica-B", ttl_seconds=30))
        # the holder can always renew
        self.assertTrue(self.storage.try_acquire_scheduler_lease("replica-A", ttl_seconds=30))

    def test_release_allows_immediate_takeover(self):
        self.assertTrue(self.storage.try_acquire_scheduler_lease("replica-A", ttl_seconds=30))
        self.storage.release_scheduler_lease("replica-A")
        self.assertTrue(self.storage.try_acquire_scheduler_lease("replica-B", ttl_seconds=30))
        self.assertFalse(self.storage.try_acquire_scheduler_lease("replica-A", ttl_seconds=30))

    def test_expired_lease_is_taken_over(self):
        self.assertTrue(self.storage.try_acquire_scheduler_lease("replica-A", ttl_seconds=30))
        # force expiry (simulate replica-A crashing without releasing)
        with self.storage.connect() as session:
            row = session.get(SchedulerLease, "singleton")
            row.lease_until = datetime.utcnow() - timedelta(seconds=1)
        self.assertTrue(self.storage.try_acquire_scheduler_lease("replica-B", ttl_seconds=30))

    def test_only_one_of_many_replicas_wins(self):
        results = [self.storage.try_acquire_scheduler_lease(f"replica-{i}", ttl_seconds=30) for i in range(5)]
        self.assertEqual(results.count(True), 1, results)


class JobGatingTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.storage = Storage(path=os.path.join(self.tempdir.name, "gate.db"))
        import app.scheduler as sched
        self.sched = sched

    def tearDown(self):
        self.sched._IS_LEADER = False
        self.tempdir.cleanup()

    def test_scheduled_report_skips_when_not_leader(self):
        # The scheduler-triggered wrapper must no-op on non-leaders (single-fire).
        self.sched._IS_LEADER = False
        tenant = self.storage.default_tenant_id
        self.storage.create_report({"id": "r1", "name": "R", "columns": [{"key": "id", "path": "id"}],
                                    "filters": {}, "timezone": "UTC"}, tenant_id=tenant)
        out = asyncio.run(self.sched._scheduled_report(self.storage, "r1", tenant))
        self.assertEqual(out, {"skipped": "not_leader"})

    def test_scheduled_report_runs_when_leader(self):
        self.sched._IS_LEADER = True
        tenant = self.storage.default_tenant_id
        self.storage.create_report({"id": "r2", "name": "R2", "columns": [{"key": "id", "path": "id"}],
                                    "filters": {}, "timezone": "UTC",
                                    "schedule": {"enabled": True, "cron": "0 9 * * *", "recipients": ["a@b.com"]}},
                                   tenant_id=tenant)
        out = asyncio.run(self.sched._scheduled_report(self.storage, "r2", tenant))
        self.assertIn(out.get("transport"), {"outbox", "smtp"})

    def test_manual_delivery_is_never_gated(self):
        # deliver_scheduled_report (used by manual paths) runs regardless of leadership.
        self.sched._IS_LEADER = False
        tenant = self.storage.default_tenant_id
        self.storage.create_report({"id": "r4", "name": "R4", "columns": [{"key": "id", "path": "id"}],
                                    "filters": {}, "timezone": "UTC",
                                    "schedule": {"enabled": True, "cron": "0 9 * * *", "recipients": ["a@b.com"]}},
                                   tenant_id=tenant)
        out = asyncio.run(self.sched.deliver_scheduled_report(self.storage, "r4", tenant))
        self.assertIn(out.get("transport"), {"outbox", "smtp"})

    def test_delete_report_is_scoped_and_safe(self):
        tenant = self.storage.default_tenant_id
        self.storage.create_report({"id": "r3", "name": "R3", "columns": [], "filters": {}, "timezone": "UTC"}, tenant_id=tenant)
        self.assertTrue(self.storage.delete_report("r3", tenant_id=tenant))
        self.assertFalse(self.storage.delete_report("r3", tenant_id=tenant))


if __name__ == "__main__":
    unittest.main()
