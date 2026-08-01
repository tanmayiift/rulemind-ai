"""Decision-log retention: archive aged decisions to an OLAP sink, then purge the hot DB.

Archive-first / purge-only-on-success (a sink failure never loses data), scoped by tenant and
retention window. Uses the in-memory sink so no ClickHouse/S3 is needed in CI.
"""
from __future__ import annotations

import os
import sys
import unittest
import uuid
from datetime import datetime, timedelta
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

import app.main as app_main  # noqa: E402
from app.archiver import MemoryArchiver, archiving_enabled, get_archiver  # noqa: E402


class _FailingArchiver(MemoryArchiver):
    name = "failing"

    def write(self, decisions):
        raise RuntimeError("sink down")


class DecisionArchiverTests(unittest.TestCase):
    def setUp(self):
        self.storage = app_main.storage
        self.tenant_id = self.storage.default_tenant_id
        self.policy = self.storage.list_policies(status="prod", tenant_id=self.tenant_id)[0]
        self.tag = "arch_" + uuid.uuid4().hex[:8]

    def _seed(self, created_at: datetime, n: int):
        ids = []
        for _ in range(n):
            row = self.storage.add_decision(
                {"policy_id": self.policy["id"], "outcome": "approve", "source": self.tag, "payload_preview": {}},
                tenant_id=self.tenant_id,
            )
            ids.append(row["id"])
        # Backdate created_at directly (add_decision stamps now()).
        from sqlalchemy import update
        from app.models import Decision

        with self.storage.connect() as session:
            session.execute(update(Decision).where(Decision.id.in_(ids)).values(created_at=created_at))
        return ids

    def _count(self, ids):
        from sqlalchemy import select, func
        from app.models import Decision

        with self.storage.connect() as session:
            return int(session.scalar(select(func.count()).select_from(Decision).where(Decision.id.in_(ids))) or 0)

    def test_factory_default_is_none_and_disabled(self):
        self.assertEqual(get_archiver({}).name, "none")
        self.assertFalse(archiving_enabled({}))
        self.assertTrue(archiving_enabled({"DECISION_ARCHIVE_SINK": "s3"}))

    def test_archives_old_and_keeps_recent(self):
        old_ids = self._seed(datetime.utcnow() - timedelta(days=100), 5)
        new_ids = self._seed(datetime.utcnow() - timedelta(days=1), 3)
        archiver = MemoryArchiver()
        cutoff = datetime.utcnow() - timedelta(days=90)
        archived = self.storage.archive_and_purge_decisions(self.tenant_id, cutoff, archiver, batch_size=2)
        self.assertEqual(archived, 5, "only the 5 old decisions are archived")
        self.assertEqual(len(archiver.written), 5)
        self.assertGreater(archiver.batches, 1, "batched (batch_size=2 over 5 rows)")
        self.assertEqual(self._count(old_ids), 0, "old decisions purged from the hot DB")
        self.assertEqual(self._count(new_ids), 3, "recent decisions retained")
        # Archived records carry tenant_id + the generic decision fields (use-case agnostic).
        self.assertEqual(archiver.written[0]["tenant_id"], self.tenant_id)
        self.assertIn("outcome", archiver.written[0])

    def test_purge_only_on_successful_archive(self):
        old_ids = self._seed(datetime.utcnow() - timedelta(days=100), 4)
        cutoff = datetime.utcnow() - timedelta(days=90)
        with self.assertRaises(RuntimeError):
            self.storage.archive_and_purge_decisions(self.tenant_id, cutoff, _FailingArchiver())
        self.assertEqual(self._count(old_ids), 4, "nothing purged when the sink fails")


if __name__ == "__main__":
    unittest.main()
