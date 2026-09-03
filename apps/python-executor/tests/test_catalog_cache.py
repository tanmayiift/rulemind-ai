"""The slow decision path re-read the whole tenant catalog on every request (task 5.4).

``PolicyExecutor`` carries a catalog memo -- variables, connectors, settings, secrets -- with a
comment explaining that it removes "the repeated DB round-trips that dominated decision
latency". The memo is per-INSTANCE, and ``main.workflow_executor()`` constructs a fresh
instance for every request. So the memo only ever de-duplicated within one decision, and every
``/decide`` paid the full catalog again:

    get_policy, list_experiments, list_rules, list_scorecards, list_decision_tables,
    list_variables, list_connectors, get_settings                       -- the 8

plus, for any policy with an action / transform / review-gate step, ``_tenant_secrets``
looping ``get_connector(id, include_secrets=True)`` once per connector, each with its own
Fernet decrypt -- the ``+N``. On the seeded demo tenant (9 connectors) that measured 16
SELECTs per decision, of which 9 were the secrets loop.

Two things this file pins down, because a cache that gets either wrong is worse than none:

* **Single-flight.** A TTL cache without it converts an N+1 into a thundering herd: every
  request in flight at the instant of expiry misses and all of them run the full rebuild
  concurrently. The stampede is proportional to load, so it lands hardest at peak.
* **Staleness.** A cached catalog must not outlive an authoring change. The epoch scheme
  handles the writes that call ``invalidate()``; the TTL backstop handles the ones that don't
  (connector CRUD and `dev` variable edits bump nothing), and the authoring sources bypass
  the shared cache entirely so "edit, hit Test" can never show a stale value.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

import app.executor as executor_mod  # noqa: E402
import app.fast_decide as fd  # noqa: E402


class _CountingStorage:
    """Counts the catalog reads the executor makes, and can be told to be slow."""

    def __init__(self, build_delay: float = 0.0):
        self.calls = {"list_variables": 0, "list_connectors": 0, "get_settings": 0,
                      "get_connector": 0}
        self.build_delay = build_delay
        self.variables = [{"id": "v1", "code": "x = 1", "source_id": "custom"}]

    def list_variables(self, tenant_id=None):
        self.calls["list_variables"] += 1
        time.sleep(self.build_delay)
        return list(self.variables)

    def list_connectors(self, tenant_id=None):
        self.calls["list_connectors"] += 1
        return [{"id": "bureau", "sample_payload": {}, "schema_paths": []},
                {"id": "bank", "sample_payload": {}, "schema_paths": []}]

    def get_settings(self, tenant_id=None):
        self.calls["get_settings"] += 1
        return {"engine_config": {}}

    def get_connector(self, connector_id, include_secrets=False, tenant_id=None):
        self.calls["get_connector"] += 1
        return {"id": connector_id, "config": {"api_key": "secret-" + connector_id}}

    # not part of the catalog, but the executor's constructor signature wants a Storage
    def list_rules(self, tenant_id=None):
        return []


class _Executor(executor_mod.PolicyExecutor):
    """PolicyExecutor without running a decision -- the catalog path is what is under test."""

    def __init__(self, storage, source="api"):
        super().__init__(storage)
        self._source = source


class CatalogCacheTests(unittest.TestCase):
    TENANT = "tenant-catalog-test"

    def setUp(self):
        fd.invalidate(None)

    def tearDown(self):
        fd.invalidate(None)

    def test_catalog_survives_across_requests(self):
        """The bug itself: a per-instance memo on a per-request instance caches nothing."""
        storage = _CountingStorage()
        for _ in range(5):
            _Executor(storage)._catalog_for(self.TENANT)  # a "request" each
        self.assertEqual(storage.calls["list_variables"], 1,
                         "the tenant catalog was rebuilt per request; the memo never survived one")
        self.assertEqual(storage.calls["list_connectors"], 1)
        self.assertEqual(storage.calls["get_settings"], 1)

    def test_the_secrets_loop_is_paid_once_not_per_request(self):
        """The `+N`: one DB read and one Fernet decrypt per connector, per decision."""
        storage = _CountingStorage()
        for _ in range(5):
            _Executor(storage)._tenant_secrets(self.TENANT)
        self.assertEqual(storage.calls["get_connector"], 2,
                         "expected one read per connector for the whole run, not per request")

    def test_secrets_stay_lazy_for_a_policy_that_never_uses_them(self):
        """Folding secrets into the catalog build would be a regression, not a fix.

        Only action / transform / review-gate steps read secrets. A pure-rule decision -- and
        every one of the thousands of cases in a backtest -- would otherwise pay N connector
        reads it never looks at.
        """
        storage = _CountingStorage()
        _Executor(storage)._catalog_for(self.TENANT)
        self.assertEqual(storage.calls["get_connector"], 0,
                         "the catalog build must not fetch secrets nobody asked for")

    def test_a_burst_of_concurrent_misses_builds_exactly_once(self):
        """Single-flight. Without it the cache trades an N+1 for a stampede at every expiry."""
        storage = _CountingStorage(build_delay=0.05)
        start = threading.Barrier(8)
        errors = []

        def worker():
            try:
                start.wait(timeout=10)
                _Executor(storage)._catalog_for(self.TENANT)
            except Exception as exc:  # pragma: no cover - surfaced via `errors`
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(errors, [])
        self.assertEqual(storage.calls["list_variables"], 1,
                         "8 concurrent misses ran 8 full rebuilds -- that is the thundering herd")

    def test_publishing_invalidates_the_shared_catalog(self):
        """A stale catalog must not survive an authoring change. `invalidate()` is what
        main.maybe_compile_bundle calls on every rule/policy/prod-variable write."""
        storage = _CountingStorage()
        first = _Executor(storage)._catalog_for(self.TENANT)
        self.assertEqual(len(first["variables"]), 1)

        storage.variables = [{"id": "v1", "code": "x = 1", "source_id": "custom"},
                             {"id": "v2", "code": "y = 2", "source_id": "custom"}]
        fd.invalidate(self.TENANT)

        second = _Executor(storage)._catalog_for(self.TENANT)
        self.assertEqual(len(second["variables"]), 2,
                         "a published change was not visible to the next decision")

    def test_a_sibling_replicas_publish_invalidates_this_processes_catalog(self):
        """The half of invalidation the local clear cannot cover.

        `invalidate()` does two independent things: it drops this process's entries, and it
        bumps the tenant's epoch (a Redis INCR in production, so it is global). Only the second
        reaches a SIBLING replica, which never ran the call. This asserts the epoch path on its
        own, by bumping the counter the way another replica's INCR would and never touching
        this process's stores -- otherwise the direct clear masks an epoch check that has
        stopped working, and every other worker serves a stale catalog until it restarts.
        """
        storage = _CountingStorage()
        _Executor(storage)._catalog_for(self.TENANT)
        storage.variables = [{"id": "v1", "code": "x = 1", "source_id": "custom"},
                             {"id": "v2", "code": "y = 2", "source_id": "custom"}]

        fd._LOCAL_EPOCH[self.TENANT] = fd._LOCAL_EPOCH.get(self.TENANT, 0) + 1
        fd._EPOCH_CACHE.pop(self.TENANT, None)  # this replica's epoch TTL expires

        refreshed = _Executor(storage)._catalog_for(self.TENANT)
        self.assertEqual(len(refreshed["variables"]), 2,
                         "the cached entry's epoch stamp is not being checked -- a sibling "
                         "replica's publish is invisible to this one")

    def test_a_stale_entry_expires_even_when_nothing_bumps_the_epoch(self):
        """The TTL backstop.

        Connector create/update/delete and `dev` variable edits do NOT call
        maybe_compile_bundle, so they never bump the epoch -- yet a connector's sample_payload
        is the field set feeding every variable of that source, and its config holds the
        secrets. With epoch coherence alone such an edit could stay invisible indefinitely.
        """
        original_ttl = fd._CACHE_TTL
        fd._CACHE_TTL = 0.05
        try:
            storage = _CountingStorage()
            _Executor(storage)._catalog_for(self.TENANT)
            time.sleep(0.08)
            _Executor(storage)._catalog_for(self.TENANT)
            self.assertEqual(storage.calls["list_variables"], 2,
                            "an entry outlived its TTL, so an edit that bumps no epoch is invisible")
        finally:
            fd._CACHE_TTL = original_ttl

    def test_the_authoring_path_never_reads_from_the_shared_cache(self):
        """"Edit a variable, hit Test, see the old value" is a correctness bug, not a stale
        cache. The test console reads the DB every time; only production sources share."""
        storage = _CountingStorage()
        for _ in range(3):
            _Executor(storage, source="test_console")._catalog_for(self.TENANT)
        self.assertEqual(storage.calls["list_variables"], 3,
                         "the authoring path was served from the shared cache")

    def test_the_shared_catalog_is_bounded(self):
        """These caches are keyed per tenant / per tenant+policy and were plain dicts: on a
        multi-tenant deployment they grew with every distinct key ever served, forever."""
        for store in (fd._SERVING, fd._CATALOG, fd._EPOCH_CACHE, fd._LOCAL_EPOCH):
            self.assertTrue(hasattr(store, "_capacity"),
                            "{0} is an unbounded dict".format(type(store).__name__))

    def test_bounded_cache_actually_evicts(self):
        storage = _CountingStorage()
        capacity = fd._CATALOG._capacity  # type: ignore[attr-defined]
        for i in range(capacity + 10):
            fd.tenant_catalog(storage, "tenant-{0}".format(i))
        self.assertLessEqual(len(fd._CATALOG), capacity)


if __name__ == "__main__":
    unittest.main()
