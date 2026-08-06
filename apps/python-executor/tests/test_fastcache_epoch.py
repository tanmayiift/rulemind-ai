"""Multi-replica fast-path cache coherence.

The serving-bundle cache is per-process. invalidate() on the worker that handled an edit
clears only that process; a sibling replica must still detect the change and rebuild rather
than serve a stale bundle forever. This is enforced by a per-tenant cache *epoch* stamped on
each cached entry: a cached entry whose epoch != the tenant's current epoch is rebuilt.

These tests exercise the epoch mechanism directly (no Redis needed — the process-local epoch
counter has identical semantics; Redis just makes the counter shared across replicas).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

import app.fast_decide as fd  # noqa: E402


class _FakeStorage:
    """Minimal storage the serving-bundle builder needs; counts rebuilds via list_variables."""

    def __init__(self):
        self.build_calls = 0

    def list_variables(self, tenant_id):
        self.build_calls += 1
        return []

    def list_rules(self, tenant_id):
        return []

    def list_scorecards(self, tenant_id):
        return []

    def list_decision_tables(self, tenant_id):
        return []

    def list_connectors(self, tenant_id):
        return []

    def get_settings(self, tenant_id):
        return {"engine_config": {}}


class FastCacheEpochTests(unittest.TestCase):
    def setUp(self):
        fd.invalidate()  # full local reset
        fd._EPOCH_TTL = 0.0  # check the epoch on every read so the test is deterministic
        self.storage = _FakeStorage()
        self.policy = {"id": "pol_1", "steps": []}
        self.tenant = "tenant_a"

    def tearDown(self):
        fd.invalidate()

    def test_second_read_is_cached(self):
        fd._serving_bundle(self.storage, self.tenant, self.policy)
        fd._serving_bundle(self.storage, self.tenant, self.policy)
        self.assertEqual(self.storage.build_calls, 1, "identical epoch should serve from cache")

    def test_invalidate_forces_rebuild_same_process(self):
        fd._serving_bundle(self.storage, self.tenant, self.policy)
        fd.invalidate(self.tenant)
        fd._serving_bundle(self.storage, self.tenant, self.policy)
        self.assertEqual(self.storage.build_calls, 2, "invalidate() must rebuild in-process")

    def test_sibling_replica_rebuilds_on_epoch_bump(self):
        # Simulate a SIBLING replica: it has a stale bundle cached at epoch 0 and never received
        # the invalidate() call, but the tenant's shared epoch was bumped elsewhere. On the next
        # decide the sibling must notice the epoch mismatch and rebuild — the core of the fix.
        stale_bundle = {"policy": self.policy, "variables": [{"id": "STALE"}], "connectors": {},
                        "core_bundle": {}, "rust_bundle": None, "timeout_ms": 2000, "memory_mb": 128}
        with fd._CACHE_LOCK:
            fd._SERVING[f"{self.tenant}:{self.policy['id']}"] = (0, stale_bundle)
        # Another replica bumped the epoch (here: the local counter stands in for the Redis INCR).
        fd._LOCAL_EPOCH[self.tenant] = 5
        fd._EPOCH_CACHE.pop(self.tenant, None)

        served = fd._serving_bundle(self.storage, self.tenant, self.policy)
        self.assertNotIn({"id": "STALE"}, served["variables"], "sibling served a stale bundle")
        self.assertEqual(self.storage.build_calls, 1, "sibling should have rebuilt exactly once")

    def test_epoch_stamp_matches_after_rebuild(self):
        fd._LOCAL_EPOCH[self.tenant] = 3
        fd._EPOCH_CACHE.pop(self.tenant, None)
        fd._serving_bundle(self.storage, self.tenant, self.policy)
        cached = fd._SERVING[f"{self.tenant}:{self.policy['id']}"]
        self.assertEqual(cached[0], 3, "rebuilt entry must be stamped with the current epoch")


if __name__ == "__main__":
    unittest.main()
