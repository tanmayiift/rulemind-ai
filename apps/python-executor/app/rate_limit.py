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

**Two budgets per request, not one.** A single tenant-wide bucket makes every endpoint
fungible: a dashboard tab polling `GET /api/v1/policies` in a loop spends the same
allowance as `/decide`, so a misbehaving browser can starve that tenant's production
decisioning. The converse is just as wrong — a decision limit sized for machine traffic
hands the same generosity to authoring writes and to `/api/v1/ai`, which spends real
money at a paid provider on every call. So a request draws from:

  1. the bucket for its RESOURCE CLASS (see `classify`), and
  2. the tenant-wide aggregate, which stays as an OUTER bound.

The aggregate is not redundant. The class shares deliberately sum to more than 1.0 (a
tenant that only ever decides should get its whole budget for deciding), so without an
aggregate a client could run every class at its maximum at once and exceed the limit the
operator actually configured. See `DEFAULT_CLASS_SHARE`.

**Configuration** — most specific wins:

  aggregate (per tenant)          per class
  ------------------------------  --------------------------------------------
  1. tenant.config["rate_limit_rpm"]     tenant.config["rate_limit_class_rpm"][class]
  2. RATE_LIMIT_RPM_<PLAN>               RATE_LIMIT_RPM_CLASS_<CLASS>   (absolute rpm)
  3. RATE_LIMIT_RPM                      RATE_LIMIT_CLASS_SHARE_<CLASS> (x aggregate)
  4. DEFAULT_PLAN_RPM                    DEFAULT_CLASS_SHARE            (x aggregate)

Set RATE_LIMIT_ENABLED=0 to switch limiting off entirely — that is the supported
way to run a load test or benchmark without the limiter shaping the result.
RATE_LIMIT_PER_CLASS=0 collapses back to the single tenant-wide bucket;
RATE_LIMIT_AGGREGATE=0 keeps only the per-class buckets (and with it, a real ceiling of
sum(shares) x the configured limit — read DEFAULT_CLASS_SHARE before setting it).
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from redis.exceptions import NoScriptError, RedisError

from .runtime import mark_redis_unavailable, redis_client
from .sandbox import _BoundedCache

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


# ── Resource classes ───────────────────────────────────────────────────────────
#
# The set is deliberately small and closed. Every authenticated request lands in exactly
# one of these, and each gets its own bucket, so exhausting one cannot reject requests in
# another.
DECISION = "decision"          # run a policy / rule / model — the product
READ = "read"                  # any GET the dashboard or a client polls
WRITE = "write"                # authoring mutations
AI = "ai"                      # /api/v1/ai — a paid provider call, see app/ai.py
EXPORT = "export"              # exports, reports, analytics, audit scans
ADMIN = "admin"                # keys, roles, settings, provisioning, promotions
UNCLASSIFIED = "unclassified"  # nothing matched — see classify()

# The aggregate bucket's scope name. Not a class: it is the outer bound over all of them.
TENANT = "tenant"

RESOURCE_CLASSES: Tuple[str, ...] = (DECISION, READ, WRITE, AI, EXPORT, ADMIN, UNCLASSIFIED)

# Each class's share of the tenant's configured limit.
#
# Chosen relative to the aggregate rather than as absolute numbers so that raising a
# tenant's plan raises every budget with it, and so a `free` tenant is not handed an
# enterprise-shaped AI allowance.
#
#   decision 1.00 — the product. A tenant that does nothing but decide should be able to
#                   spend its entire configured limit deciding; the whole point of the
#                   split is that reads can no longer take this away.
#   read     0.50 — dashboards poll. Generous, but structurally unable to starve decisions.
#   write    0.25 — authoring is human-paced; 250/min at the standard plan is far above
#                   any editor and still bounds a runaway sync loop.
#   admin    0.10 — key/role/settings changes are rare. A tight bucket also bounds the
#                   blast radius of a stolen admin credential.
#   export   0.10 — each one is a table scan or a report render, i.e. seconds of DB time.
#   ai       0.05 — the only class that spends money at a third party on every request
#                   (ai.estimate_cost: up to $5/$25 per MTok for Opus, $15/$75 for o1).
#                   20x tighter than decisions, because a stuck retry loop here shows up
#                   on the customer's provider invoice, not just in our latency graph.
#   unclassified 0.05 — see classify(): unmapped writes land here on purpose.
#
# These sum to 2.05, which is why the aggregate bucket stays: it is what stops a client
# running all seven flat out at the same time.
#
# One consequence worth stating, because it is a choice and not an oversight: decision at
# 1.00 means saturating decisions also saturates the aggregate, so at 100% of plan the
# dashboard is refused too (with scope=TENANT — the read bucket itself is still full). The
# alternative is a tenant unable to spend the rate it is paying for. An operator who wants
# guaranteed headroom for their own console during saturation sets
# RATE_LIMIT_CLASS_SHARE_DECISION below 1.0 and keeps the difference in reserve.
DEFAULT_CLASS_SHARE: Dict[str, float] = {
    DECISION: 1.00,
    READ: 0.50,
    WRITE: 0.25,
    ADMIN: 0.10,
    EXPORT: 0.10,
    AI: 0.05,
    UNCLASSIFIED: 0.05,
}

# A share of a small plan rounds toward nothing (5% of the free plan's 120 is 6/min, and a
# tenant-level override of 10 would leave AI at 1). No derived class budget goes below this
# — an explicit absolute override still does, because that is an operator saying so.
CLASS_FLOOR_RPM = 5


def _under(path: str, prefixes: Tuple[str, ...]) -> bool:
    """Prefix match on whole path segments.

    A bare `str.startswith` would file `/api/v1/rulesets` under `/api/v1/rules`, and file
    `/api/v1/aiming` under `/api/v1/ai` — i.e. quietly hand a new endpoint the tightest
    budget in the table because of a shared substring. `is_exempt` already matches this way.
    """
    return any(path == p or path.startswith(p.rstrip("/") + "/") for p in prefixes)


# Path-prefix → class. Ordered most specific first, the same shape as rbac._WRITE_PREFIX_CAPABILITY.
_AI_PREFIXES = ("/api/v1/ai",)

_ADMIN_PREFIXES = (
    "/api/v1/access",
    "/api/v1/settings",
    "/api/v1/branding",
    "/api/v1/deploy",
    "/api/v1/promotions",
    "/scim/v2",
    # Listed for completeness of the MAP, not as the enforcement point. The middleware
    # returns before this check for all three, and check_pre_auth_rate_limit already gives
    # them their own IP-keyed budget (there is no tenant yet to key one on). Mapping them
    # means a sibling route added later inherits ADMIN instead of UNCLASSIFIED.
    "/api/admin/v1",
    "/api/v1/auth",
    "/api/mobile/v1/auth",
)

_EXPORT_PREFIXES = (
    "/api/v1/export",
    "/api/v1/import",
    "/api/v1/reports",
    "/api/v1/analytics",
    "/api/v1/audit",
    "/api/v1/insights",
)

_DECISION_PREFIXES = (
    "/api/v1/decide",
    "/api/v1/simulate",
    "/api/v1/test",
    "/api/v1/decisions",
    "/api/v1/executions",
    "/api/v1/workflows",
    "/sdk/v1",
)

_AUTHORING_PREFIXES = (
    "/api/v1/connectors",
    "/api/v1/variables",
    "/api/v1/rules",
    "/api/v1/scorecards",
    "/api/v1/policies",
    "/api/v1/decision-tables",
    "/api/v1/experiments",
    "/api/v1/schedules",
    "/api/v1/webhooks",
    "/api/v1/bundles",
    "/api/v1/segments",
    "/api/v1/models",
    "/api/v1/collab",
    "/api/v1/evaluations",
    "/api/v1/reviews",
    "/api/v1/onboarding",
    "/api/v1/providers",
    "/api/v1/bootstrap",
    "/api/v1/usage",
    "/api/v1/slo",
    "/api/mobile/v1",
)

# Evaluation subroutes that live under an authoring URL. `/policies/{id}/execute` and
# `/models/{id}/predict` run the engine; charging them to the authoring budget would put the
# expensive half of authoring in the cheap bucket.
_EVALUATION_SUFFIXES = (
    "/execute", "/evaluate", "/predict", "/test", "/test-draft", "/backtest",
    "/whatif", "/shadow", "/replay", "/run-now", "/analyze", "/analyze-mece",
)

# ...but only under the prefixes that actually host them. `/connectors/{id}/test` opens an
# outbound connection to a customer data source: expensive, yes, but billing a dashboard's
# "test connection" button to the DECISION budget is exactly the starvation this split
# exists to prevent. It stays authoring.
_EVALUATION_HOSTS = (
    "/api/v1/policies",
    "/api/v1/rules",
    "/api/v1/scorecards",
    "/api/v1/variables",
    "/api/v1/decision-tables",
    "/api/v1/models",
    "/api/v1/experiments",
    "/api/v1/schedules",
    "/api/v1/evaluations",
)

_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def classify(method: str, path: str) -> str:
    """The budget a request draws from.

    Modelled on `rbac.required_capability`, including the part that matters most: an
    UNMAPPED WRITE does not fall through to something generous. rbac was changed to return
    MANAGE_ACCESS for unknown writes after `/scim/v2` reached `policy_maker` purely by
    falling off the end of the table; the same omission here would give a new mutating
    endpoint the largest budget in the map, or (worse, if the map were consulted only for
    known prefixes) no budget at all. Unknown writes land in UNCLASSIFIED, which has its
    own bucket at the tightest share — tight enough that a missing entry surfaces as a 429
    in development, separate enough that it cannot drain a real class.

    Unknown READS land in READ, matching rbac's first line (`GET → READ`). A GET has no
    side effects, and the aggregate bucket still bounds it.
    """
    if _under(path, _AI_PREFIXES):
        return AI
    if _under(path, _ADMIN_PREFIXES):
        return ADMIN
    if _under(path, _EXPORT_PREFIXES):
        return EXPORT
    if _under(path, _DECISION_PREFIXES):
        return DECISION
    if _under(path, _EVALUATION_HOSTS) and path.endswith(_EVALUATION_SUFFIXES):
        return DECISION
    if (method or "").upper() in _SAFE_METHODS:
        return READ
    if _under(path, _AUTHORING_PREFIXES):
        return WRITE
    return UNCLASSIFIED


def per_class_enabled() -> bool:
    return (os.getenv("RATE_LIMIT_PER_CLASS", "1") or "").strip().lower() in _TRUTHY


def aggregate_enabled() -> bool:
    """Whether the tenant-wide bucket still applies on top of the per-class ones.

    Default on. Turning it off raises the true ceiling from the configured limit to
    sum(DEFAULT_CLASS_SHARE) x that limit, because nothing then stops a client from running
    every class at its own maximum simultaneously.
    """
    return (os.getenv("RATE_LIMIT_AGGREGATE", "1") or "").strip().lower() in _TRUTHY


def resolve_class_limit(tenant: Optional[Dict[str, Any]], resource_class: str) -> int:
    """Requests per minute this tenant may spend on one resource class."""
    tenant = tenant or {}
    config = tenant.get("config") or {}
    if isinstance(config, dict):
        per_class = config.get("rate_limit_class_rpm")
        if isinstance(per_class, dict):
            try:
                override = int(per_class.get(resource_class))  # type: ignore[arg-type]
                if override > 0:
                    return override
            except (TypeError, ValueError):
                pass

    suffix = resource_class.upper().replace("-", "_")
    absolute = _env_int("RATE_LIMIT_RPM_CLASS_{0}".format(suffix))
    if absolute:
        return absolute

    share = _env_float("RATE_LIMIT_CLASS_SHARE_{0}".format(suffix),
                       DEFAULT_CLASS_SHARE.get(resource_class, 1.0))
    return max(CLASS_FLOOR_RPM, int(math.ceil(resolve_limit(tenant) * share)))


# ── Request cost ───────────────────────────────────────────────────────────────
#
# A batch of N payloads is N decisions; charging it one token would let a caller buy the
# whole budget's worth of work for a single request and make the per-class split cosmetic.
#
# Cost is derived from Content-Length, NOT from parsing the body. This runs inside the
# outermost auth middleware: reading the body there would force every bulk upload to be
# buffered before it reaches the handler that streams it (/decide/batch/jsonl exists to
# take large files), turning a limiter into a memory amplifier. Content-Length is free, and
# the ASGI server reads exactly that many bytes, so under-declaring it truncates the very
# request the caller wanted to send.
#
# A chunked upload declares no length and is charged _BULK_BASE_COST — hence the base being
# meaningfully above 1 rather than a rounding detail.
DEFAULT_BATCH_UNIT_BYTES = 512   # ~ one small decision payload
DEFAULT_MAX_REQUEST_COST = 100.0
_BULK_BASE_COST = 4.0

_BULK_PREFIXES = (
    "/api/v1/decide/batch",   # and /decide/batch/jsonl beneath it
    "/api/v1/test/batch",
    "/api/v1/test/variables",
    "/api/v1/import",
    "/sdk/v1/decisions",
    "/sdk/v1/events",
)
_BULK_SUFFIXES = ("/batch", "/backtest")


def request_cost(method: str, path: str, content_length: Any = None) -> float:
    """Tokens one request consumes. 1.0 unless the endpoint processes N items per call."""
    if (method or "").upper() in _SAFE_METHODS:
        return 1.0
    if not (_under(path, _BULK_PREFIXES) or path.endswith(_BULK_SUFFIXES)):
        return 1.0
    try:
        size = max(0, int(content_length or 0))
    except (TypeError, ValueError):
        size = 0
    unit = _env_int("RATE_LIMIT_BATCH_UNIT_BYTES", DEFAULT_BATCH_UNIT_BYTES) or DEFAULT_BATCH_UNIT_BYTES
    cap = _env_float("RATE_LIMIT_MAX_REQUEST_COST", DEFAULT_MAX_REQUEST_COST)
    return min(cap, max(_BULK_BASE_COST, 1.0 + size / float(unit)))


@dataclass
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int
    retry_after: int
    enforced: bool = True
    # Which budget these numbers describe: a resource class, TENANT for the aggregate, or
    # "preauth:<surface>" for the IP-keyed pre-auth buckets. Empty when nothing was enforced.
    scope: str = ""

    def headers(self) -> Dict[str, str]:
        """RateLimit-* per the IETF draft, so clients can self-throttle instead of
        discovering the ceiling by being rejected at it."""
        h = {
            "RateLimit-Limit": str(self.limit),
            "RateLimit-Remaining": str(max(0, self.remaining)),
            "RateLimit-Reset": str(max(0, self.reset_seconds)),
        }
        if self.scope:
            # Non-standard, and deliberately so. The draft's RateLimit-* triple describes ONE
            # budget, but a request now passes through two, so the triple alone is ambiguous:
            # "Remaining: 3" is unactionable if the client cannot tell whether that is three
            # more decisions or three more of everything. This names the budget the numbers
            # above belong to, and it is the same string that appears in the 429 body.
            h["RateLimit-Scope"] = self.scope
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
    which is bounded and far safer than removing limits during a Redis blip.

    **Bounded.** `_buckets` was a plain dict: one entry per bucket key, kept forever.
    Bucket keys are tenant ids by default, but `check(key=...)` accepts an arbitrary
    key, and this dict fills precisely during a Redis outage — the scenario it exists
    to survive. Now it is a `_BoundedCache` (the same one sandbox.py uses for its code
    caches), so the fallback has a hard memory ceiling.

    Eviction cost is correct-by-construction here: an evicted bucket is recreated full,
    which is the same state an idle bucket refills to. Insertion-order eviction therefore
    at worst forgives one burst for the least-recently-*created* key, never over-limits.
    """

    def __init__(self, capacity: int) -> None:
        self._buckets: Dict[str, Tuple[float, float]] = _BoundedCache(capacity)

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


# One entry is two floats; 50k keys is a few MB. Large enough that a real tenant
# population never evicts, small enough to be a ceiling rather than a leak.
_LOCAL_BUCKET_CAPACITY = _env_int("RATE_LIMIT_LOCAL_BUCKETS", 50000) or 50000
_LOCAL = _LocalTokenBucket(_LOCAL_BUCKET_CAPACITY)


def reset_local_state() -> None:
    """Test helper — clears the in-process buckets."""
    _LOCAL.reset()


def _fail_open() -> bool:
    return (os.getenv("RATE_LIMIT_FAIL_OPEN", "") or "").strip().lower() in _TRUTHY


def _bucket_decision(bucket_key: str, limit: int, cost: float, scope: str) -> RateLimitDecision:
    """Consume `cost` from one named bucket and report the outcome."""
    burst = resolve_burst(limit)
    window = _env_float("RATE_LIMIT_WINDOW_SECONDS", 60.0)
    rate = limit / window if window > 0 else float(limit)
    if rate <= 0:
        return ALLOW_ALL

    # A cost larger than the bucket can ever hold is not a rate limit, it is a permanent 429:
    # `tokens` never reaches `cost`, so the request is refused however long the caller waits
    # and retry_after only grows. Clamping means one bulk request may drain a whole budget,
    # but it can always eventually be served.
    cost = max(0.0, min(float(cost), float(burst)))

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
        scope=scope,
    )


def check(tenant: Optional[Dict[str, Any]], *, key: Optional[str] = None,
          cost: float = 1.0, scope: str = TENANT) -> RateLimitDecision:
    """Consume one unit of a tenant's AGGREGATE allowance and report the outcome.

    Still the entry point for callers that have no resource class to speak of — the
    IP-keyed pre-auth buckets in middleware.py pass their own `key` and `scope`.
    Authenticated request handling goes through `check_request` instead.
    """
    if not enabled():
        return ALLOW_ALL
    tenant = tenant or {}
    bucket_key = key or str(tenant.get("id") or "anonymous")
    return _bucket_decision(bucket_key, resolve_limit(tenant), cost, scope)


def check_request(tenant: Optional[Dict[str, Any]], method: str, path: str, *,
                  cost: float = 1.0, key: Optional[str] = None) -> RateLimitDecision:
    """Rate-limit one authenticated request against its resource class AND the aggregate.

    Returns the BINDING decision — the budget with the least headroom, i.e. the one that
    will reject this caller first. `.scope` (and the RateLimit-Scope header) names it, so a
    client reading `Remaining` knows what it is remaining *of*.
    """
    if not enabled() or is_exempt(path):
        return ALLOW_ALL

    tenant = tenant or {}
    base_key = key or str(tenant.get("id") or "anonymous")
    resource_class = classify(method, path)

    decisions = []
    if per_class_enabled():
        # The class bucket is consumed FIRST, and a rejection here returns without touching
        # the aggregate. Order matters: checking the aggregate first would let a client
        # hammering one throttled class drain the tenant-wide budget anyway and take every
        # OTHER class down with it — reintroducing, one level up, the exact starvation this
        # split exists to prevent.
        klass = _bucket_decision("{0}:{1}".format(base_key, resource_class),
                                 resolve_class_limit(tenant, resource_class),
                                 cost, resource_class)
        if not klass.allowed:
            return klass
        if klass.enforced:
            decisions.append(klass)

    if aggregate_enabled():
        # The outer bound. Without it the real ceiling is sum(DEFAULT_CLASS_SHARE) x the
        # configured limit, because the shares deliberately overlap.
        #
        # A request the class bucket allowed but the aggregate rejects has already spent a
        # class token. That over-charge is one token on one bucket for a request that was
        # refused anyway, and it is the price of not making the aggregate drainable by a
        # class that is already over its own limit.
        aggregate = _bucket_decision(base_key, resolve_limit(tenant), cost, TENANT)
        if not aggregate.allowed:
            return aggregate
        if aggregate.enforced:
            decisions.append(aggregate)

    if not decisions:  # limiting disabled for both, or Redis failed open
        return ALLOW_ALL
    # `remaining` is denominated in requests, so the smallest one is genuinely the budget
    # that runs out first. min() keeps the FIRST minimum, so a tie resolves to the class
    # bucket — the more specific and more actionable of the two.
    return min(decisions, key=lambda d: d.remaining)


def _consume(key: str, rate: float, burst: int, cost: float):
    global _LUA_SHA
    client = redis_client()
    if client is not None:
        redis_key = "ratelimit:tb:{0}".format(key)
        now = time.time()
        try:
            if _LUA_SHA is None:
                # redis-py's stubs type these as the sync/async union (Awaitable[Any] | Any) because
                # one class serves both; this module is the sync client only.
                _LUA_SHA = client.script_load(_LUA_TOKEN_BUCKET)  # type: ignore[assignment]
            try:
                res = client.evalsha(_LUA_SHA, 1, redis_key, rate, burst, now, cost)  # type: ignore[arg-type]
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
                # redis-py's stubs type these as the sync/async union (Awaitable[Any] | Any) because
                # one class serves both; this module is the sync client only.
                _LUA_SHA = client.script_load(_LUA_TOKEN_BUCKET)  # type: ignore[assignment]
                res = client.evalsha(_LUA_SHA, 1, redis_key, rate, burst, now, cost)  # type: ignore[arg-type]
            return int(res[0]), float(res[1]), float(res[2])  # type: ignore[index]
        except (RedisError, OSError):
            # RedisError now includes redis.exceptions.TimeoutError, which only ever fires
            # because runtime.redis_client() sets socket_timeout. Before that, a HUNG Redis
            # (failover, blackholed route — not a refused connection) never raised at all:
            # this call blocked in recv() forever on a Starlette threadpool thread, and the
            # pool was consumed one rate-limit check at a time until the API stopped
            # answering. OSError is belt-and-braces for a socket error redis-py does not wrap.
            _LUA_SHA = None
            # Latch the outage so the NEXT request skips Redis entirely instead of paying the
            # timeout again. The timeout bounds one request; the latch bounds the incident.
            mark_redis_unavailable()
            if _fail_open():
                return None, 0.0, 0.0
            return _LOCAL.consume(key, rate, burst, cost)

    if _fail_open():
        return None, 0.0, 0.0
    return _LOCAL.consume(key, rate, burst, cost)
