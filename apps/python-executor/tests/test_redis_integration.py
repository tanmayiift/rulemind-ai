"""The Redis-backed paths, against a REAL Redis (#127).

Everything Redis-shaped in this repo has only ever been tested through its no-Redis fallback:
test_decision_bus.py says so outright, and test_rate_limit.py exercises the local bucket. So
the Lua token bucket, the evalsha/NOSCRIPT recovery, the Redis-backed cache, the actual
PUBLISH and the async subscriber have never run -- which is awkward, because the entire
justification for the Lua script (rate_limit.py:10-16) is cross-replica atomicity, a property
that by definition cannot be observed without a real server.

Gated on REDIS_URL so a plain checkout still passes with no docker:

    docker run -d -p 6380:6379 redis:7-alpine
    REDIS_URL=redis://127.0.0.1:6380/9 python -m unittest tests.test_redis_integration

Uses DB 9 and FLUSHDBs it, so it cannot disturb a developer's real data on DB 0.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
import uuid
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from app import decision_bus, rate_limit, runtime  # noqa: E402

REDIS_URL = os.getenv("REDIS_URL", "")


@unittest.skipUnless(REDIS_URL, "set REDIS_URL (e.g. redis://127.0.0.1:6380/9) to run the Redis suite")
class RedisIntegrationTest(unittest.TestCase):
    def setUp(self):
        # Both resets are required and they are separate globals. The client latch lives in
        # runtime; _LUA_SHA lives in rate_limit and survives a client reset, so a stale SHA
        # would otherwise reach the NOSCRIPT path by accident rather than by the test's design.
        runtime.reset_redis_client_for_tests()
        rate_limit._LUA_SHA = None
        client = runtime.redis_client()
        self.assertIsNotNone(client, "REDIS_URL is set but Redis is not reachable")
        self.client = client
        self.client.flushdb()

    def tearDown(self):
        try:
            self.client.flushdb()
        finally:
            runtime.reset_redis_client_for_tests()
            rate_limit._LUA_SHA = None

    # ── cache ───────────────────────────────────────────────────────────────

    def test_cache_round_trips_through_redis(self):
        key = "test:cache:" + uuid.uuid4().hex
        runtime.cache_set_json(key, {"a": 1, "b": ["x"]}, ttl_seconds=60)
        self.assertEqual(runtime.cache_get_json(key), {"a": 1, "b": ["x"]})
        # Asserted at the server, not just through the helper: a helper that silently fell
        # back to the in-process dict would satisfy the line above on its own.
        self.assertIsNotNone(self.client.get(key) or self.client.get("cache:" + key))

    def test_a_cache_entry_expires(self):
        key = "test:cache:" + uuid.uuid4().hex
        runtime.cache_set_json(key, {"v": 1}, ttl_seconds=1)
        self.assertEqual(runtime.cache_get_json(key), {"v": 1})
        time.sleep(1.6)
        self.assertIsNone(runtime.cache_get_json(key), "TTL must be applied server-side")

    def test_a_missing_key_is_none(self):
        self.assertIsNone(runtime.cache_get_json("test:cache:" + uuid.uuid4().hex))

    # ── token bucket (the Lua script) ───────────────────────────────────────

    def test_the_bucket_allows_a_burst_then_denies(self):
        key = "test:rl:" + uuid.uuid4().hex
        allowed = sum(1 for _ in range(5) if rate_limit._consume(key, rate=1.0, burst=5, cost=1.0)[0])
        self.assertEqual(allowed, 5, "a full burst must be admitted")
        nxt = rate_limit._consume(key, rate=1.0, burst=5, cost=1.0)
        self.assertEqual(nxt[0], 0, "the 6th within the same second must be denied")

    def test_the_bucket_refills_over_time(self):
        key = "test:rl:" + uuid.uuid4().hex
        for _ in range(3):
            rate_limit._consume(key, rate=5.0, burst=3, cost=1.0)
        self.assertEqual(rate_limit._consume(key, rate=5.0, burst=3, cost=1.0)[0], 0)
        time.sleep(0.5)  # 5/s => ~2 tokens back
        self.assertEqual(rate_limit._consume(key, rate=5.0, burst=3, cost=1.0)[0], 1)

    def test_noscript_recovery_reloads_the_script(self):
        """The branch a real server is required to reach. SCRIPT FLUSH invalidates the cached
        SHA; rate_limit must reload it rather than failing the request."""
        key = "test:rl:" + uuid.uuid4().hex
        self.assertEqual(rate_limit._consume(key, rate=10.0, burst=10, cost=1.0)[0], 1)
        sha_before = rate_limit._LUA_SHA
        self.assertIsNotNone(sha_before, "the script should be cached after the first call")

        self.client.script_flush()

        allowed, _, _ = rate_limit._consume(key, rate=10.0, burst=10, cost=1.0)
        self.assertEqual(allowed, 1, "a flushed script must be reloaded, not surfaced as a failure")
        self.assertIsNotNone(rate_limit._LUA_SHA)

    def test_two_clients_share_one_bucket(self):
        """The property the Lua script exists for. Two independent connections -- standing in
        for two API replicas -- must not each get their own allowance."""
        key = "test:rl:" + uuid.uuid4().hex
        for _ in range(4):
            rate_limit._consume(key, rate=0.1, burst=4, cost=1.0)
        runtime.reset_redis_client_for_tests()   # a genuinely new connection
        rate_limit._LUA_SHA = None
        self.assertEqual(rate_limit._consume(key, rate=0.1, burst=4, cost=1.0)[0], 0,
                         "a second replica must see the first replica's consumption")

    # ── pub/sub ─────────────────────────────────────────────────────────────

    def test_publish_reaches_a_real_subscriber(self):
        tenant = "t-" + uuid.uuid4().hex[:8]
        sub = self.client.pubsub()
        sub.subscribe(decision_bus.channel_for(tenant))
        self.assertEqual(sub.get_message(timeout=2)["type"], "subscribe")

        decision_bus.publish_decision(tenant, {
            "id": "d1", "outcome": "approve", "policy_id": "p1",
            "payload": {"secret": "should not travel"}, "trace": ["step"],
        })

        deadline = time.time() + 3
        frame = None
        while time.time() < deadline and frame is None:
            msg = sub.get_message(timeout=1)
            if msg and msg["type"] == "message":
                frame = msg["data"]
        sub.close()
        self.assertIsNotNone(frame, "the decision never arrived on the channel")
        # compact_frame drops the bulky/sensitive fields; asserting that on the WIRE is the
        # only place it can be checked, since the caller passes the full decision.
        self.assertNotIn("should not travel", frame)
        self.assertIn("d1", frame)

    def test_has_redis_is_true_against_a_real_server(self):
        self.assertTrue(decision_bus.has_redis())

    def test_the_async_subscriber_yields_a_published_frame(self):
        tenant = "t-" + uuid.uuid4().hex[:8]

        async def drain():
            out = []
            agen = decision_bus.subscribe_decisions(tenant)
            task = asyncio.ensure_future(_collect(agen, out))
            await asyncio.sleep(0.4)          # let SUBSCRIBE land before publishing
            decision_bus.publish_decision(tenant, {"id": "d2", "outcome": "reject"})
            await asyncio.sleep(0.8)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return out

        async def _collect(agen, out):
            async for item in agen:
                if item:
                    out.append(item)

        frames = asyncio.run(drain())
        self.assertTrue(any(f.get("id") == "d2" for f in frames), f"no frame delivered: {frames}")


if __name__ == "__main__":
    unittest.main()
