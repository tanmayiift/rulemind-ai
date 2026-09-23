"""Caching "is an experiment running" must not let the fast path serve an experimented policy.

`fast_path_eligible` used to call `storage.list_experiments(tenant_id=...)` inline, uncached,
on EVERY decision -- and inside the eligibility check, so the query was paid even when the
policy turned out ineligible and `FAST_DECIDE` did nothing at all. Every other catalog read on
that path already went through the epoch+TTL cache; this one did not, purely because it was
written as a loop rather than a lookup.

Caching it introduces a hazard bigger than the saving. The check exists because the fast path
applies NO experiment overrides and makes NO variant assignment: a policy under a running
experiment must go to the executor. A stale cache means a decision taken in the window after an
experiment starts silently skips assignment -- which is the A/B bug this function was written to
prevent, reintroduced through the optimisation for it.

So the writes invalidate, at the STORAGE layer rather than in the router, because the
scheduler's auto-rollback writes experiment status too and a cache correct only for HTTP
callers is not correct. These tests are about that invalidation. The one that matters is
`test_starting_an_experiment_takes_effect_immediately`: it fails on a cache with no
invalidation, which is the change that would otherwise look free.
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

import app.main as app_main  # noqa: E402
from app import fast_decide  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import preserve_storage_global  # noqa: E402

POLICY = {
    "id": "p_exp", "name": "Rules-only", "status": "dev", "version": 1,
    "steps": [{"type": "outcome", "ref_id": "approve"}],
}
EXPERIMENT = {
    "id": "e1", "name": "Champion vs challenger", "status": "draft",
    "hash_key": "user_id", "target_policy_id": "p_exp",
    "variants": [{"id": "control", "weight": 50}, {"id": "challenger", "weight": 50}],
}


class RunningExperimentCacheTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tempdir.cleanup)
        preserve_storage_global(self)
        self.storage = Storage(path=os.path.join(self.tempdir.name, "exp.db"))
        app_main.storage = self.storage
        self.tenant = self.storage.default_tenant_id
        self.storage.create_policy(POLICY, tenant_id=self.tenant)
        self.policy = self.storage.get_policy("p_exp", tenant_id=self.tenant)
        fast_decide.invalidate()
        self.addCleanup(fast_decide.invalidate)

    def _eligible(self):
        return fast_decide.fast_path_eligible(self.storage, self.policy, self.tenant)

    def test_a_policy_with_no_experiment_is_eligible(self):
        self.assertTrue(self._eligible())

    def test_starting_an_experiment_takes_effect_immediately(self):
        """THE test. Warm the cache with 'no running experiment', start one, and require the
        very next eligibility check to see it. Without invalidation on the write this keeps
        answering True for the cache TTL, and every decision in that window is served with no
        variant assigned."""
        self.assertTrue(self._eligible())                      # warms the cache
        self.storage.create_or_update_experiment(
            {**EXPERIMENT, "status": "running"}, tenant_id=self.tenant)
        self.assertFalse(self._eligible(),
                         "the fast path would serve a policy under a RUNNING experiment with "
                         "no variant assignment")

    def test_stopping_an_experiment_takes_effect_immediately(self):
        """The other direction. A stale 'still running' costs throughput rather than
        correctness, but a cache invalidated in only one direction is a cache nobody can
        reason about."""
        self.storage.create_or_update_experiment(
            {**EXPERIMENT, "status": "running"}, tenant_id=self.tenant)
        self.assertFalse(self._eligible())                     # warms the cache
        self.storage.create_or_update_experiment(
            {**EXPERIMENT, "status": "completed"}, tenant_id=self.tenant)
        self.assertTrue(self._eligible())

    def test_deleting_an_experiment_takes_effect_immediately(self):
        self.storage.create_or_update_experiment(
            {**EXPERIMENT, "status": "draft"}, tenant_id=self.tenant)
        self.assertTrue(self._eligible())
        self.storage.delete_experiment("e1", tenant_id=self.tenant)
        self.assertTrue(self._eligible())

    def test_only_a_RUNNING_experiment_blocks_the_fast_path(self):
        for status in ("draft", "completed", "paused"):
            with self.subTest(status=status):
                self.storage.create_or_update_experiment(
                    {**EXPERIMENT, "status": status}, tenant_id=self.tenant)
                self.assertTrue(self._eligible(),
                                f"a '{status}' experiment must not block the fast path")

    def test_an_experiment_on_a_DIFFERENT_policy_does_not_block_this_one(self):
        self.storage.create_or_update_experiment(
            {**EXPERIMENT, "id": "e2", "target_policy_id": "some_other_policy",
             "status": "running"},
            tenant_id=self.tenant)
        self.assertTrue(self._eligible())

    def test_the_lookup_is_actually_cached(self):
        """Otherwise the change is a rename. Counts the storage reads across three consecutive
        eligibility checks: uncached that is 3, cached it is 1."""
        calls = []
        original = self.storage.list_experiments

        def counting(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        self.storage.list_experiments = counting  # type: ignore[method-assign]
        try:
            for _ in range(3):
                self._eligible()
        finally:
            self.storage.list_experiments = original  # type: ignore[method-assign]
        self.assertEqual(len(calls), 1, f"queried storage {len(calls)} times, expected 1")


if __name__ == "__main__":
    unittest.main()
