"""Rate limiter fails CLOSED when Redis is unavailable.

A Redis blip must not silently remove all per-tenant limits. With no Redis client the limiter
falls back to the bounded per-replica in-memory limiter (limits still enforced), unless an
operator explicitly opts into fail-open with RATE_LIMIT_FAIL_OPEN=1.
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

import app.runtime as runtime  # noqa: E402


class RateLimitFailClosedTests(unittest.TestCase):
    def setUp(self):
        # Ensure the no-Redis fallback path (test env has no REDIS_URL / reachable Redis).
        self._saved = os.environ.get("RATE_LIMIT_FAIL_OPEN")
        os.environ.pop("RATE_LIMIT_FAIL_OPEN", None)
        self.key = "test:" + uuid.uuid4().hex

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RATE_LIMIT_FAIL_OPEN", None)
        else:
            os.environ["RATE_LIMIT_FAIL_OPEN"] = self._saved

    def test_fallback_still_enforces_limit(self):
        limit = 3
        results = [runtime.rate_limit_allow(self.key, limit)[0] for _ in range(limit + 2)]
        self.assertEqual(results[:limit], [True] * limit, "first N within limit are allowed")
        self.assertIn(False, results[limit:], "requests beyond the limit are blocked, not open")

    def test_fail_open_opt_in(self):
        os.environ["RATE_LIMIT_FAIL_OPEN"] = "1"
        limit = 2
        results = [runtime.rate_limit_allow(self.key, limit)[0] for _ in range(limit + 5)]
        self.assertTrue(all(results), "with RATE_LIMIT_FAIL_OPEN=1 everything is allowed")


if __name__ == "__main__":
    unittest.main()
