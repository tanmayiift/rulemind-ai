"""Response headers a browser needs, and not publishing the API's own map to strangers.

Both found by probing the LIVE deployment rather than by reading code. The only header coming
back from https://rulemind-api.onrender.com was `x-request-id` — no nosniff, no frame-ancestors,
no HSTS, no referrer policy — and `/docs`, `/redoc` and `/openapi.json` all answered 200 to an
unauthenticated request, publishing the complete endpoint map and every schema.

Neither is exotic. Both are the kind of thing that is invisible until something is actually
deployed and someone runs `curl -I` against it.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402


class SecurityHeaderTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app_main.app)
        self.addCleanup(self.client.close)

    def test_the_four_always_present_headers(self):
        headers = self.client.get("/health").headers
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_headers_are_on_error_responses_too(self):
        """The middleware is registered OUTERMOST for this reason.

        A 401 from TenantContextMiddleware returns early, before any route runs. Those are
        exactly the responses an attacker sees most of, and a browser needs nosniff and
        frame-ancestors on them just as much as on a 200.
        """
        response = self.client.get("/api/v1/policies")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

    def test_a_json_api_csp_allows_nothing(self):
        csp = self.client.get("/health").headers["Content-Security-Policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertIn("base-uri 'none'", csp)

    def test_hsts_is_absent_over_plain_http(self):
        """Sending HSTS over http is meaningless, and from a dev server it is actively hostile:
        it pins localhost to HTTPS in that developer's browser for a year."""
        self.assertNotIn("Strict-Transport-Security", self.client.get("/health").headers)

    def test_hsts_is_sent_when_a_proxy_terminated_tls(self):
        """The deployment case. Behind Render's TLS-terminating proxy the app itself sees plain
        http, so trusting request.url.scheme alone would never send HSTS in production."""
        headers = self.client.get("/health", headers={"X-Forwarded-Proto": "https"}).headers
        self.assertIn("max-age=31536000", headers["Strict-Transport-Security"])
        self.assertIn("includeSubDomains", headers["Strict-Transport-Security"])

    def test_a_forwarded_proto_list_takes_the_first_hop(self):
        headers = self.client.get("/health", headers={"X-Forwarded-Proto": "https, http"}).headers
        self.assertIn("Strict-Transport-Security", headers)

    def test_the_docs_csp_is_looser_but_still_frames_nothing(self):
        response = self.client.get("/docs")
        if response.status_code != 200:
            self.skipTest("docs are disabled in this environment")
        csp = response.headers["Content-Security-Policy"]
        self.assertIn("cdn.jsdelivr.net", csp, "Swagger UI cannot load its assets")
        self.assertIn("frame-ancestors 'none'", csp, "relaxing the CSP must not relax framing")


class DocsExposureTests(unittest.TestCase):
    """`/docs`, `/redoc` and `/openapi.json` were served unauthenticated in production."""

    def _client_with(self, **environ):
        with mock.patch.dict(os.environ, environ, clear=False):
            enabled = app_main._docs_enabled()
        return enabled

    def test_docs_are_off_in_production(self):
        for value in ("production", "prod", "PRODUCTION", "staging"):
            with mock.patch.dict(os.environ, {"RULEMIND_ENV": value, "RULEMIND_ENABLE_DOCS": ""}):
                self.assertFalse(app_main._docs_enabled(),
                                 "docs would be published in RULEMIND_ENV={0}".format(value))

    def test_docs_are_on_in_development(self):
        for value in ("development", "dev", ""):
            with mock.patch.dict(os.environ, {"RULEMIND_ENV": value, "RULEMIND_ENABLE_DOCS": ""}):
                self.assertTrue(app_main._docs_enabled())

    def test_production_can_opt_back_in_explicitly(self):
        """A deployment that wants a public API reference should be able to say so — the point
        is that it becomes a decision rather than the default."""
        with mock.patch.dict(os.environ, {"RULEMIND_ENV": "production",
                                          "RULEMIND_ENABLE_DOCS": "1"}):
            self.assertTrue(app_main._docs_enabled())


if __name__ == "__main__":
    unittest.main()
