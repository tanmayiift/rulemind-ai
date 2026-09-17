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
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")  # tests use the sample lending inventory

from app.models import SchedulerLease  # noqa: E402
from app.storage import Storage  # noqa: E402


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
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
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.storage = Storage(path=os.path.join(self.tempdir.name, "gate.db"))
        import app.scheduler as sched
        import app.mailer as mailer
        self.sched = sched
        self.mailer = mailer

        # Without SMTP, every delivery here returns transport="unconfigured" -- indistinguishable
        # from the job never running, which is exactly what these tests exist to tell apart.
        # Configure the tenant and inject a transport so a send is observable.
        self.storage.set_email_config({"host": "smtp.example.com", "port": 587,
                                       "from_addr": "reports@example.com"},
                                      tenant_id=self.storage.default_tenant_id)
        self.sent_to = []
        outer = self

        class _CapturingSMTP:
            def send_message(self, msg):
                outer.sent_to.extend(a.strip() for a in msg["To"].split(","))

            def quit(self):
                pass

        self._orig_factory = mailer._SMTP_FACTORY
        mailer._SMTP_FACTORY = lambda _cfg: _CapturingSMTP()

    def tearDown(self):
        self.mailer._SMTP_FACTORY = self._orig_factory
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
        # This used to be assertIn(transport, {"unconfigured", "smtp"}) -- which passes when
        # the report was NOT sent, since "unconfigured" is precisely the not-sent outcome.
        # The test is here to prove leadership lets the job RUN, so it has to observe a send.
        self.assertEqual(out.get("transport"), "smtp")
        self.assertTrue(out.get("delivered"))
        self.assertEqual(self.sent_to, ["a@b.com"])

    def test_manual_delivery_is_never_gated(self):
        # deliver_scheduled_report (used by manual paths) runs regardless of leadership.
        self.sched._IS_LEADER = False
        tenant = self.storage.default_tenant_id
        self.storage.create_report({"id": "r4", "name": "R4", "columns": [{"key": "id", "path": "id"}],
                                    "filters": {}, "timezone": "UTC",
                                    "schedule": {"enabled": True, "cron": "0 9 * * *", "recipients": ["a@b.com"]}},
                                   tenant_id=tenant)
        out = asyncio.run(self.sched.deliver_scheduled_report(self.storage, "r4", tenant))
        # Same as above: "unconfigured" would satisfy the old assertion while proving the
        # opposite of this test's name. A manual delivery under _IS_LEADER=False must send.
        self.assertEqual(out.get("transport"), "smtp")
        self.assertTrue(out.get("delivered"))
        self.assertEqual(self.sent_to, ["a@b.com"])

    def test_saml_assertion_prune_is_leader_gated_and_actually_prunes(self):
        """`storage.prune_saml_assertions` existed, had a test, and nothing ever called it.

        The table takes one row per successful SSO login and is only ever read to reject a
        replay, so on an SSO deployment it grew forever -- and that lookup is on the hot login
        path, so the cost landed on authentication rather than as a slow query somewhere
        ignorable.
        """
        tenant = self.storage.default_tenant_id
        stale = datetime.utcnow() - timedelta(hours=1)
        fresh = datetime.utcnow() + timedelta(hours=1)
        self.assertTrue(self.storage.claim_saml_assertion("expired-assertion", stale, tenant_id=tenant))
        self.assertTrue(self.storage.claim_saml_assertion("live-assertion", fresh, tenant_id=tenant))

        self.sched._IS_LEADER = False
        self.assertEqual(asyncio.run(self.sched._scheduled_prune_saml_assertions(self.storage)),
                         {"skipped": "not_leader"})

        self.sched._IS_LEADER = True
        out = asyncio.run(self.sched._scheduled_prune_saml_assertions(self.storage))
        self.assertEqual(out["pruned"], 1, "the expired assertion should have been dropped")
        # The live one must survive: pruning it would let a still-valid assertion be replayed.
        # claim_ returns False when the id is already recorded, so False here means still there.
        self.assertFalse(self.storage.claim_saml_assertion("live-assertion", fresh, tenant_id=tenant),
                         "a still-valid assertion was pruned and can now be replayed")
        # ...and the expired one is genuinely gone, so the row can be re-claimed.
        self.assertTrue(self.storage.claim_saml_assertion("expired-assertion", stale, tenant_id=tenant))

    def test_delete_report_is_scoped_and_safe(self):
        tenant = self.storage.default_tenant_id
        self.storage.create_report({"id": "r3", "name": "R3", "columns": [], "filters": {}, "timezone": "UTC"}, tenant_id=tenant)
        self.assertTrue(self.storage.delete_report("r3", tenant_id=tenant))
        self.assertFalse(self.storage.delete_report("r3", tenant_id=tenant))


if __name__ == "__main__":
    unittest.main()
