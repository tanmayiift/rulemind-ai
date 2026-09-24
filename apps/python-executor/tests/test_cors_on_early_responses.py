"""CORS headers must be on the responses the auth middleware returns early.

CORSMiddleware was registered first, which in Starlette makes it the INNERMOST layer -- so the
401/403/429 that TenantContextMiddleware returns before any route runs carried no
Access-Control-Allow-Origin. A browser cannot read such a response: fetch() throws, and the
dashboard showed "Unable to reach the API server" for a wrong key, an expired session and a rate
limit alike. Found when the dashboard's auth gate (plan 6.6) could not see a 401.
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

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import preserve_storage_global  # noqa: E402

ORIGIN = "http://localhost:3000"


class CorsOnEarlyResponsesTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tempdir.cleanup)
        preserve_storage_global(self)
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "cors.db"))
        self.client = TestClient(app_main.app)
        self.addCleanup(self.client.close)

    def _allowed(self, response):
        return response.headers.get("access-control-allow-origin") == ORIGIN

    def test_a_401_is_readable_by_the_dashboard(self):
        response = self.client.get("/api/v1/policies", headers={"Origin": ORIGIN})
        self.assertEqual(response.status_code, 401)
        self.assertTrue(self._allowed(response), "a 401 without CORS headers reads as a network failure")
        self.assertEqual(response.headers.get("access-control-allow-credentials"), "true")

    def test_a_bad_key_is_readable_too(self):
        response = self.client.get("/api/v1/policies", headers={"Origin": ORIGIN, "x-api-key": "rm_live_wrong"})
        self.assertEqual(response.status_code, 401)
        self.assertTrue(self._allowed(response))

    def test_a_429_carries_both_cors_and_its_retry_budget(self):
        from app import middleware

        limited = mock.patch.object(middleware, "check_pre_auth_rate_limit",
                                    return_value=middleware.JSONResponse(status_code=429, content={"error": "rate_limited"},
                                                                         headers={"Retry-After": "7"}))
        with limited:
            response = self.client.post("/api/v1/auth/login", headers={"Origin": ORIGIN},
                                        json={"email": "a@b.co", "password": "x"})
        self.assertEqual(response.status_code, 429)
        self.assertTrue(self._allowed(response))
        self.assertIn("Retry-After", response.headers.get("access-control-expose-headers", ""))

    def test_the_control_a_success_was_always_fine(self):
        key = app_main.storage.generate_api_key_for_tenant(app_main.storage.default_tenant_id)["plaintext"]
        response = self.client.get("/api/v1/access/me", headers={"Origin": ORIGIN, "x-api-key": key})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self._allowed(response))


if __name__ == "__main__":
    unittest.main()
