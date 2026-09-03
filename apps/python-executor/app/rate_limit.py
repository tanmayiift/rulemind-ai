"""Tenant request rate limiting — configurable, and correct across replicas.

Design notes
------------
**Token bucket, not a fixed window.** A fixed counter bucket lets a caller spend
its whole allowance at the end of one window and again at the start of the next —
a 2x burst at every boundary. A token bucket refills continuously, so the long-run
rate is exactly the configured one while still permitting a deliberate burst.

**O(1) memory per tenant.** The bucket is two Redis fields (tokens, timestamp),
not one entry per request. That matters: a sliding-window log at 100k rpm would
hold 100k members per tenant. This holds two, at any limit.

**Atomic.** The refill-check-consume cycle runs inside a Lua script, so it is a
single round trip and cannot interleave between API replicas. Read-then-write from
Python would let two replicas both observe "1 token left" and both allow.

**Configuration** — most specific wins:

  1. per-tenant   tenant.config["rate_limit_rpm"]
  2. per-plan     RATE_LIMIT_RPM_<PLAN>     e.g. RATE_LIMIT_RPM_ENTERPRISE=20000
  3. global       RATE_LIMIT_RPM
  4. built-in     DEFAULT_PLAN_RPM

Set RATE_LIMIT_ENABLED=0 to switch limiting off entirely — that is the supported
way to run a load test or benchmark without the limiter shaping the result.
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from redis.exceptions import NoScriptError, RedisError

from .runtime import redis_client

# Built-in per-plan requests/minute, used only when nothing is configured.
DEFAULT_PLAN_RPM: Dict[str, int] = {
    "enterprise": 5000,
    "growth": 2000,
    "standard": 1000,
    "free": 120,
}
DEFAULT_RPM = 1000

# Never rate limit the endpoints that orchestrators poll to decide whether this
# process is alive. A 429 on /health reads as an outage to a load balancer.
DEFAULT_EXEMPT_PATHS = ("/health", "/ready", "/metrics", "/api/v1/health", "/sdk/v1/health")

_TRUTHY = {"1", "true", "yes", "on"}


def _env_int(name: str, default: Optional[int] = None) -> Optional[int]:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(float(raw))
    except ValueError:
        return default
    return value if value >= 0 else default


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def enabled() -> bool:
    raw = (os.getenv("RATE_LIMIT_ENABLED", "1") or "").strip().lower()
    return raw in _TRUTHY


def exempt_paths() -> Tuple[str, ...]:
    raw = (os.getenv("RATE_LIMIT_EXEMPT_PATHS", "") or "").strip()
    if not raw:
        return DEFAULT_EXEMPT_PATHS
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def is_exempt(path: str) -> bool:
    return any(path == p or path.startswith(p.rstrip("/") + "/") for p in exempt_paths())


def resolve_limit(tenant: Optional[Dict[str, Any]]) -> int:
    """Requests per minute allowed for this tenant."""
    tenant = tenant or {}
    config = tenant.get("config") or {}
    if isinstance(config, dict):
        override = config.get("rate_limit_rpm")
        if override is not None:
            try:
                parsed = int(override)
                if parsed > 0:
                    return parsed
            except (TypeError, ValueError):
                pass

    plan = str(tenant.get("plan") or "standard").strip().lower()
    per_plan = _env_int("RATE_LIMIT_RPM_{0}".format(plan.upper().replace("-", "_")))
    if per_plan:
        return per_plan

    global_rpm = _env_int("RATE_LIMIT_RPM")
    if global_rpm:
        return global_rpm

    return DEFAULT_PLAN_RPM.get(plan, DEFAULT_RPM)


def resolve_burst(limit: int) -> int:
    """Bucket capacity. A multiplier > 1 tolerates short spikes without raising
    the sustained rate; the default keeps burst == limit."""
    multiplier = _env_float("RATE_LIMIT_BURST_MULTIPLIER", 1.0)
    return max(1, int(math.ceil(limit * multiplier)))


@dataclass
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int
    retry_after: int
    enforced: bool = True

    def headers(self) -> Dict[str, str]:
        """RateLimit-* per the IETF draft, so clients can self-throttle instead of
        discovering the ceiling by being rejected at it."""
        h = {
            "RateLimit-Limit": str(self.limit),
            "RateLimit-Remaining": str(max(0, self.remaining)),
            "RateLimit-Reset": str(max(0, self.reset_seconds)),
        }
        if not self.allowed:
            h["Retry-After"] = str(max(1, self.retry_after))
        return h


ALLOW_ALL = RateLimitDecision(allowed=True, limit=0, remaining=0, reset_seconds=0,
                              retry_after=0, enforced=False)

# KEYS[1] bucket   ARGV: rate(tokens/sec) burst now(sec) cost
_LUA_TOKEN_BUCKET = """
local key   = KEYS[1]
local rate  = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local now   = tonumber(ARGV[3])
local cost  = tonumber(ARGV[4])

local state  = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts     = tonumber(state[2])
if tokens == nil or ts == nil then
  tokens = burst
  ts = now
end

local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
tokens = math.min(burst, tokens + elapsed * rate)

local allowed = 0
local retry = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  retry = (cost - tokens) / rate
end

redis.call('HSET', key, 'tokens', tokens, 'ts', now)
-- Idle buckets expire once they would have refilled completely.
redis.call('EXPIRE', key, math.ceil(burst / rate) + 60)

return {allowed, tostring(tokens), tostring(retry)}
"""

_LUA_SHA: Optional[str] = None


class _LocalTokenBucket:
    """Per-process fallback used when Redis is unavailable. Same algorithm, so
    behaviour is consistent; the effective global ceiling becomes limit x replicas,
    which is bounded and far safer than removing limits during a Redis blip."""

    def __init__(self) -> None:
        self._buckets: Dict[str, Tuple[float, float]] = {}

    def consume(self, key: str, rate: float, burst: int, cost: float = 1.0) -> Tuple[bool, float, float]:
        now = time.monotonic()
        tokens, ts = self._buckets.get(key, (float(burst), now))
        tokens = min(float(burst), tokens + max(0.0, now - ts) * rate)
        if tokens >= cost:
            tokens -= cost
            self._buckets[key] = (tokens, now)
            return True, tokens, 0.0
        retry = (cost - tokens) / rate if rate > 0 else 1.0
        self._buckets[key] = (tokens, now)
        return False, tokens, retry

    def reset(self) -> None:
        self._buckets.clear()


_LOCAL = _LocalTokenBucket()


def reset_local_state() -> None:
    """Test helper — clears the in-process buckets."""
    _LOCAL.reset()


def _fail_open() -> bool:
    return (os.getenv("RATE_LIMIT_FAIL_OPEN", "") or "").strip().lower() in _TRUTHY


def check(tenant: Optional[Dict[str, Any]], *, key: Optional[str] = None,
          cost: float = 1.0) -> RateLimitDecision:
    """Consume one unit of a tenant's allowance and report the outcome."""
    if not enabled():
        return ALLOW_ALL

    tenant = tenant or {}
    bucket_key = key or str(tenant.get("id") or "anonymous")
    limit = resolve_limit(tenant)
    burst = resolve_burst(limit)
    window = _env_float("RATE_LIMIT_WINDOW_SECONDS", 60.0)
    rate = limit / window if window > 0 else float(limit)
    if rate <= 0:
        return ALLOW_ALL

    allowed, tokens, retry = _consume(bucket_key, rate, burst, cost)
    if allowed is None:  # Redis error and operator chose availability
        return ALLOW_ALL

    # Seconds until the bucket is full again — what "reset" means for a bucket.
    reset_seconds = int(math.ceil(max(0.0, (burst - tokens)) / rate)) if rate > 0 else 0
    return RateLimitDecision(
        allowed=bool(allowed),
        limit=limit,
        remaining=int(tokens),
        reset_seconds=reset_seconds,
        retry_after=int(math.ceil(retry)) if retry else 0,
    )


def _consume(key: str, rate: float, burst: int, cost: float):
    global _LUA_SHA
    client = redis_client()
    if client is not None:
        redis_key = "ratelimit:tb:{0}".format(key)
        now = time.time()
        try:
            if _LUA_SHA is None:
                _LUA_SHA = client.script_load(_LUA_TOKEN_BUCKET)
            try:
                res = client.evalsha(_LUA_SHA, 1, redis_key, rate, burst, now, cost)
            except NoScriptError:
                # The server was flushed or failed over -- reload the script once.
                #
                # This used to match on `"NOSCRIPT" in str(exc).upper()`, which NEVER
                # matched: redis-py raises NoScriptError with the message "No matching
                # script. Please use EVAL." -- the word NOSCRIPT appears nowhere in it. So
                # the guard re-raised, the outer handler dropped the SHA, and every
                # rate-limit check silently fell back to the per-process local bucket,
                # losing the cross-replica atomicity this Lua script exists for, with no
                # error surfaced anywhere. Matching the exception TYPE cannot drift with
                # the server's wording. Found by tests/test_redis_integration.py, which is
                # the first thing in this repo to run against a real Redis.
                _LUA_SHA = client.script_load(_LUA_TOKEN_BUCKET)
                res = client.evalsha(_LUA_SHA, 1, redis_key, rate, burst, now, cost)
            return int(res[0]), float(res[1]), float(res[2])
        except RedisError:
            _LUA_SHA = None
            if _fail_open():
                return None, 0.0, 0.0
            return _LOCAL.consume(key, rate, burst, cost)

    if _fail_open():
        return None, 0.0, 0.0
    return _LOCAL.consume(key, rate, burst, cost)
