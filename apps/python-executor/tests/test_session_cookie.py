"""Browser session is an httpOnly cookie + double-submit CSRF.

The member session token is no longer sent as a bearer from JS-readable storage; login sets an
httpOnly `rm_session` cookie (auto-sent) plus a readable `rm_csrf` cookie echoed back in
`X-CSRF-Token` on state-changing requests. Header/API-key callers (machines, mobile) are exempt.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

import app.main as app_main  # noqa: E402
from app.storage import Storage  # noqa: E402


class SessionCookieTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "cookie.db"))
        self.client = TestClient(app_main.app)
        self.admin_key = {"x-api-key": app_main.storage.default_api_key or ""}
        self.password = "s3cret-pass"
        created = self.client.post(
            "/api/v1/access/members", headers=self.admin_key,
            json={"email": "owner@acme.com", "name": "Owner", "role": "admin", "password": self.password},
        )
        self.assertEqual(created.status_code, 200, created.text)

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def _login(self):
        r = self.client.post("/api/v1/auth/login", json={"email": "owner@acme.com", "password": self.password})
        self.assertEqual(r.status_code, 200, r.text)
        return r

    def test_login_sets_httponly_session_and_csrf_cookies(self):
        r = self._login()
        set_cookie = r.headers.get("set-cookie", "")
        # Both cookies issued; session is httpOnly, csrf is not.
        self.assertIn("rm_session=", set_cookie)
        self.assertIn("rm_csrf=", set_cookie)
        self.assertIn("httponly", set_cookie.lower())
        self.assertIn("csrf_token", r.json())
        # TestClient's jar now holds the session cookie.
        self.assertTrue(self.client.cookies.get("rm_session"))

    def test_session_endpoint_authenticates_via_cookie(self):
        self._login()
        # No Authorization header, no api key — pure cookie auth.
        me = self.client.get("/api/v1/auth/session")
        self.assertEqual(me.status_code, 200, me.text)
        self.assertEqual(me.json()["member"]["email"], "owner@acme.com")

    def test_cookie_mutation_without_csrf_is_blocked(self):
        self._login()
        # Cookie is auto-sent; no X-CSRF-Token header -> blocked.
        r = self.client.post(
            "/api/v1/access/members",
            json={"email": "x@acme.com", "name": "X", "role": "viewer", "password": "pw-123456"},
        )
        self.assertEqual(r.status_code, 403)
        self.assertIn("CSRF", r.json().get("error", ""))

    def test_cookie_mutation_with_csrf_passes(self):
        csrf = self._login().json()["csrf_token"]
        r = self.client.post(
            "/api/v1/access/members",
            headers={"X-CSRF-Token": csrf},
            json={"email": "y@acme.com", "name": "Y", "role": "viewer", "password": "pw-123456"},
        )
        self.assertEqual(r.status_code, 200, r.text)

    def test_bearer_auth_is_exempt_from_csrf(self):
        token = self._login().json()["token"]
        # Fresh client (no cookie jar); authenticate with the bearer header, no CSRF.
        bare = TestClient(app_main.app)
        try:
            r = bare.post(
                "/api/v1/access/members",
                headers={"Authorization": "Bearer {0}".format(token)},
                json={"email": "z@acme.com", "name": "Z", "role": "viewer", "password": "pw-123456"},
            )
            self.assertNotEqual(r.status_code, 403, "bearer/API-key callers must not need a CSRF token")
            self.assertEqual(r.status_code, 200, r.text)
        finally:
            bare.close()

    def test_logout_clears_cookies(self):
        self._login()
        out = self.client.post("/api/v1/auth/logout")
        self.assertEqual(out.status_code, 200)
        # The session cookie is cleared (deleted) after logout.
        self.assertFalse(self.client.cookies.get("rm_session"))


if __name__ == "__main__":
    unittest.main()
