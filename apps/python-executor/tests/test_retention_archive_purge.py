"""Retention end to end (parity P16): old decisions archive, then purge; recent ones stay.

`tests/test_retention_months.py` covers the CUTOFF MATH thoroughly -- month arithmetic, end-of-
month clamping, months winning over days -- and stops there. Nothing exercised
`archive_and_purge_decisions`, which is the half that actually DELETES a workspace's decisions.
That is the most destructive operation in the product, it runs unattended on a scheduler, and its
only coverage was of the number it is handed.

The ordering guarantee is the reason this file exists: archive FIRST, purge only on success. A
sink that fails must leave every row in the hot database, because the alternative is decisions
that exist in neither place. `test_a_failing_sink_purges_nothing` is the one to keep working.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from app import scheduler  # noqa: E402
from app.archiver import MemoryArchiver  # noqa: E402
from app.models import Decision  # noqa: E402
from app.storage import Storage  # noqa: E402


class _ExplodingArchiver(MemoryArchiver):
    name = "exploding"

    def write(self, decisions):
        raise RuntimeError("the sink is down")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.storage = Storage(path=os.path.join(self.tempdir.name, "retain.db"))
        self.tenant = self.storage.default_tenant_id
        self.archiver = MemoryArchiver()

    def tearDown(self):
        try:
            self.storage.engine.dispose()
        except Exception:  # pragma: no cover
            pass
        self.tempdir.cleanup()

    def seed(self, count, days_ago, tenant=None, prefix="d"):
        """Decisions with a controlled created_at. Written through the session because
        add_decision stamps `now`, and the whole subject here is age."""
        tenant = tenant or self.tenant
        when = datetime.utcnow() - timedelta(days=days_ago)
        with self.storage.connect() as session:
            for index in range(count):
                session.add(Decision(
                    id="{0}-{1}-{2}".format(prefix, days_ago, index),
                    tenant_id=tenant, policy_id="pol", outcome="approve",
                    payload_hash="h{0}".format(index), created_at=when))

    def remaining(self, tenant=None):
        tenant = tenant or self.tenant
        with self.storage.connect() as session:
            return session.query(Decision).filter(Decision.tenant_id == tenant).count()


class TheRoundTripTest(_Base):

    def test_old_decisions_are_archived_and_then_gone(self):
        self.seed(5, days_ago=400)
        self.seed(3, days_ago=1)
        moved = self.storage.archive_and_purge_decisions(
            self.tenant, datetime.utcnow() - timedelta(days=30), self.archiver)
        self.assertEqual(moved, 5)
        self.assertEqual(len(self.archiver.written), 5, "the sink did not receive them")
        self.assertEqual(self.remaining(), 3, "recent decisions were purged too")

    def test_the_archived_records_carry_their_tenant(self):
        """A cold store holding rows with no workspace on them cannot answer a subject-access
        request, which is usually why the data was kept at all."""
        self.seed(2, days_ago=400)
        self.storage.archive_and_purge_decisions(
            self.tenant, datetime.utcnow() - timedelta(days=30), self.archiver)
        self.assertTrue(all(r.get("tenant_id") == self.tenant for r in self.archiver.written))

    def test_nothing_older_than_the_cutoff_survives(self):
        self.seed(4, days_ago=90)
        self.seed(4, days_ago=45)
        self.seed(4, days_ago=5)
        self.storage.archive_and_purge_decisions(
            self.tenant, datetime.utcnow() - timedelta(days=30), self.archiver)
        self.assertEqual(self.remaining(), 4)
        self.assertEqual(len(self.archiver.written), 8)

    def test_a_decision_exactly_at_the_cutoff_is_retained(self):
        """`created_at < older_than` is a strict comparison. Retaining the boundary row errs
        toward keeping data the workspace was promised, which is the safe direction."""
        cutoff = datetime.utcnow() - timedelta(days=30)
        with self.storage.connect() as session:
            session.add(Decision(id="boundary", tenant_id=self.tenant, policy_id="pol",
                                 outcome="approve", payload_hash="hb", created_at=cutoff))
        self.storage.archive_and_purge_decisions(self.tenant, cutoff, self.archiver)
        self.assertEqual(self.remaining(), 1)
        self.assertEqual(self.archiver.written, [])

    def test_it_pages_through_more_rows_than_one_batch(self):
        """The loop, not just the first batch: a workspace hitting retention for the first time
        has far more than `batch_size` rows waiting."""
        self.seed(25, days_ago=400)
        moved = self.storage.archive_and_purge_decisions(
            self.tenant, datetime.utcnow() - timedelta(days=30), self.archiver, batch_size=10)
        self.assertEqual(moved, 25)
        self.assertEqual(self.remaining(), 0)
        self.assertGreaterEqual(self.archiver.batches, 3, "it did not page")


class TheOrderingGuaranteeTest(_Base):
    """Archive first, purge only on success. The alternative loses decisions permanently."""

    def test_a_failing_sink_purges_nothing(self):
        self.seed(6, days_ago=400)
        with self.assertRaises(RuntimeError):
            self.storage.archive_and_purge_decisions(
                self.tenant, datetime.utcnow() - timedelta(days=30), _ExplodingArchiver())
        self.assertEqual(self.remaining(), 6,
                         "decisions were deleted despite the archive write failing -- they now "
                         "exist in neither the hot database nor the sink")

    def test_a_sink_failing_midway_leaves_the_unwritten_rows_intact(self):
        """Batch 1 lands, batch 2 fails. The rows in batch 1 are safely in the sink and gone;
        everything after must still be in the database for the next run to retry."""
        self.seed(20, days_ago=400)

        class _FailsOnSecondBatch(MemoryArchiver):
            name = "flaky"

            def write(self_inner, decisions):
                if self_inner.batches >= 1:
                    raise RuntimeError("sink died")
                super().write(decisions)

        flaky = _FailsOnSecondBatch()
        with self.assertRaises(RuntimeError):
            self.storage.archive_and_purge_decisions(
                self.tenant, datetime.utcnow() - timedelta(days=30), flaky, batch_size=10)
        self.assertEqual(self.remaining(), 10, "the un-archived batch was purged anyway")
        self.assertEqual(len(flaky.written), 10)


class TenantScopingTest(_Base):

    def test_one_workspaces_retention_never_touches_anothers(self):
        """The purge is tenant-scoped. Without that predicate, one workspace's short retention
        would delete every other workspace's decisions -- unattended, on a timer."""
        other = self.storage.create_tenant("Other Co")["id"]
        self.seed(5, days_ago=400)
        self.seed(5, days_ago=400, tenant=other, prefix="o")
        self.storage.archive_and_purge_decisions(
            self.tenant, datetime.utcnow() - timedelta(days=30), self.archiver)
        self.assertEqual(self.remaining(), 0)
        self.assertEqual(self.remaining(other), 5, "another workspace's decisions were purged")
        self.assertTrue(all(r["tenant_id"] == self.tenant for r in self.archiver.written))


class TheScheduledJobTest(_Base):
    """The job that actually runs this unattended."""

    def setUp(self):
        super().setUp()
        self._leader = scheduler._IS_LEADER
        scheduler._IS_LEADER = True

    def tearDown(self):
        scheduler._IS_LEADER = self._leader
        super().tearDown()

    def run_job(self):
        with mock.patch("app.archiver.archiving_enabled", return_value=True):
            with mock.patch("app.archiver.get_archiver", return_value=self.archiver):
                return scheduler.archive_old_decisions(self.storage)

    def test_it_is_a_no_op_when_no_sink_is_configured(self):
        """Purging without a sink would be deletion, not archiving."""
        self.seed(5, days_ago=400)
        with mock.patch("app.archiver.archiving_enabled", return_value=False):
            result = scheduler.archive_old_decisions(self.storage)
        # The KEY, not the whole dict: the job also reports how many workspaces have a
        # retention window configured while nothing enforces it, and pinning the exact object
        # made adding that diagnostic a test failure. What this test is about is that nothing
        # was purged.
        self.assertEqual(result.get("skipped"), "archiving_disabled")
        self.assertEqual(self.remaining(), 5)

    def test_retention_disabled_for_a_workspace_purges_nothing(self):
        """Disabling is `audit_retention_days: 0`, NOT None.

        `update_settings` ignores None (PATCH semantics: absent means unchanged), so writing None
        leaves the default 90 days in place and everything older is purged. Worth a test rather
        than a comment, because "set it to null to turn it off" is the natural guess and it
        silently does the opposite of what the operator intended.
        """
        self.seed(5, days_ago=400)
        self.storage.update_settings({"audit_retention_days": 0}, tenant_id=self.tenant)
        self.run_job()
        self.assertEqual(self.remaining(), 5)

    def test_writing_null_does_not_disable_retention(self):
        """The trap above, pinned from the other side."""
        from app.archiver import retention_cutoff

        self.storage.update_settings({"audit_retention_days": None}, tenant_id=self.tenant)
        settings = self.storage.get_settings(tenant_id=self.tenant)
        self.assertEqual(settings.get("audit_retention_days"), 90,
                         "None cleared the setting; the disable path is now 0 OR None and this "
                         "test should be updated to say so")
        self.assertIsNotNone(retention_cutoff(settings))

    def test_a_configured_retention_archives_and_purges(self):
        self.seed(5, days_ago=400)
        self.seed(2, days_ago=1)
        self.storage.update_settings({"audit_retention_months": 6}, tenant_id=self.tenant)
        result = self.run_job()
        self.assertEqual(result["archived"], 5, result)
        self.assertEqual(self.remaining(), 2)

    def test_a_follower_replica_does_nothing(self):
        """The leader gate is on the SCHEDULED wrapper, not on archive_old_decisions itself --
        so this drives the wrapper. Calling the sync function directly (as a first version of
        this test did) bypasses the gate entirely and proves nothing about the job."""
        import asyncio

        self.seed(5, days_ago=400)
        self.storage.update_settings({"audit_retention_months": 6}, tenant_id=self.tenant)
        scheduler._IS_LEADER = False
        with mock.patch("app.archiver.archiving_enabled", return_value=True):
            with mock.patch("app.archiver.get_archiver", return_value=self.archiver):
                result = asyncio.run(scheduler._scheduled_archive_decisions(self.storage))
        self.assertEqual(result, {"skipped": "not_leader"})
        self.assertEqual(self.remaining(), 5)


if __name__ == "__main__":
    unittest.main()
