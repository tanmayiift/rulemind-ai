"""Durable decision WAL — closes the SIGKILL/OOM loss window (task #88).

Proves: (1) a decision appended to the WAL but never written to the DB is recovered on the next
startup; (2) recovery is idempotent (replaying twice never double-counts); (3) a torn final line
from a mid-write kill is skipped safely; (4) a REAL SIGKILL after append() — before any DB write —
loses nothing, because startup replay re-inserts it.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from sqlalchemy import func, select  # noqa: E402

import app.decision_wal as decision_wal  # noqa: E402
from app.models import Decision  # noqa: E402
from app.storage import Storage  # noqa: E402


def _decision_count(storage: Storage) -> int:
    with storage.connect() as session:
        return int(session.scalar(select(func.count(Decision.id))) or 0)


def _record(rec_id: str, tenant_id: str) -> dict:
    return {"id": rec_id, "tenant_id": tenant_id, "policy_id": "p1", "outcome": "approve",
            "payload": {"score": 720}, "source": "api_fast"}


class DecisionWalTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        os.environ["DECISION_WAL"] = "1"
        os.environ["DECISION_WAL_DIR"] = os.path.join(self.tempdir.name, "wal")
        self.db_path = os.path.join(self.tempdir.name, "wal.db")
        self.storage = Storage(path=self.db_path)
        self.tenant = self.storage.default_tenant_id
        decision_wal.reset_for_test()

    def tearDown(self):
        decision_wal.reset_for_test()
        os.environ.pop("DECISION_WAL", None)
        os.environ.pop("DECISION_WAL_DIR", None)
        self.tempdir.cleanup()

    def test_append_then_recover_reinserts_missing_decisions(self):
        # Simulate the crash window: append to WAL, but the DB write never happens.
        for i in range(50):
            decision_wal.append(_record("wal-%d" % i, self.tenant), self.tenant)
        self.assertEqual(_decision_count(self.storage), 0)  # nothing in the DB yet

        result = decision_wal.recover(self.storage)
        self.assertEqual(result["replayed"], 50)
        self.assertEqual(_decision_count(self.storage), 50)  # all recovered

    def test_recovery_is_idempotent(self):
        for i in range(20):
            decision_wal.append(_record("idem-%d" % i, self.tenant), self.tenant)
        first = decision_wal.recover(self.storage)
        self.assertEqual(first["replayed"], 20)
        self.assertEqual(_decision_count(self.storage), 20)

        # Re-append the SAME ids (a retry that didn't see the ack) and recover again:
        # they are already durable in the DB, so replay must insert NOTHING new.
        for i in range(20):
            decision_wal.append(_record("idem-%d" % i, self.tenant), self.tenant)
        second = decision_wal.recover(self.storage)
        self.assertEqual(second["replayed"], 0)
        self.assertEqual(second["already_durable"], 20)
        self.assertEqual(_decision_count(self.storage), 20)  # no double-count

    def test_committed_write_is_compacted_not_replayed(self):
        # A decision whose DB write already landed must not be replayed on recovery.
        decision_wal.append(_record("done-1", self.tenant), self.tenant)
        self.storage.add_decision(_record("done-1", self.tenant), tenant_id=self.tenant)  # DB write lands
        result = decision_wal.recover(self.storage)
        self.assertEqual(result["replayed"], 0)
        self.assertEqual(result["already_durable"], 1)
        self.assertEqual(_decision_count(self.storage), 1)

    def test_torn_final_line_is_skipped(self):
        decision_wal.append(_record("good-1", self.tenant), self.tenant)
        # Simulate a process killed mid-write: append a truncated JSON fragment.
        path = decision_wal._path_for()
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"id": "torn", "record": {"id": "torn", "outc')  # no newline, invalid JSON
        result = decision_wal.recover(self.storage)
        self.assertEqual(result["replayed"], 1)          # only the good record
        self.assertEqual(_decision_count(self.storage), 1)

    def test_real_sigkill_after_append_loses_nothing(self):
        """The core guarantee: a hard SIGKILL after append() (before any DB write) is recovered."""
        child = textwrap.dedent(
            """
            import os, signal, sys
            os.environ["DECISION_WAL"] = "1"
            os.environ["DECISION_WAL_DIR"] = sys.argv[1]
            os.environ["RULEMIND_CONFIG_KEY"] = "rulemind-test-key"
            sys.path.insert(0, sys.argv[3])
            import app.decision_wal as decision_wal
            rec = {"id": "killed-1", "tenant_id": sys.argv[2], "policy_id": "p1",
                   "outcome": "approve", "payload": {"x": 1}, "source": "api_fast"}
            decision_wal.append(rec, sys.argv[2])   # durable: fsynced before we die
            os.kill(os.getpid(), signal.SIGKILL)    # hard kill BEFORE any DB write
            """
        )
        proc = subprocess.run(
            [sys.executable, "-c", child, os.environ["DECISION_WAL_DIR"], self.tenant, str(APP_ROOT)],
            capture_output=True,
        )
        # The child was SIGKILLed -> negative returncode (-9), and it never touched the DB.
        self.assertEqual(proc.returncode, -signal.SIGKILL)
        self.assertEqual(_decision_count(self.storage), 0)

        # Startup replay recovers the decision the killed process had only WAL-appended.
        result = decision_wal.recover(self.storage)
        self.assertEqual(result["replayed"], 1)
        self.assertEqual(_decision_count(self.storage), 1)
        with self.storage.connect() as session:
            row = session.scalar(select(Decision).where(Decision.id == "killed-1"))
            self.assertIsNotNone(row)
            self.assertEqual(row.outcome, "approve")


if __name__ == "__main__":
    unittest.main()
