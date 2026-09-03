"""A HUNG Redis used to hang the whole API, not just Redis (task 5.3).

``runtime.redis_client()`` built its client with ``Redis.from_url(url, decode_responses=True)``
and nothing else: no ``socket_timeout``, no ``socket_connect_timeout``, no
``health_check_interval``, no ``retry_on_timeout``.

The distinction that matters is between a REFUSED connection and a HUNG one. A refused
connection raises immediately and was always handled; it is not the incident operators
actually get. The incident is a Redis that accepts the TCP connection and then never answers
-- a failover in progress, a paused primary, a blackholed route after a security-group change.
With no socket timeout, ``recv()`` blocks forever. Every rate-limit check runs on a Starlette
threadpool thread, so the pool is consumed one hung request at a time until the API stops
answering *anything*: a Redis outage becomes a total outage, and ``/health`` goes with it.
``client.ping()`` at construction was unbounded in the same way, so it could hang startup.

Two more things were needed beyond the timeout:

* **Re-latching on a mid-flight failure.** ``_REDIS_UNAVAILABLE`` was only ever set when the
  very first ping failed. If Redis died *after* a successful connect, every subsequent request
  re-entered the dead client and paid the full timeout before falling back. The timeout bounds
  one request; the latch is what bounds the incident.
* **A cooldown on that latch.** A permanent latch would leave a process on the per-replica
  fallback until someone restarted it, for a failover that resolved in seconds.

Also covered: the in-process fallback caches, which grew without ceiling *while Redis was
down* -- i.e. exactly when they fill fastest, in the components whose job is to survive it.
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from redis.exceptions import TimeoutError as RedisTimeoutError  # noqa: E402

import app.rate_limit as rate_limit  # noqa: E402
import app.runtime as runtime  # noqa: E402


class _HungRedis:
    """A client that accepts everything and answers nothing -- what a socket timeout is for.

    Real redis-py surfaces this as redis.exceptions.TimeoutError once socket_timeout is set;
    with no timeout it would simply never return, which a unit test cannot assert against
    without hanging the suite. Raising the exception the timeout produces is the faithful
    stand-in for "the timeout fired".
    """

    def __init__(self):
        self.calls = 0

    def _hang(self, *args, **kwargs):
        self.calls += 1
        raise RedisTimeoutError("Timeout reading from redis:6379")

    ping = _hang
    get = _hang
    setex = _hang
    incr = _hang
    evalsha = _hang
    script_load = _hang
    publish = _hang

    def close(self):
        pass


class RedisTimeoutTests(unittest.TestCase):
    def setUp(self):
        self._saved_url = os.environ.pop("REDIS_URL", None)
        runtime.reset_redis_client_for_tests()
        rate_limit.reset_local_state()
        rate_limit._LUA_SHA = None

    def tearDown(self):
        if self._saved_url is not None:
            os.environ["REDIS_URL"] = self._saved_url
        else:
            os.environ.pop("REDIS_URL", None)
        runtime.reset_redis_client_for_tests()
        rate_limit.reset_local_state()

    def test_the_client_is_built_with_bounded_socket_timeouts(self):
        os.environ["REDIS_URL"] = "redis://127.0.0.1:6399/0"
        with mock.patch("app.runtime.Redis.from_url") as from_url:
            from_url.return_value = mock.MagicMock()
            runtime.redis_client()
        self.assertTrue(from_url.called, "redis_client() did not build a client")
        kwargs = from_url.call_args.kwargs
        for required in ("socket_timeout", "socket_connect_timeout",
                         "health_check_interval", "retry_on_timeout"):
            self.assertIn(required, kwargs,
                          "{0} missing: a hung Redis blocks a threadpool thread forever".format(required))
        self.assertGreater(kwargs["socket_timeout"], 0)
        self.assertGreater(kwargs["socket_connect_timeout"], 0)
        self.assertTrue(kwargs["retry_on_timeout"])

    def test_a_hung_redis_is_latched_after_one_request_not_paid_on_every_one(self):
        hung = _HungRedis()
        os.environ["REDIS_URL"] = "redis://127.0.0.1:6399/0"
        runtime._REDIS_CLIENT = hung  # connected fine, then Redis died mid-flight
        runtime._REDIS_UNAVAILABLE = False

        tenant = {"id": "t-hung", "config": {"rate_limit_rpm": 100}}
        for _ in range(20):
            rate_limit.check(tenant, key="t-hung")

        self.assertLessEqual(hung.calls, 2,
                             "every request re-entered the dead client and paid the full socket "
                             "timeout; the latch only fires at construction, not mid-flight")
        self.assertTrue(runtime._REDIS_UNAVAILABLE)

    def test_the_limiter_still_enforces_while_redis_is_hung(self):
        """Failing over to the local bucket must not mean failing OPEN."""
        os.environ["REDIS_URL"] = "redis://127.0.0.1:6399/0"
        runtime._REDIS_CLIENT = _HungRedis()
        runtime._REDIS_UNAVAILABLE = False
        os.environ.pop("RATE_LIMIT_FAIL_OPEN", None)

        tenant = {"id": "t-enforce", "config": {"rate_limit_rpm": 3}}
        allowed = [rate_limit.check(tenant, key="t-enforce").allowed for _ in range(6)]
        self.assertEqual(allowed[:3], [True, True, True])
        self.assertIn(False, allowed[3:], "limits vanished during a Redis outage")

    def test_the_unavailable_latch_expires_so_a_failover_self_heals(self):
        os.environ["REDIS_RETRY_AFTER_SECONDS"] = "0.05"
        os.environ["REDIS_URL"] = "redis://127.0.0.1:6399/0"
        try:
            runtime._REDIS_CLIENT = _HungRedis()
            runtime.mark_redis_unavailable()
            self.assertIsNone(runtime.redis_client(), "should be latched immediately after failure")
            time.sleep(0.08)
            with mock.patch("app.runtime.Redis.from_url") as from_url:
                from_url.return_value = mock.MagicMock()
                self.assertIsNotNone(runtime.redis_client(),
                                     "the latch never expires, so a 5-second failover downgrades "
                                     "this process until it is restarted")
        finally:
            os.environ.pop("REDIS_RETRY_AFTER_SECONDS", None)

    def test_the_async_subscriber_client_is_also_bounded(self):
        """decision_bus builds its own client. An SSE subscriber against a blackholed Redis
        would otherwise hang in subscribe() forever, holding the task open."""
        from app import decision_bus

        timeouts = runtime.redis_timeouts()
        self.assertIn("socket_connect_timeout", timeouts)
        source = Path(decision_bus.__file__).read_text(encoding="utf-8")
        self.assertIn("redis_timeouts()", source,
                      "decision_bus.subscribe_decisions builds an async client with no timeouts")


class _EmptyRedis:
    """A live, healthy Redis that simply does not hold the key."""

    def get(self, key):
        return None

    def setex(self, *a, **kw):
        return True

    def ping(self):
        return True

    def close(self):
        pass


class CacheMissTests(unittest.TestCase):
    """A cache MISS against a live Redis used to raise, i.e. 500 the request.

    `cache_get_json` did `json.loads(client.get(key))`, and redis-py returns None for a key
    that is not there. `json.loads(None)` raises TypeError -- which is not a RedisError, so it
    sailed past the `except RedisError` below it and out to the caller. Every cold cache read
    was a 500. It survived because nothing in this repo ran against a real Redis until
    tests/test_redis_integration.py: with REDIS_URL unset the client is None and this branch
    never executes at all, so the whole suite passed.
    """

    def setUp(self):
        self._saved_url = os.environ.pop("REDIS_URL", None)
        runtime.reset_redis_client_for_tests()

    def tearDown(self):
        # The `else` branch is not optional. These tests SET REDIS_URL to a dead address; with
        # no saved value to restore, a bare `if` leaves that dead address in os.environ for the
        # rest of the process -- and /ready then tries to reach it and 503s. That is exactly the
        # leak this module's own docstring warns about, and it broke test_smoke in the full run
        # while passing in isolation.
        if self._saved_url is not None:
            os.environ["REDIS_URL"] = self._saved_url
        else:
            os.environ.pop("REDIS_URL", None)
        runtime.reset_redis_client_for_tests()

    def test_a_miss_returns_none_instead_of_raising(self):
        os.environ["REDIS_URL"] = "redis://127.0.0.1:6399/0"
        runtime._REDIS_CLIENT = _EmptyRedis()
        runtime._REDIS_UNAVAILABLE = False
        self.assertIsNone(runtime.cache_get_json("absent-key"))

    def test_a_corrupt_cached_value_is_a_miss_not_a_crash(self):
        os.environ["REDIS_URL"] = "redis://127.0.0.1:6399/0"

        class _Garbage(_EmptyRedis):
            def get(self, key):
                return "{not json"

        runtime._REDIS_CLIENT = _Garbage()
        runtime._REDIS_UNAVAILABLE = False
        self.assertIsNone(runtime.cache_get_json("corrupt-key"))


class FallbackCacheBoundsTests(unittest.TestCase):
    """The in-process fallbacks all grew without a ceiling while Redis was down."""

    def setUp(self):
        # Save/restore REDIS_URL rather than popping it: an unrestored pop leaks out of this
        # module and silently disables every later Redis-backed test in the same process --
        # exactly the "passed for the wrong reason" failure runtime.reset_redis_client_for_tests
        # was written to prevent.
        self._saved_url = os.environ.pop("REDIS_URL", None)
        runtime.reset_redis_client_for_tests()

    def tearDown(self):
        # The `else` branch is not optional. These tests SET REDIS_URL to a dead address; with
        # no saved value to restore, a bare `if` leaves that dead address in os.environ for the
        # rest of the process -- and /ready then tries to reach it and 503s. That is exactly the
        # leak this module's own docstring warns about, and it broke test_smoke in the full run
        # while passing in isolation.
        if self._saved_url is not None:
            os.environ["REDIS_URL"] = self._saved_url
        else:
            os.environ.pop("REDIS_URL", None)
        runtime.reset_redis_client_for_tests()

    def test_the_runtime_cache_is_bounded(self):
        self.assertTrue(hasattr(runtime._MEMORY_CACHE, "_capacity"),
                        "_MEMORY_CACHE is an unbounded dict; entries are only evicted lazily "
                        "when the SAME key is re-read after expiry, so a write-once key is "
                        "kept forever")

    def test_the_runtime_cache_actually_evicts(self):
        capacity = runtime._MEMORY_CACHE._capacity  # type: ignore[attr-defined]
        for i in range(capacity + 50):
            runtime.cache_set_json("k{0}".format(i), {"i": i}, ttl_seconds=300)
        self.assertLessEqual(len(runtime._MEMORY_CACHE), capacity)

    def test_the_local_token_bucket_is_bounded(self):
        self.assertTrue(hasattr(rate_limit._LOCAL._buckets, "_capacity"),
                        "_LocalTokenBucket._buckets keeps one entry per bucket key forever")

    def test_the_local_token_bucket_actually_evicts(self):
        bucket = rate_limit._LocalTokenBucket(16)
        for i in range(64):
            bucket.consume("key-{0}".format(i), rate=10.0, burst=10)
        self.assertLessEqual(len(bucket._buckets), 16)


if __name__ == "__main__":
    unittest.main()
