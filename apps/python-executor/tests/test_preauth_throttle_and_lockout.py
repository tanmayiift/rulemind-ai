"""Unauthenticated endpoints must be throttled, and credential endpoints must lock out (Tier 3.6).

TenantContextMiddleware.dispatch() returns early three separate times before it ever reaches
the rate-limit check ~76 lines further down:

    path in EXEMPT_PATHS                     -> /api/v1/onboarding/signup lives in that set
    path.startswith("/api/admin/v1")         -> the entire platform-admin surface
    path.startswith("/api/v1/auth/")         -> member login and the OTP endpoints

So the limiter was not *skipped* for those routes, it was UNREACHABLE from them. No config
change and no per-tenant override could have switched it on, which is why this was invisible:
`RATE_LIMIT_ENABLED=1` was true, the limiter worked perfectly, and none of these paths ever
got to it.

What that left open, in order of severity:

  * `/api/v1/onboarding/signup` performs four unauthenticated writes -- create_tenant,
    seed_sample_inventory, generate_api_key_for_tenant, update_onboarding -- and returns a
    LIVE API key in the response body. Unbounded, it is a tenant and API-key faucet, and each
    minted key is a valid credential for every authenticated route behind it, the model-upload
    path included. Its own docstring said "In production, gate this behind email verification
    / CAPTCHA".

  * The superadmin login had no attempt counter, no backoff, no lockout and no audit event on
    failure. bcrypt's cost factor was the ONLY thing between an attacker and an unlimited
    online guessing attack against the account that can read and mutate every tenant. Its
    mobile twin `mobile_admin_login` shared the same credential through a different exempt
    path, so even a per-endpoint counter would have been bypassable by switching endpoints --
    both share one counter here, and this file tests that specifically.

The lockout's shape is copied from `storage.verify_member_otp(..., max_attempts=5)`, which had
already solved exactly this for OTP codes; passwords simply never got the same treatment.

Determinism note: these tests force the documented in-process fallback by patching
`redis_client` to None. Left alone, an operator's REDIS_URL in the ambient environment would
persist counters across runs and make the results depend on what ran previously.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app import middleware, rate_limit  # noqa: E402
from app.storage import Storage  # noqa: E402

ADMIN_EMAIL = "lockout-admin@example.com"
ADMIN_PASSWORD = "correct-horse-battery-staple"


class _NoRedisTestCase(unittest.TestCase):
    """Shared fixture: a private database, empty limiter buckets, no Redis."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "throttle.db"))
        self.client = TestClient(app_main.app)
        # Both limiters keep process-global state, so a previous module's requests would
        # otherwise decide this module's results.
        rate_limit.reset_local_state()
        middleware.reset_login_attempts_for_tests()
        self._patches = [
            mock.patch("app.rate_limit.redis_client", return_value=None),
            mock.patch("app.runtime.redis_client", return_value=None),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        self.client.close()
        self.tempdir.cleanup()
        rate_limit.reset_local_state()
        middleware.reset_login_attempts_for_tests()


class ClientIpDerivationTests(unittest.TestCase):
    """X-Forwarded-For is caller-supplied, so trusting it is worse than having no limit at all:
    a fresh forged header per request means a fresh bucket per request."""

    def _request(self, peer="10.0.0.9", forwarded=None):
        headers = []
        if forwarded is not None:
            headers.append((b"x-forwarded-for", forwarded.encode()))
        scope = {"type": "http", "method": "GET", "path": "/", "headers": headers,
                 "client": (peer, 1234), "query_string": b""}
        from fastapi import Request

        return Request(scope)

    def test_forwarded_headers_are_ignored_by_default(self):
        request = self._request(forwarded="203.0.113.7")
        self.assertEqual(middleware.client_ip(request), "10.0.0.9",
                         "defaulting to the socket peer is what makes the budget unforgeable")

    def test_one_trusted_hop_reads_the_last_entry(self):
        with mock.patch.dict(os.environ, {"RULEMIND_TRUSTED_PROXY_HOPS": "1"}):
            request = self._request(forwarded="198.51.100.4, 203.0.113.7")
            self.assertEqual(middleware.client_ip(request), "203.0.113.7")

    def test_a_chain_shorter_than_configured_falls_back_to_the_peer(self):
        """The request did not traverse the expected proxy chain, so nothing in the header was
        written by us. Accepting the leftmost entry here is the classic spoof."""
        with mock.patch.dict(os.environ, {"RULEMIND_TRUSTED_PROXY_HOPS": "2"}):
            request = self._request(forwarded="203.0.113.7")
            self.assertEqual(middleware.client_ip(request), "10.0.0.9")

    def test_blank_hop_config_does_not_crash_the_request(self):
        with mock.patch.dict(os.environ, {"RULEMIND_TRUSTED_PROXY_HOPS": ""}):
            self.assertEqual(middleware.trusted_proxy_hops(), 0)


class SignupThrottleTests(_NoRedisTestCase):
    def _signup(self, company):
        return self.client.post("/api/v1/onboarding/signup",
                                json={"company": company, "contact_email": "a@b.com"})

    def test_bulk_tenant_and_key_minting_is_capped_per_ip(self):
        with mock.patch.dict(os.environ, {"RULEMIND_SIGNUP_RATE_LIMIT_PER_MIN": "2"}):
            first = self._signup("One")
            second = self._signup("Two")
            third = self._signup("Three")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(third.status_code, 429,
                         "signup returned an API key past its budget: {0}".format(third.text[:200]))
        self.assertIn("Retry-After", third.headers)
        # And no tenant was created for the rejected attempt.
        self.assertNotIn("api_key", third.json())

    def test_self_serve_signup_can_be_closed_entirely(self):
        with mock.patch.dict(os.environ, {"RULEMIND_SELF_SERVE_SIGNUP": "0"}):
            response = self._signup("Locked Down")
        self.assertEqual(response.status_code, 403, response.text)


class AdminSurfaceThrottleTests(_NoRedisTestCase):
    def test_the_whole_admin_prefix_is_throttled_even_unauthenticated(self):
        """The second early return covers every /api/admin/v1 route, not just login, so an
        unauthenticated caller could hammer tenant listing and key creation for free."""
        with mock.patch.dict(os.environ, {"RULEMIND_ADMIN_RATE_LIMIT_PER_MIN": "2"}):
            codes = [self.client.get("/api/admin/v1/tenants").status_code for _ in range(3)]
        self.assertEqual(codes[-1], 429, "admin surface still unthrottled: {0}".format(codes))

    def test_the_credential_endpoints_are_throttled_but_session_polling_is_not(self):
        """/api/v1/auth/session is polled on every dashboard navigation. Throttling it at
        login rates would sign real users out, so only the paths that accept a guess are
        covered."""
        self.assertIsNotNone(middleware._pre_auth_limit("/api/v1/auth/login"))
        self.assertIsNotNone(middleware._pre_auth_limit("/api/mobile/v1/auth/login"))
        self.assertIsNone(middleware._pre_auth_limit("/api/v1/auth/session"))
        self.assertIsNone(middleware._pre_auth_limit("/api/v1/decide"))


class PlatformAdminLockoutTests(_NoRedisTestCase):
    def setUp(self):
        super().setUp()
        app_main.storage.create_platform_admin_user(ADMIN_EMAIL, ADMIN_PASSWORD, name="Lockout Admin")

    def _login(self, password, path="/api/admin/v1/auth/login"):
        return self.client.post(path, json={"email": ADMIN_EMAIL, "password": password})

    def test_repeated_wrong_passwords_stop_being_answered(self):
        with mock.patch.dict(os.environ, {"RULEMIND_LOGIN_MAX_ATTEMPTS": "3"}):
            codes = [self._login("wrong-{0}".format(i)).status_code for i in range(4)]
        self.assertEqual(codes[:3], [401, 401, 401])
        self.assertEqual(codes[3], 429,
                         "superadmin login still accepts unlimited guesses: {0}".format(codes))

    def test_lockout_holds_against_the_correct_password(self):
        """The difference between a counter and a lockout. If the right password still worked
        during the window, an attacker who guesses it on attempt N+1 is unaffected."""
        with mock.patch.dict(os.environ, {"RULEMIND_LOGIN_MAX_ATTEMPTS": "3"}):
            for i in range(3):
                self._login("wrong-{0}".format(i))
            locked = self._login(ADMIN_PASSWORD)
        self.assertEqual(locked.status_code, 429, locked.text)
        self.assertIn("Retry-After", locked.headers)

    def test_the_mobile_twin_shares_the_counter(self):
        """`mobile_admin_login` verifies the SAME credential through a different exempt path.
        A per-endpoint counter would just move the attack one URL to the left."""
        with mock.patch.dict(os.environ, {"RULEMIND_LOGIN_MAX_ATTEMPTS": "3"}):
            for i in range(3):
                self._login("wrong-{0}".format(i))
            mobile = self._login(ADMIN_PASSWORD, path="/api/mobile/v1/auth/login")
        self.assertEqual(mobile.status_code, 429, mobile.text)

    def test_a_successful_login_clears_the_counter(self):
        with mock.patch.dict(os.environ, {"RULEMIND_LOGIN_MAX_ATTEMPTS": "3"}):
            self._login("wrong-1")
            self._login("wrong-2")
            self.assertEqual(self._login(ADMIN_PASSWORD).status_code, 200)
            # Two more failures must not tip a freshly-authenticated account into lockout.
            self.assertEqual(self._login("wrong-3").status_code, 401)
            self.assertEqual(self._login("wrong-4").status_code, 401)

    def test_an_unknown_email_is_counted_too(self):
        """Skipping the count for unknown accounts would answer "is this address a superadmin?"
        through the lockout's status code."""
        with mock.patch.dict(os.environ, {"RULEMIND_LOGIN_MAX_ATTEMPTS": "2"}):
            codes = [
                self.client.post("/api/admin/v1/auth/login",
                                 json={"email": "ghost@example.com", "password": "x"}).status_code
                for _ in range(3)
            ]
        self.assertEqual(codes, [401, 401, 429])

    def test_repeated_failures_are_alertable_not_just_blocked(self):
        """Blocking without a signal means the first anyone hears of a campaign against the
        superadmin account is a support ticket."""
        with mock.patch.dict(os.environ, {"RULEMIND_LOGIN_MAX_ATTEMPTS": "5",
                                          "RULEMIND_LOGIN_ALERT_AFTER": "2"}):
            for i in range(3):
                self._login("wrong-{0}".format(i))
        events = app_main.storage.list_audit_events(tenant_id=app_main.storage.default_tenant_id)
        types = [event.get("event_type") for event in events]
        self.assertIn("login_failed_repeatedly", types, types[:10])


class MemberLoginLockoutTests(_NoRedisTestCase):
    def setUp(self):
        super().setUp()
        self.tenant_id = app_main.storage.default_tenant_id
        self.member = app_main.storage.create_member(
            self.tenant_id, "member-lockout@example.com", name="Locked Member",
            role="editor", password="member-password-1")

    def _login(self, password):
        return self.client.post("/api/v1/auth/login", json={
            "email": "member-lockout@example.com", "password": password, "tenant_id": self.tenant_id})

    def test_member_password_guessing_is_capped(self):
        with mock.patch.dict(os.environ, {"RULEMIND_LOGIN_MAX_ATTEMPTS": "3"}):
            codes = [self._login("wrong-{0}".format(i)).status_code for i in range(4)]
        self.assertEqual(codes[:3], [401, 401, 401])
        self.assertEqual(codes[3], 429, codes)

    def test_a_locked_member_cannot_log_in_with_the_right_password(self):
        with mock.patch.dict(os.environ, {"RULEMIND_LOGIN_MAX_ATTEMPTS": "3"}):
            for i in range(3):
                self._login("wrong-{0}".format(i))
            self.assertEqual(self._login("member-password-1").status_code, 429)

    def test_the_lockout_is_scoped_to_the_account_not_the_endpoint(self):
        """One member being locked out must not lock the workspace's other members out."""
        app_main.storage.create_member(self.tenant_id, "other-member@example.com",
                                       name="Other Member", role="editor",
                                       password="other-password-1")
        with mock.patch.dict(os.environ, {"RULEMIND_LOGIN_MAX_ATTEMPTS": "3"}):
            for i in range(4):
                self._login("wrong-{0}".format(i))
            other = self.client.post("/api/v1/auth/login", json={
                "email": "other-member@example.com", "password": "other-password-1",
                "tenant_id": self.tenant_id})
        self.assertEqual(other.status_code, 200, other.text)


if __name__ == "__main__":
    unittest.main()
