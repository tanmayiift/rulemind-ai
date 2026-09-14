"""Rate limiter fails CLOSED when Redis is unavailable.

A Redis blip must not silently remove all per-tenant limits. With no Redis client the limiter
falls back to the bounded per-replica in-memory limiter (limits still enforced), unless an
operator explicitly opts into fail-open with RATE_LIMIT_FAIL_OPEN=1.

These used to call `runtime.rate_limit_allow`, which no requestever went through: the
middleware calls `rate_limit.check`, and `rate_limit_allow` was a second, older fixed-window
limiter with no callers anywhere in `app/` (and an unbounded bucket dict). So this file
asserted the fail-closed property of code that was not in the request path -- the property
held for the wrong limiter. It is now asserted against `rate_limit.check`, the one the
middleware actually calls; the dead limiter has been deleted.
"""
from __future__ import annotations

import os
import sys
import unittest
import uuid
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

import app.rate_limit as rate_limit  # noqa: E402
import app.runtime as runtime  # noqa: E402


class RateLimitFailClosedTests(unittest.TestCase):
    def setUp(self):
        # FORCE the no-Redis fallback path rather than assuming the environment lacks Redis.
        # These tests are about what happens when Redis is unreachable, so leaving that to
        # ambient state meant they failed for a developer who simply had Redis running --
        # and passed in CI for a reason unrelated to the code under test.
        self._saved = os.environ.get("RATE_LIMIT_FAIL_OPEN")
        self._saved_redis = os.environ.pop("REDIS_URL", None)
        os.environ.pop("RATE_LIMIT_FAIL_OPEN", None)
        runtime.reset_redis_client_for_tests()
        rate_limit.reset_local_state()
        self.key = "test:" + uuid.uuid4().hex

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RATE_LIMIT_FAIL_OPEN", None)
        else:
            os.environ["RATE_LIMIT_FAIL_OPEN"] = self._saved
        if self._saved_redis is not None:
            os.environ["REDIS_URL"] = self._saved_redis
        # Clear the latch so a Redis-backed test running later is not poisoned by this one.
        runtime.reset_redis_client_for_tests()

    def _tenant(self, limit):
        return {"id": self.key, "config": {"rate_limit_rpm": limit}}

    def test_fallback_still_enforces_limit(self):
        limit = 3
        results = [rate_limit.check(self._tenant(limit), key=self.key).allowed
                   for _ in range(limit + 2)]
        self.assertEqual(results[:limit], [True] * limit, "first N within limit are allowed")
        self.assertIn(False, results[limit:], "requests beyond the limit are blocked, not open")

    def test_fail_open_opt_in(self):
        os.environ["RATE_LIMIT_FAIL_OPEN"] = "1"
        limit = 2
        results = [rate_limit.check(self._tenant(limit), key=self.key).allowed
                   for _ in range(limit + 5)]
        self.assertTrue(all(results), "with RATE_LIMIT_FAIL_OPEN=1 everything is allowed")

    def test_the_dead_duplicate_limiter_is_gone(self):
        """Guards the deletion, not the behaviour.

        `runtime.rate_limit_allow` and its `InMemoryRateLimiter` were a second limiter with a
        different algorithm (fixed window vs token bucket) and an unbounded bucket dict, reachable
        only from this test file. Two limiters is how the tested one and the shipped one drift;
        if one comes back, this fails and points at that.
        """
        for name in ("rate_limit_allow", "InMemoryRateLimiter", "_MEMORY_LIMITER", "_rate_limit_fallback"):
            self.assertFalse(hasattr(runtime, name),
                             "runtime.{0} is the dead duplicate limiter -- use rate_limit.check".format(name))


if __name__ == "__main__":
    unittest.main()
