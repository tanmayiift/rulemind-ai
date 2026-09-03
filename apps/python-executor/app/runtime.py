from __future__ import annotations

import json
import os
import time
from collections import defaultdict, deque
from typing import Any, Optional

from redis import Redis
from redis.exceptions import RedisError


_REDIS_CLIENT: Optional[Redis] = None
_REDIS_UNAVAILABLE = False
_MEMORY_CACHE: dict[str, tuple[float, str]] = {}


class InMemoryRateLimiter:
    def __init__(self) -> None:
        self._buckets: dict[str, deque[float]] = defaultdict(deque)

    def allowed(self, key: str, limit: int, window_seconds: int = 60) -> tuple[bool, int]:
        now = time.time()
        bucket = self._buckets[key]
        while bucket and bucket[0] <= now - window_seconds:
            bucket.popleft()
        if len(bucket) >= limit:
            retry_after = max(1, int(window_seconds - (now - bucket[0])))
            return False, retry_after
        bucket.append(now)
        return True, 0


_MEMORY_LIMITER = InMemoryRateLimiter()


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
    global _REDIS_CLIENT, _REDIS_UNAVAILABLE
    try:
        if _REDIS_CLIENT is not None:
            _REDIS_CLIENT.close()
    except Exception:  # noqa: BLE001 - teardown must not fail a test
        pass
    _REDIS_CLIENT = None
    _REDIS_UNAVAILABLE = False
    _MEMORY_CACHE.clear()


def redis_client() -> Optional[Redis]:
    global _REDIS_CLIENT, _REDIS_UNAVAILABLE
    if _REDIS_UNAVAILABLE:
        return None
    if _REDIS_CLIENT is not None:
        return _REDIS_CLIENT
    url = os.getenv("REDIS_URL")
    if not url:
        _REDIS_UNAVAILABLE = True
        return None
    try:
        client = Redis.from_url(url, decode_responses=True)
        client.ping()
    except RedisError:
        _REDIS_UNAVAILABLE = True
        return None
    _REDIS_CLIENT = client
    return _REDIS_CLIENT


def cache_get_json(key: str) -> Optional[Any]:
    client = redis_client()
    if client is not None:
        try:
            raw = client.get(key)
            # redis-py types its return as the sync/async union; this module is sync-only.
            return json.loads(raw)  # type: ignore[arg-type]
        except RedisError:
            pass
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
            pass
    _MEMORY_CACHE[key] = (time.time() + ttl_seconds, payload)


def _rate_limit_fallback(key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
    """Used when Redis is unavailable (unconfigured, or errored mid-request). Falls back to the
    bounded per-replica in-memory limiter so limits STILL apply during a Redis outage — a blip
    must not silently remove all per-tenant limits (DoS / cost-blowout exposure). The per-replica
    cap means the effective global limit is roughly limit x replicas while Redis is down, which
    is bounded and vastly safer than unlimited. Operators who prefer availability over protection
    can opt into the old fail-open behaviour with RATE_LIMIT_FAIL_OPEN=1."""
    if (os.getenv("RATE_LIMIT_FAIL_OPEN", "") or "").strip() in ("1", "true", "yes"):
        return True, 0
    return _MEMORY_LIMITER.allowed(key, limit, window_seconds)


def rate_limit_allow(key: str, limit: int, window_seconds: int = 60) -> tuple[bool, int]:
    client = redis_client()
    if client is not None:
        try:
            bucket_key = "ratelimit:{0}:{1}".format(key, int(time.time() // window_seconds))
            count = int(client.incr(bucket_key))  # type: ignore[arg-type]
            if count == 1:
                client.expire(bucket_key, window_seconds + 1)
            if count > limit:
                # redis-py types one class for sync and async; this module is the sync client.
                ttl = int(client.ttl(bucket_key))  # type: ignore[arg-type]
                return False, max(1, ttl if ttl > 0 else window_seconds)
            return True, 0
        except RedisError:
            return _rate_limit_fallback(key, limit, window_seconds)
    # No Redis client (unconfigured, or connection failed) — fail closed to the local limiter.
    return _rate_limit_fallback(key, limit, window_seconds)
