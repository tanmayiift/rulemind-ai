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
    return os.getenv("NODE_ENV", "development") == "development" or os.getenv("PYTEST_CURRENT_TEST") is not None


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
            return json.loads(raw) if raw else None
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


def rate_limit_allow(key: str, limit: int, window_seconds: int = 60) -> tuple[bool, int]:
    client = redis_client()
    if client is not None:
        try:
            bucket_key = "ratelimit:{0}:{1}".format(key, int(time.time() // window_seconds))
            count = int(client.incr(bucket_key))
            if count == 1:
                client.expire(bucket_key, window_seconds + 1)
            if count > limit:
                ttl = int(client.ttl(bucket_key))
                return False, max(1, ttl if ttl > 0 else window_seconds)
            return True, 0
        except RedisError:
            if is_local_dev():
                return _MEMORY_LIMITER.allowed(key, limit, window_seconds)
            return True, 0
    if not is_local_dev() and os.getenv("REDIS_URL"):
        return True, 0
    return _MEMORY_LIMITER.allowed(key, limit, window_seconds)
