from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

from redis import Redis
from redis.exceptions import RedisError

from . import env
from .sandbox import _BoundedCache


_REDIS_CLIENT: Optional[Redis] = None
_REDIS_UNAVAILABLE = False
# monotonic deadline after which a latched-unavailable Redis is re-dialled once. Without
# this the latch is permanent, so a 5-second failover would downgrade the process to the
# per-replica fallback until someone restarted it.
_REDIS_RETRY_AT = 0.0

# Bounded, not a plain dict. This is the fallback used while Redis is unreachable, i.e.
# exactly when it fills fastest, and entries were only ever evicted lazily when the SAME
# key was read back after expiry — a key written once and never re-read stayed forever.
# A multi-hour Redis outage on a busy tenant was therefore an unbounded memory leak in the
# component whose job is to survive that outage. `_BoundedCache` is the one bounded cache
# in this tree (sandbox.py); reusing it beats a third implementation. TTL still applies on
# read; the ceiling only decides what is dropped first when the cache is full.
_MEMORY_CACHE_CAPACITY = env.env_int("RUNTIME_CACHE_MAX_ENTRIES", 4096)
_MEMORY_CACHE: dict[str, tuple[float, str]] = _BoundedCache(_MEMORY_CACHE_CAPACITY)

# Socket timeouts for the shared Redis client.
#
# A REFUSED connection fails fast and was always handled. The incident that hurts is a
# HUNG Redis — a failover mid-flight, a blackholed route, a paused primary — where the
# TCP connection stays open and the server simply never answers. With no socket_timeout,
# redis-py blocks in recv() forever. Every rate-limit check runs on a Starlette threadpool
# thread, so the whole pool is consumed one hung request at a time until the API stops
# answering anything at all, /health included. These bound that to seconds.
_DEFAULT_SOCKET_TIMEOUT = 2.0
_DEFAULT_CONNECT_TIMEOUT = 2.0
_DEFAULT_HEALTH_CHECK_INTERVAL = 30


def redis_timeouts() -> dict[str, Any]:
    """Connection kwargs shared by the sync client here and the async client in decision_bus,
    so the two cannot drift into one being bounded and the other not."""
    return {
        "socket_timeout": env.env_float("REDIS_SOCKET_TIMEOUT", _DEFAULT_SOCKET_TIMEOUT),
        "socket_connect_timeout": env.env_float("REDIS_CONNECT_TIMEOUT", _DEFAULT_CONNECT_TIMEOUT),
        # Ping an idle pooled connection before reusing it: a connection silently dropped by
        # a failover or an idle-timeout middlebox otherwise surfaces as one failed command.
        "health_check_interval": env.env_int("REDIS_HEALTH_CHECK_INTERVAL", _DEFAULT_HEALTH_CHECK_INTERVAL),
        # Retry once on a timeout. A single blip should not cost a rate-limit decision;
        # more than one retry just multiplies the worst-case latency by the retry count.
        "retry_on_timeout": True,
    }


def mark_redis_unavailable() -> None:
    """Latch the fallback after a MID-FLIGHT failure, not only at construction.

    Before this, `_REDIS_UNAVAILABLE` was set only when the very first `ping()` failed. If
    Redis died *after* a successful connect, every subsequent request re-entered the same
    dead client, paid the full socket timeout, and only then fell back — turning a Redis
    outage into an API-wide latency outage. Latching on the first mid-flight error means
    the second request onward takes the in-memory path immediately.
    """
    global _REDIS_CLIENT, _REDIS_UNAVAILABLE, _REDIS_RETRY_AT
    client, _REDIS_CLIENT = _REDIS_CLIENT, None
    _REDIS_UNAVAILABLE = True
    _REDIS_RETRY_AT = time.monotonic() + env.env_float("REDIS_RETRY_AFTER_SECONDS", 30.0)
    if client is not None:
        try:
            client.close()
        except Exception:  # noqa: BLE001 - closing a broken client must not raise
            pass


def is_local_dev() -> bool:
    """Delegates to security_config so the two cannot drift.

    This was a byte-identical copy of security_config._is_local_dev, which meant fixing the
    NODE_ENV fail-open gap in one place would silently leave the other behind.
    """
    from .security_config import _is_local_dev

    return _is_local_dev(os.environ)


def reset_redis_client_for_tests() -> None:
    """Drop the memoized client and the unavailable latch.

    Named for tests so nothing calls it from a request path. It exists because
    `_REDIS_UNAVAILABLE` latches True permanently the first time redis_client() runs without
    REDIS_URL set -- and several suites deliberately unset REDIS_URL to exercise the
    no-Redis fallback. In one `unittest discover` process, whichever ran first decided the
    answer for every test after it, so a Redis-backed test could silently take the fallback
    path and pass for the wrong reason. There was no way to undo that latch.
    """
    global _REDIS_CLIENT, _REDIS_UNAVAILABLE, _REDIS_RETRY_AT
    try:
        if _REDIS_CLIENT is not None:
            _REDIS_CLIENT.close()
    except Exception:  # noqa: BLE001 - teardown must not fail a test
        pass
    _REDIS_CLIENT = None
    _REDIS_UNAVAILABLE = False
    _REDIS_RETRY_AT = 0.0
    _MEMORY_CACHE.clear()


def redis_client() -> Optional[Redis]:
    global _REDIS_CLIENT, _REDIS_UNAVAILABLE
    if _REDIS_UNAVAILABLE:
        # Time-boxed, not permanent: after the cooldown, try once more. A failover that
        # resolves in seconds must not leave this process on the fallback until restart.
        if time.monotonic() < _REDIS_RETRY_AT:
            return None
        _REDIS_UNAVAILABLE = False
    if _REDIS_CLIENT is not None:
        return _REDIS_CLIENT
    url = env.env_opt("REDIS_URL")
    if not url:
        # Deliberately does NOT latch. `_REDIS_UNAVAILABLE` exists to stop a *failing* Redis
        # from being re-dialled on every request; "not configured" is not a failure, and
        # latching it meant one early call before the config was read (or with REDIS_URL="")
        # permanently pinned the whole process to the in-memory fallback — per-replica rate
        # limits and no cross-replica decision bus — with no way back short of a restart.
        return None
    try:
        # decode_responses only; no timeouts at all is what this used to be. `ping()` below
        # was likewise unbounded, so a hung Redis blocked *startup-time* callers forever too.
        client = Redis.from_url(url, decode_responses=True, **redis_timeouts())
        client.ping()
    except RedisError:
        mark_redis_unavailable()
        return None
    _REDIS_CLIENT = client
    return _REDIS_CLIENT


def cache_get_json(key: str) -> Optional[Any]:
    client = redis_client()
    if client is not None:
        try:
            raw = client.get(key)
            # A CACHE MISS returns None, and `json.loads(None)` raises TypeError -- which is
            # not a RedisError, so it escaped the handler below and propagated to the caller.
            # Every miss against a live Redis was a 500. It went unnoticed because until
            # tests/test_redis_integration.py nothing in this repo ran against a real server:
            # with no Redis configured the client is None and this branch never executes.
            if raw is None:
                return None
            # redis-py types its return as the sync/async union; this module is sync-only.
            return json.loads(raw)  # type: ignore[arg-type]
        except ValueError:
            # Corrupt / non-JSON cached value (JSONDecodeError is a ValueError): treat it as a
            # miss rather than failing the caller. Deliberately NOT catching TypeError as well:
            # after the None guard above, a TypeError here would mean redis-py returned
            # something other than a string, which is a real bug worth surfacing rather than
            # swallowing — and it keeps the two guards independently testable.
            return None
        except RedisError:
            # Includes TimeoutError (a RedisError subclass): the socket timeout above turns a
            # hung server into an exception, and this latch stops the next request paying it
            # again. Without the latch the timeout only bounds ONE request, not the outage.
            mark_redis_unavailable()
    item = _MEMORY_CACHE.get(key)
    if not item:
        return None
    expires_at, payload = item
    if expires_at < time.time():
        _MEMORY_CACHE.pop(key, None)
        return None
    return json.loads(payload)


def cache_set_json(key: str, value: Any, ttl_seconds: int = 300) -> None:
    payload = json.dumps(value)
    client = redis_client()
    if client is not None:
        try:
            client.setex(key, ttl_seconds, payload)
            return
        except RedisError:
            mark_redis_unavailable()
    _MEMORY_CACHE[key] = (time.time() + ttl_seconds, payload)

# `InMemoryRateLimiter`, `_MEMORY_LIMITER`, `_rate_limit_fallback` and `rate_limit_allow`
# used to live here: a fixed-window counter limiter with its own unbounded
# defaultdict(deque) of buckets — one entry per key, forever, never evicted.
#
# It was DEAD CODE. The middleware calls `rate_limit.check()` (app/rate_limit.py), which is
# the token-bucket implementation with the Lua script and the bounded local fallback; nothing
# in app/ ever called `rate_limit_allow`. Its only callers were two tests, which therefore
# asserted the fail-closed property of a limiter that served no request. Deleted rather than
# bounded: two limiters with different algorithms is how the tested one and the shipped one
# drift apart. (HLD.md:99 still names `rate_limit_allow` — reported for a docs fix.)
