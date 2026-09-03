"""The sandbox pool must survive wedged workers and never silently drop its boundary (T2.2).

Three failures, one theme -- under load the sandbox degraded toward running untrusted code with
no boundary and no timeout:

  1. future.result(timeout=) times out the WAIT, not the task. A `while True` worker keeps
     running; that pool slot is gone forever, and SANDBOX_POOL_SIZE of them kill the pool.
  2. A broken pool was cached and never rebuilt, so one crash disabled the sandbox until redeploy.
  3. SANDBOX_FALLBACK_MODE defaulted to "inline" -- on any pool failure, customer Python ran in
     the API process with no timeout, no RLIMIT, no process boundary.

Combined with the null-flips-gates bug (T1.2), (1)+(3) meant that at peak load -- when fraud
volume is highest -- the failure mode was "run everything unsandboxed and approve what errors."
That combination, not the latency, is the reason this could not ship.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from app import sandbox  # noqa: E402

WEDGE = "@variable\ndef v(payload, variables):\n    while True:\n        pass\n"
GOOD = "@variable\ndef v(payload, variables):\n    return payload.get('age', 0)\n"


class FallbackDefaultTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("SANDBOX_FALLBACK_MODE", None)

    def test_the_default_is_fail_not_inline(self):
        """The single most important line: an unavailable sandbox must not run code in-process."""
        os.environ.pop("SANDBOX_FALLBACK_MODE", None)
        with mock.patch.object(sandbox, "_execute_inline",
                               side_effect=AssertionError("inline fallback ran by default")):
            result = sandbox._fallback_or_fail(RuntimeError("pool dead"), GOOD, {"age": 40}, {})
        self.assertIsNone(result["value"])
        self.assertIn("unavailable", result["error"])

    def test_inline_fallback_is_opt_in(self):
        os.environ["SANDBOX_FALLBACK_MODE"] = "inline"
        result = sandbox._fallback_or_fail(RuntimeError("pool dead"), GOOD, {"age": 40}, {})
        self.assertEqual(result["value"], 40)
        self.assertEqual(result["sandbox_fallback"], "inline")

    def test_a_failed_variable_under_the_fail_default_is_a_none_with_an_error(self):
        """So the T1.2 guard sees an error and fails the decision closed, rather than a bare null
        that silently flips a gate."""
        os.environ.pop("SANDBOX_FALLBACK_MODE", None)
        result = sandbox._fallback_or_fail(RuntimeError("x"), GOOD, {}, {})
        self.assertIsNone(result["value"])
        self.assertTrue(result["error"])


class PoolRebuildTest(unittest.TestCase):
    def setUp(self):
        sandbox._pool = None

    def tearDown(self):
        try:
            if sandbox._pool is not None:
                sandbox._recycle_pool(sandbox._pool)
        finally:
            sandbox._pool = None

    def test_a_broken_pool_is_rebuilt_on_the_next_get(self):
        pool = sandbox._get_pool()
        # Simulate the state ProcessPoolExecutor lands in when a worker dies mid-task.
        pool._broken = "a worker died"
        rebuilt = sandbox._get_pool()
        self.assertIsNot(rebuilt, pool, "a broken pool was handed back instead of rebuilt")
        self.assertFalse(getattr(rebuilt, "_broken", False))

    def test_recycle_only_kills_the_instance_that_failed(self):
        """Two threads timing out at once must not have the second kill the first's fresh pool."""
        pool = sandbox._get_pool()
        sandbox._recycle_pool(pool)
        self.assertIsNone(sandbox._pool)
        fresh = sandbox._get_pool()
        # A stale reference recycling again is a no-op against the fresh pool.
        sandbox._recycle_pool(pool)
        self.assertIs(sandbox._get_pool(), fresh, "a stale recycle killed the fresh pool")


class WedgeRecoveryTest(unittest.TestCase):
    """The end-to-end property: a wedged worker does not permanently consume the pool.

    Real, not mocked -- spawns actual workers, wedges enough of them to exhaust the pool, then
    asserts a normal variable still computes afterward.
    """

    def setUp(self):
        self._prev_size = os.environ.get("SANDBOX_POOL_SIZE")
        os.environ["SANDBOX_POOL_SIZE"] = "2"
        sandbox._POOL_SIZE = 2
        sandbox._pool = None

    def tearDown(self):
        try:
            if sandbox._pool is not None:
                sandbox._recycle_pool(sandbox._pool)
        finally:
            sandbox._pool = None
        if self._prev_size is None:
            os.environ.pop("SANDBOX_POOL_SIZE", None)
        else:
            os.environ["SANDBOX_POOL_SIZE"] = self._prev_size
        sandbox._POOL_SIZE = int(os.environ.get("SANDBOX_POOL_SIZE", "4"))

    def test_the_pool_recovers_after_being_wedged(self):
        # Wedge more tasks than there are workers, each with a short timeout so the test is quick.
        for _ in range(4):
            out = sandbox.execute_variable(WEDGE, {}, {}, timeout_ms=300)
            self.assertIn("timed out", (out.get("error") or ""))
        # A broken/wedged pool used to make this hang or error forever. It must now just work.
        os.environ.pop("SANDBOX_FALLBACK_MODE", None)
        good = sandbox.execute_variable(GOOD, {"age": 51}, {}, timeout_ms=4000)
        self.assertEqual(good.get("value"), 51,
                         "the pool did not recover after wedged workers: {0}".format(good))


if __name__ == "__main__":
    unittest.main()
