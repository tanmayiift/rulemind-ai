"""The WAL used to destroy the decisions it exists to protect (task 5.5).

Two distinct ways, both of which turn DECISION_WAL=1 from a safety net into a liability:

**1. recover() deleted its siblings' LIVE WAL files.** The Dockerfile runs uvicorn with
``--workers 4``. recover() globbed every ``decision_wal_*.jsonl`` in the directory and
``os.remove``d each one, with no pid check and no lock. So whenever one worker booted -- a
crash-restart, or the staggered start of a rolling deploy -- it deleted the WAL files the
other three were actively appending to. Every decision those three had fsynced but not yet
committed to the DB went with them. That is precisely the SIGKILL/OOM window the WAL was
added to close, and having the WAL ON made it *more* likely to lose data, because a WAL
append happens on every decision while a crash does not.

**2. A record whose replay INSERT failed was dropped permanently.** The replay loop
``continue``d past the exception and the file was removed unconditionally afterwards. A
transient DB error during startup -- a connection not yet up, a lock timeout, exactly the
conditions of a mass restart -- silently destroyed the record instead of retrying it on the
next boot. The one code path whose entire job is "do not lose this" had a branch that lost it
without a trace.

The fix is an exclusive ``flock`` per WAL file, held for the life of the owning process:
recovery touches only files it can lock (i.e. orphans of dead processes), and a file with an
unresolved record is rewritten to keep that record rather than deleted.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

import app.decision_wal as decision_wal  # noqa: E402
from app.storage import Storage  # noqa: E402


def _record(rec_id: str, tenant_id: str) -> dict:
    return {"id": rec_id, "tenant_id": tenant_id, "policy_id": "p1", "outcome": "approve",
            "payload": {"score": 720}, "source": "api"}


class WalMultiWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.wal_dir = os.path.join(self.tempdir.name, "wal")
        os.environ["DECISION_WAL"] = "1"
        os.environ["DECISION_WAL_DIR"] = self.wal_dir
        self.storage = Storage(path=os.path.join(self.tempdir.name, "wal.db"))
        self.tenant = self.storage.default_tenant_id
        decision_wal.reset_for_test()
        self.sibling = None

    def tearDown(self):
        if self.sibling is not None:
            if self.sibling.poll() is None:
                self.sibling.kill()
                self.sibling.wait(timeout=5)
            for stream in (self.sibling.stdout, self.sibling.stderr):
                if stream is not None:
                    stream.close()
        decision_wal.reset_for_test()
        os.environ.pop("DECISION_WAL", None)
        os.environ.pop("DECISION_WAL_DIR", None)
        self.tempdir.cleanup()

    def _start_live_sibling(self, path: str, lines: list) -> None:
        """A second PROCESS that owns `path`: writes the lines, takes the exclusive lock, waits.

        A thread would not do — flock is per-process-per-open-file-description, and the whole
        point is what one uvicorn worker can do to another's file.
        """
        script = textwrap.dedent("""
            import fcntl, sys, time
            path = sys.argv[1]
            payload = sys.argv[2]
            fh = open(path, "a+", encoding="utf-8")
            fh.write(payload)
            fh.flush()
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            print("locked", flush=True)
            time.sleep(60)
        """)
        payload = "".join(json.dumps(line) + "\n" for line in lines)
        self.sibling = subprocess.Popen(
            [sys.executable, "-c", script, path, payload],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        # Wait for it to actually hold the lock before we race it.
        deadline = time.time() + 15
        while time.time() < deadline:
            line = self.sibling.stdout.readline()
            if line.strip() == "locked":
                return
            if self.sibling.poll() is not None:
                self.fail("sibling worker died: " + (self.sibling.stderr.read() or ""))
        self.fail("sibling worker never acquired the lock")

    def test_recovery_does_not_delete_a_live_workers_wal(self):
        os.makedirs(self.wal_dir, exist_ok=True)
        sibling_path = os.path.join(self.wal_dir, "decision_wal_999999_sibling.jsonl")
        entries = [{"id": "live-{0}".format(i), "tenant_id": self.tenant,
                    "record": _record("live-{0}".format(i), self.tenant)} for i in range(5)]
        self._start_live_sibling(sibling_path, entries)

        # This process boots and recovers. It must leave the sibling's file completely alone.
        result = decision_wal.recover(self.storage)

        self.assertTrue(os.path.exists(sibling_path),
                        "recover() deleted a WAL file another live worker is appending to -- "
                        "every un-committed decision in it is now unrecoverable")
        with open(sibling_path, "r", encoding="utf-8") as fh:
            surviving = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(len(surviving), 5, "the sibling's entries must be untouched")
        self.assertEqual(result["skipped_locked"], 1)
        self.assertEqual(result["replayed"], 0)

    def test_a_replay_that_fails_is_kept_not_dropped(self):
        decision_wal.append(_record("keeps-failing", self.tenant), self.tenant)
        decision_wal.append(_record("inserts-fine", self.tenant), self.tenant)
        path = decision_wal._file_path
        decision_wal.reset_for_test()  # the owning process died

        real_add = self.storage.add_decision

        def flaky_add(record, tenant_id=None):
            if record.get("id") == "keeps-failing":
                raise RuntimeError("transient DB error during startup replay")
            return real_add(record, tenant_id=tenant_id)

        self.storage.add_decision = flaky_add  # type: ignore[method-assign]
        try:
            result = decision_wal.recover(self.storage)
        finally:
            self.storage.add_decision = real_add  # type: ignore[method-assign]

        self.assertEqual(result["replayed"], 1)
        self.assertEqual(result["quarantined"], 1)
        self.assertTrue(os.path.exists(path),
                        "the WAL file was deleted even though one record never reached the DB")
        with open(path, "r", encoding="utf-8") as fh:
            kept = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual([e["id"] for e in kept], ["keeps-failing"],
                         "only the unresolved record should remain (the rest is durable in the DB)")

        # The next boot, with a healthy DB, must finish the job.
        second = decision_wal.recover(self.storage)
        self.assertEqual(second["replayed"], 1)
        self.assertEqual(second["quarantined"], 0)
        self.assertFalse(os.path.exists(path), "file should be gone once every record is durable")

    def test_a_live_process_holds_an_exclusive_lock_on_its_own_file(self):
        """The property every other guarantee here rests on.

        If the owning process does not hold the lock, "can I lock it?" stops meaning "is its
        owner dead?", and recovery is back to deleting live files.
        """
        import fcntl

        decision_wal.append(_record("held", self.tenant), self.tenant)
        path = decision_wal._file_path
        probe = open(path, "r+", encoding="utf-8")
        try:
            with self.assertRaises(OSError, msg="another process was able to lock our live WAL"):
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            probe.close()

    def test_committed_entries_are_compacted_so_the_file_does_not_grow_forever(self):
        """A long-lived worker leaked one line of disk and one set entry per decision.

        Nothing ever compacted a WAL file at runtime -- only recover(), at startup -- and
        `_committed`/`_appended` were only ever added to. On a busy worker that is unbounded
        growth in the component whose whole purpose is to survive running out of memory.
        """
        os.environ["DECISION_WAL_COMPACT_EVERY"] = "10"
        try:
            for i in range(30):
                rec_id = "c{0}".format(i)
                decision_wal.append(_record(rec_id, self.tenant), self.tenant)
                decision_wal.mark_committed(rec_id)
            path = decision_wal._file_path
            decision_wal._file.flush()
            with open(path, "r", encoding="utf-8") as fh:
                remaining = [line for line in fh if line.strip()]
            self.assertLess(len(remaining), 30,
                            "committed entries were never dropped -- the WAL grows without bound")
            self.assertLess(len(decision_wal._committed), 10,
                            "_committed grows one entry per decision, forever")
        finally:
            os.environ.pop("DECISION_WAL_COMPACT_EVERY", None)


if __name__ == "__main__":
    unittest.main()
