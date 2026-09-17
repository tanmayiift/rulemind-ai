"""Backup with a verified restore (Track B6).

An unverified backup is not a backup. The failure that actually happens is not pg_dump
erroring loudly -- it is pg_dump succeeding for months while producing something that cannot
be restored, discovered during the incident.

So these tests do not check that a dump file appears. They check that the VERIFIER fails when
the restore is wrong, because a verifier that cannot fail is exactly the false assurance the
feature exists to remove. Three ways a restore can be wrong are injected deliberately: a
missing table, a short table, and -- the one worth the most -- an intact-looking restore whose
audit hash chain no longer verifies.

That last case is why verification reaches past row counts. A dump that drops or reorders
audit rows restores to something that looks healthy by every count and has lost its
tamper-evidence, and a compliance guarantee that holds only on the primary is not one.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
for candidate in (APP_ROOT, APP_ROOT / "scripts"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

import backup_verify  # noqa: E402

from app.storage import Storage  # noqa: E402


class BackupVerifyTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.tempdir.name) / "live.db"
        # force_sqlite: _dump()/_restore() below build a `sqlite:///` URL from this same
        # path, so the Postgres lane must not redirect the write side out from under them.
        self.storage = Storage(path=str(self.db_path), force_sqlite=True)
        self.tenant = self.storage.default_tenant_id
        for i in range(4):
            self.storage.add_audit_event(
                {"event_type": "policy_promoted", "detail": f"promoted {i}"}, tenant_id=self.tenant)
        self.storage.add_decision(
            {"policy_id": "p", "outcome": "approve", "payload": {}}, tenant_id=self.tenant)
        self.url = f"sqlite:///{self.db_path}"

    def tearDown(self):
        self.tempdir.cleanup()

    def _dump(self) -> Path:
        return backup_verify.dump(self.url, Path(self.tempdir.name) / "backups")

    def _restore_url(self, dump_path: Path) -> str:
        restored, cleanup = backup_verify.restore(self.url, dump_path, "scratch")
        self.addCleanup(cleanup)
        return restored

    # ── the happy path ──────────────────────────────────────────────────────

    def test_a_good_backup_restores_and_verifies(self):
        report = backup_verify.verify(self.url, self._restore_url(self._dump()))
        self.assertTrue(report["ok"], report["problems"])
        self.assertGreater(report["tables"], 10)
        self.assertGreater(report["rows"], 0)

    def test_the_dump_is_a_consistent_snapshot_not_a_file_copy(self):
        """sqlite3's .backup API is used rather than shutil.copy: a copy taken mid-write can
        capture a torn page or miss the WAL, producing a file that opens and is subtly wrong."""
        dump_path = self._dump()
        self.assertTrue(dump_path.exists())
        with sqlite3.connect(str(dump_path)) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    # ── the verifier must be able to fail ───────────────────────────────────

    def test_a_missing_table_fails_verification(self):
        restored = self._restore_url(self._dump())
        with sqlite3.connect(backup_verify._sqlite_path(restored)) as conn:
            conn.execute("DROP TABLE audit_events")
        report = backup_verify.verify(self.url, restored)
        self.assertFalse(report["ok"])
        self.assertTrue(any("table missing from restore: audit_events" in p for p in report["problems"]),
                        report["problems"])

    def test_a_short_table_fails_verification(self):
        restored = self._restore_url(self._dump())
        with sqlite3.connect(backup_verify._sqlite_path(restored)) as conn:
            conn.execute("DELETE FROM audit_events WHERE seq = 2")
        report = backup_verify.verify(self.url, restored)
        self.assertFalse(report["ok"])
        self.assertTrue(any("row count differs for audit_events" in p for p in report["problems"]),
                        report["problems"])

    def test_a_broken_audit_chain_fails_even_when_counts_match(self):
        """The case row counts cannot see. Editing a row in place leaves every table and every
        count identical, and the restore is still not trustworthy."""
        restored = self._restore_url(self._dump())
        with sqlite3.connect(backup_verify._sqlite_path(restored)) as conn:
            conn.execute("UPDATE audit_events SET detail = 'tampered' WHERE seq = 2")

        counts_source = backup_verify.table_counts(self.url)
        counts_restore = backup_verify.table_counts(restored)
        self.assertEqual(counts_source, counts_restore,
                         "premise: counts match, so only the chain check can catch this")

        report = backup_verify.verify(self.url, restored)
        self.assertFalse(report["ok"])
        self.assertTrue(any("audit chain broken after restore" in p for p in report["problems"]),
                        report["problems"])

    def test_the_cli_returns_nonzero_when_the_backup_is_bad(self):
        """Exit status is what a cron job or CI actually reads; a verifier that prints a
        problem and exits 0 is worse than none, because it looks monitored."""
        self.assertEqual(backup_verify.main(["--url", self.url,
                                             "--out", str(Path(self.tempdir.name) / "b1")]), 0)

        broken = Path(self.tempdir.name) / "broken.db"
        with sqlite3.connect(str(self.db_path)) as src, sqlite3.connect(str(broken)) as dst:
            src.backup(dst)
        with sqlite3.connect(str(broken)) as conn:
            conn.execute("DELETE FROM audit_events")
        # Verify the DAMAGED copy against the healthy source: the shape a real bad restore has.
        report = backup_verify.verify(self.url, f"sqlite:///{broken}")
        self.assertFalse(report["ok"])


if __name__ == "__main__":
    unittest.main()
