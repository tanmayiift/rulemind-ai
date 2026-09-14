"""Concurrent boot seeding — the race a single-process test suite structurally cannot see.

Every replica runs `Storage.seed_if_empty()` at startup against ONE shared database. It is
check-then-insert with nothing in between, so two processes starting together both observe an
empty table and both insert. One wins; the loser raises

    IntegrityError: duplicate key value violates unique constraint "ix_api_keys_lookup_hash"

during startup, and the process dies.

WHY IT SURVIVED ~1900 TESTS. The whole suite runs one process against SQLite, where there is no
second seeder to race. It surfaced only on a real Kubernetes cluster, where the api and worker
pods boot at the same moment: the worker CrashLoopBackOff'd with exactly this traceback. The
Helm chart's production defaults are api=3 and worker=2, so a first install races five ways.

These tests hit the shared-database case directly instead of waiting for a cluster to find it.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from sqlalchemy.exc import IntegrityError  # noqa: E402

from app.storage import Storage  # noqa: E402


class ConcurrentSeedTest(unittest.TestCase):
    """Two Storage instances seeding one database at the same time."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = os.path.join(self.tempdir.name, "seedrace.db")
        self._engines = []

    def tearDown(self):
        for engine in self._engines:
            try:
                engine.dispose()
            except Exception:  # pragma: no cover - teardown must not fail the test
                pass
        self.tempdir.cleanup()

    def _storage(self):
        s = Storage(path=self.path)
        self._engines.append(s.engine)
        return s

    def test_a_second_seeder_does_not_raise(self):
        """The direct case: seed twice over the same database.

        This passes even without the fix, because the second pass takes the `if existing`
        branch -- which is exactly why the bug survived. It is the CONTROL: it proves seeding is
        re-entrant when the two runs are ordered, so the next test isolates concurrency as the
        only variable.
        """
        self._storage().seed_if_empty()
        self._storage().seed_if_empty()

    def test_losing_the_race_recovers_instead_of_crashing(self):
        """The real one: the loser must recover, not die.

        Rather than hoping two threads interleave at the right instant, this makes the race
        deterministic -- the first call raises IntegrityError the way a losing replica does,
        and the seeded state is already present, as it would be if a winner had just committed.
        A process that dies here is a pod in CrashLoopBackOff.
        """
        winner = self._storage()
        winner.seed_if_empty()

        loser = self._storage()
        calls = {"n": 0}
        real = loser._seed_if_empty_once

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise IntegrityError("INSERT INTO api_keys", {}, Exception("duplicate key"))
            return real()

        loser._seed_if_empty_once = flaky
        loser.seed_if_empty()  # must not raise

        self.assertEqual(calls["n"], 2, "the loser never retried after the IntegrityError")
        self.assertTrue(loser.default_api_key, "the loser recovered but has no API key to serve")
        self.assertEqual(loser.default_tenant_id, winner.default_tenant_id,
                         "the loser adopted a different tenant than the winner wrote")

    def test_the_retry_is_bounded(self):
        """A retry loop around a permanently failing insert would hang a pod's startup forever
        instead of crashing it, which is worse -- the probe never fails, so nothing replaces it.
        A persistent IntegrityError must still surface."""
        storage = self._storage()

        def always_fails():
            raise IntegrityError("INSERT INTO api_keys", {}, Exception("duplicate key"))

        storage._seed_if_empty_once = always_fails
        with self.assertRaises(IntegrityError):
            storage.seed_if_empty()

    def test_threads_seeding_together_all_survive(self):
        """The end-to-end shape: N threads seeding one database, no exception anywhere.

        The Storage objects are constructed SERIALLY and only `seed_if_empty()` is raced. That is
        not a convenience -- constructing them inside the threads races `_create_schema` instead,
        and on SQLite that fails with "table api_keys already exists", masking the thing under
        test. `_create_schema` already handles concurrency where it matters: it takes a Postgres
        advisory lock and swallows a lost race, and its docstring describes this exact
        worker-startup crash. The unguarded branch is SQLite-only, which is single-process by
        definition. So the schema race is solved and the seeding race was not; this test isolates
        the second.

        Threads share a process, so this is still weaker than separate pods -- but it fails
        loudly if the retry is removed.
        """
        errors = []
        # Serial construction: schema exists before any thread starts.
        storages = [self._storage() for _ in range(4)]
        barrier = threading.Barrier(len(storages))

        def boot(storage):
            try:
                barrier.wait(timeout=10)
                storage.seed_if_empty()
            except Exception as exc:  # noqa: BLE001 - the assertion is that this list stays empty
                errors.append(exc)

        threads = [threading.Thread(target=boot, args=(st,)) for st in storages]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        self.assertEqual(errors, [], "a concurrent seeder crashed: {0}".format(errors))


if __name__ == "__main__":
    unittest.main()
