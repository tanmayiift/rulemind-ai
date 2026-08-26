"""Configurable rate limiting: resolution order, token-bucket behaviour, kill switch."""
import os
import time
import unittest

from app import rate_limit


class _Env:
    """Set/restore a batch of env vars around a test."""

    KEYS = ("RATE_LIMIT_ENABLED", "RATE_LIMIT_RPM", "RATE_LIMIT_RPM_STANDARD",
            "RATE_LIMIT_RPM_ENTERPRISE", "RATE_LIMIT_BURST_MULTIPLIER",
            "RATE_LIMIT_WINDOW_SECONDS", "RATE_LIMIT_EXEMPT_PATHS", "RATE_LIMIT_FAIL_OPEN")

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in self.KEYS}
        for k in self.KEYS:
            os.environ.pop(k, None)
        rate_limit.reset_local_state()
        return self

    def __exit__(self, *a):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        rate_limit.reset_local_state()


class ResolutionOrderTests(unittest.TestCase):
    def test_builtin_plan_defaults(self):
        with _Env():
            self.assertEqual(rate_limit.resolve_limit({"plan": "standard"}), 1000)
            self.assertEqual(rate_limit.resolve_limit({"plan": "enterprise"}), 5000)
            self.assertEqual(rate_limit.resolve_limit({"plan": "free"}), 120)

    def test_unknown_plan_falls_back_to_default(self):
        with _Env():
            self.assertEqual(rate_limit.resolve_limit({"plan": "bespoke"}), rate_limit.DEFAULT_RPM)

    def test_global_env_overrides_builtin(self):
        with _Env():
            os.environ["RATE_LIMIT_RPM"] = "250"
            self.assertEqual(rate_limit.resolve_limit({"plan": "standard"}), 250)

    def test_per_plan_env_beats_global_env(self):
        with _Env():
            os.environ["RATE_LIMIT_RPM"] = "250"
            os.environ["RATE_LIMIT_RPM_STANDARD"] = "999"
            self.assertEqual(rate_limit.resolve_limit({"plan": "standard"}), 999)
            # a plan without its own var still uses the global
            self.assertEqual(rate_limit.resolve_limit({"plan": "growth"}), 250)

    def test_per_tenant_config_beats_everything(self):
        with _Env():
            os.environ["RATE_LIMIT_RPM"] = "250"
            os.environ["RATE_LIMIT_RPM_STANDARD"] = "999"
            tenant = {"plan": "standard", "config": {"rate_limit_rpm": 42}}
            self.assertEqual(rate_limit.resolve_limit(tenant), 42)

    def test_malformed_values_do_not_crash(self):
        with _Env():
            os.environ["RATE_LIMIT_RPM"] = "not-a-number"
            self.assertEqual(rate_limit.resolve_limit({"plan": "standard"}), 1000)
            bad_tenant = {"plan": "standard", "config": {"rate_limit_rpm": "abc"}}
            self.assertEqual(rate_limit.resolve_limit(bad_tenant), 1000)

    def test_zero_or_negative_is_ignored_not_a_total_block(self):
        with _Env():
            os.environ["RATE_LIMIT_RPM"] = "0"
            self.assertEqual(rate_limit.resolve_limit({"plan": "standard"}), 1000)


class KillSwitchTests(unittest.TestCase):
    def test_disabled_allows_everything_and_marks_unenforced(self):
        with _Env():
            os.environ["RATE_LIMIT_ENABLED"] = "0"
            tenant = {"id": "t-disabled", "plan": "free", "config": {"rate_limit_rpm": 1}}
            for _ in range(50):
                d = rate_limit.check(tenant)
                self.assertTrue(d.allowed)
            self.assertFalse(d.enforced)
            self.assertEqual(d.headers(), {"RateLimit-Limit": "0", "RateLimit-Remaining": "0",
                                           "RateLimit-Reset": "0"})

    def test_enabled_by_default(self):
        with _Env():
            self.assertTrue(rate_limit.enabled())


class TokenBucketTests(unittest.TestCase):
    def test_allows_exactly_the_budget_then_rejects(self):
        with _Env():
            tenant = {"id": "t-bucket", "plan": "standard", "config": {"rate_limit_rpm": 5}}
            allowed = [rate_limit.check(tenant).allowed for _ in range(8)]
            self.assertEqual(allowed[:5], [True] * 5)
            self.assertEqual(allowed[5:], [False] * 3)

    def test_rejection_reports_a_usable_retry_after(self):
        with _Env():
            tenant = {"id": "t-retry", "plan": "standard", "config": {"rate_limit_rpm": 2}}
            rate_limit.check(tenant); rate_limit.check(tenant)
            d = rate_limit.check(tenant)
            self.assertFalse(d.allowed)
            self.assertGreaterEqual(d.retry_after, 1)
            self.assertEqual(d.headers()["Retry-After"], str(d.retry_after))

    def test_remaining_counts_down(self):
        with _Env():
            tenant = {"id": "t-remain", "plan": "standard", "config": {"rate_limit_rpm": 10}}
            first = rate_limit.check(tenant).remaining
            second = rate_limit.check(tenant).remaining
            self.assertLess(second, first)

    def test_bucket_refills_over_time(self):
        with _Env():
            # 600/min = 10/s, so ~0.3s of refill returns at least one token.
            tenant = {"id": "t-refill", "plan": "standard", "config": {"rate_limit_rpm": 600}}
            while rate_limit.check(tenant).allowed:
                pass
            time.sleep(0.35)
            self.assertTrue(rate_limit.check(tenant).allowed)

    def test_tenants_have_independent_buckets(self):
        with _Env():
            a = {"id": "t-a", "plan": "standard", "config": {"rate_limit_rpm": 2}}
            b = {"id": "t-b", "plan": "standard", "config": {"rate_limit_rpm": 2}}
            rate_limit.check(a); rate_limit.check(a)
            self.assertFalse(rate_limit.check(a).allowed)
            self.assertTrue(rate_limit.check(b).allowed, "tenant B drained by tenant A")

    def test_burst_multiplier_raises_capacity(self):
        with _Env():
            os.environ["RATE_LIMIT_BURST_MULTIPLIER"] = "2"
            tenant = {"id": "t-burst", "plan": "standard", "config": {"rate_limit_rpm": 5}}
            allowed = sum(1 for _ in range(12) if rate_limit.check(tenant).allowed)
            self.assertEqual(allowed, 10, "burst capacity should be rpm x multiplier")


class ExemptPathTests(unittest.TestCase):
    def test_health_endpoints_are_exempt_by_default(self):
        with _Env():
            for path in ("/health", "/ready", "/metrics", "/api/v1/health", "/sdk/v1/health"):
                self.assertTrue(rate_limit.is_exempt(path), path)

    def test_business_endpoints_are_not_exempt(self):
        with _Env():
            for path in ("/api/v1/decide", "/api/v1/rules", "/sdk/v1/bundle"):
                self.assertFalse(rate_limit.is_exempt(path), path)

    def test_exempt_paths_are_configurable(self):
        with _Env():
            os.environ["RATE_LIMIT_EXEMPT_PATHS"] = "/api/v1/decide,/custom"
            self.assertTrue(rate_limit.is_exempt("/api/v1/decide"))
            self.assertTrue(rate_limit.is_exempt("/custom/nested"))
            self.assertFalse(rate_limit.is_exempt("/health"), "explicit list replaces the default")


if __name__ == "__main__":
    unittest.main()
