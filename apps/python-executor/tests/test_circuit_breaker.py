"""Circuit-breaker state machine + connector-integration tests (task #84)."""
import time
import unittest

from app import circuit_breaker as cb


class CircuitBreakerStateMachineTest(unittest.TestCase):
    def setUp(self) -> None:
        cb.reset_all()

    def _breaker(self):
        return cb.CircuitBreaker("t", failure_threshold=3, recovery_seconds=0.15, half_open_max_calls=1)

    def test_closed_allows_calls(self) -> None:
        b = self._breaker()
        b.allow()  # does not raise
        self.assertEqual(b.state, cb.CLOSED)

    def test_trips_open_after_threshold_consecutive_failures(self) -> None:
        b = self._breaker()
        for _ in range(3):
            b.allow()
            b.record_failure()
        self.assertEqual(b.state, cb.OPEN)
        with self.assertRaises(cb.CircuitOpenError):
            b.allow()
        self.assertEqual(b.trips, 1)

    def test_success_resets_failure_streak(self) -> None:
        b = self._breaker()
        b.allow(); b.record_failure()
        b.allow(); b.record_failure()
        b.allow(); b.record_success()  # heals before hitting the threshold
        b.allow(); b.record_failure()
        self.assertEqual(b.state, cb.CLOSED)  # streak was reset, so 1 failure != trip

    def test_open_then_half_open_probe_success_closes(self) -> None:
        b = self._breaker()
        for _ in range(3):
            b.allow(); b.record_failure()
        self.assertEqual(b.state, cb.OPEN)
        time.sleep(0.2)  # cooldown elapses
        self.assertEqual(b.state, cb.HALF_OPEN)
        b.allow()               # one probe permitted
        with self.assertRaises(cb.CircuitOpenError):
            b.allow()           # second probe blocked while first is in flight
        b.record_success()      # probe succeeded -> fully closed
        self.assertEqual(b.state, cb.CLOSED)
        b.allow()               # calls flow again

    def test_half_open_probe_failure_reopens(self) -> None:
        b = self._breaker()
        for _ in range(3):
            b.allow(); b.record_failure()
        time.sleep(0.2)
        self.assertEqual(b.state, cb.HALF_OPEN)
        b.allow()
        b.record_failure()      # probe failed -> reopen for another cooldown
        self.assertEqual(b.state, cb.OPEN)
        with self.assertRaises(cb.CircuitOpenError):
            b.allow()

    def test_registry_returns_same_breaker_per_key(self) -> None:
        self.assertIs(cb.get_breaker("conn-a"), cb.get_breaker("conn-a"))
        self.assertIsNot(cb.get_breaker("conn-a"), cb.get_breaker("conn-b"))

    def test_short_circuit_counter_increments(self) -> None:
        b = self._breaker()
        for _ in range(3):
            b.allow(); b.record_failure()
        for _ in range(5):
            try:
                b.allow()
            except cb.CircuitOpenError:
                pass
        self.assertEqual(b.short_circuits, 5)


class CircuitBreakerConnectorIntegrationTest(unittest.IsolatedAsyncioTestCase):
    """A persistently failing downstream trips the breaker so later calls short-circuit
    (fast fail) instead of paying the full timeout budget again."""

    async def test_failing_action_trips_breaker_and_short_circuits(self) -> None:
        import os
        os.environ["CIRCUIT_BREAKER"] = "1"
        os.environ["CB_FAILURE_THRESHOLD"] = "2"
        cb.reset_all()
        from app.executor import PolicyExecutor, ExecutionContext

        class _FakeStorage:
            def add_action_log(self, *a, **k):
                return None

        execu = PolicyExecutor.__new__(PolicyExecutor)
        execu.storage = _FakeStorage()

        ctx = ExecutionContext.__new__(ExecutionContext)
        ctx.tenant_id = "t"
        ctx.execution_id = "e"
        ctx.action_results = []
        ctx.outcome = "pending"
        ctx.payload = {}
        ctx.variables = {}

        # Point at a black-hole port so the httpx call fails fast (connection refused).
        cfg = {"url": "http://127.0.0.1:59999/never", "method": "GET", "timeoutMs": 200,
               "retries": 0, "onFailure": "continue"}

        # Patch _context_view to a no-op passthrough (avoid full ctx setup).
        execu._context_view = lambda c: {}

        step = {"id": "s1", "name": "flaky", "ref_id": "flaky-conn"}
        # First two calls actually attempt and fail -> breaker trips at threshold 2.
        r1 = await execu._fire_action_request(step, ctx, cfg)
        r2 = await execu._fire_action_request(step, ctx, cfg)
        self.assertFalse(r1["success"])
        self.assertFalse(r2["success"])
        breaker = cb.get_breaker("flaky-conn")
        self.assertEqual(breaker.state, cb.OPEN)

        # Third call must be short-circuited by the breaker (no HTTP attempt).
        r3 = await execu._fire_action_request(step, ctx, cfg)
        self.assertTrue(r3.get("circuit_open"))
        self.assertEqual(r3.get("attempts"), 0)


if __name__ == "__main__":
    unittest.main()
