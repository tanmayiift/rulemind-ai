"""Human RBAC: workspace member accounts + password/OTP login + session auth.

Covers the P0-E core: per-person accounts carrying an RBAC role, login by password
and by email OTP, bearer-session authentication of the normal API, and role-change-
in-place (an admin changing a member's role takes effect immediately)."""
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


class MemberAuthTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "auth.db"))
        self.client = TestClient(app_main.app)
        self.tenant_id = app_main.storage.default_tenant_id
        self.admin_key = {"x-api-key": app_main.storage.default_api_key or ""}

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def _create_member(self, email="analyst@acme.com", role="policy_maker", password="s3cret-pass"):
        return self.client.post("/api/v1/access/members", headers=self.admin_key,
                                json={"email": email, "name": "Analyst", "role": role, "password": password})

    # ── member management ────────────────────────────────────────────────
    def test_admin_creates_and_lists_members(self):
        r = self._create_member()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["role"], "policy_maker")
        self.assertFalse("password" in r.json() and r.json().get("password"))  # never echoed
        members = self.client.get("/api/v1/access/members", headers=self.admin_key).json()
        self.assertTrue(any(m["email"] == "analyst@acme.com" for m in members))

    def test_duplicate_member_email_conflicts(self):
        self._create_member()
        self.assertEqual(self._create_member().status_code, 409)

    def test_create_member_rejects_owner_role(self):
        r = self.client.post("/api/v1/access/members", headers=self.admin_key,
                             json={"email": "x@acme.com", "role": "owner", "password": "p"})
        self.assertEqual(r.status_code, 422)  # owner is the bootstrap role, not assignable

    # ── password login → session auth ───────────────────────────────────
    def test_password_login_issues_working_session(self):
        self._create_member(role="viewer")
        login = self.client.post("/api/v1/auth/login", json={"email": "analyst@acme.com", "password": "s3cret-pass"})
        self.assertEqual(login.status_code, 200, login.text)
        token = login.json()["token"]
        self.assertTrue(token.startswith("rms_"))
        # The session authenticates a normal API call (viewer can read).
        me = self.client.get("/api/v1/access/me", headers={"Authorization": "Bearer " + token})
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["role"], "viewer")

    def test_wrong_password_is_rejected(self):
        self._create_member()
        r = self.client.post("/api/v1/auth/login", json={"email": "analyst@acme.com", "password": "nope"})
        self.assertEqual(r.status_code, 401)

    def test_session_endpoint_and_logout(self):
        self._create_member(role="reviewer")
        token = self.client.post("/api/v1/auth/login",
                                 json={"email": "analyst@acme.com", "password": "s3cret-pass"}).json()["token"]
        bearer = {"Authorization": "Bearer " + token}
        self.assertEqual(self.client.get("/api/v1/auth/session", headers=bearer).json()["role"], "reviewer")
        self.assertTrue(self.client.post("/api/v1/auth/logout", headers=bearer).json()["logged_out"])
        # After logout the token no longer authenticates.
        self.assertEqual(self.client.get("/api/v1/auth/session", headers=bearer).status_code, 401)
        self.assertEqual(self.client.get("/api/v1/access/me", headers=bearer).status_code, 401)

    # ── RBAC enforced on human sessions ─────────────────────────────────
    def test_viewer_session_cannot_author(self):
        self._create_member(role="viewer")
        token = self.client.post("/api/v1/auth/login",
                                 json={"email": "analyst@acme.com", "password": "s3cret-pass"}).json()["token"]
        bearer = {"Authorization": "Bearer " + token}
        r = self.client.post("/api/v1/rules", headers=bearer, json={"name": "R", "expression": "x > 1"})
        self.assertEqual(r.status_code, 403)

    def test_role_change_in_place_takes_effect_immediately(self):
        self._create_member(role="viewer")
        token = self.client.post("/api/v1/auth/login",
                                 json={"email": "analyst@acme.com", "password": "s3cret-pass"}).json()["token"]
        bearer = {"Authorization": "Bearer " + token}
        member_id = self.client.get("/api/v1/access/members", headers=self.admin_key).json()
        member_id = next(m["id"] for m in member_id if m["email"] == "analyst@acme.com")
        # Promote viewer -> policy_maker; the existing session must gain author rights now.
        up = self.client.patch("/api/v1/access/members/" + member_id, headers=self.admin_key, json={"role": "policy_maker"})
        self.assertEqual(up.status_code, 200)
        self.assertEqual(self.client.get("/api/v1/access/me", headers=bearer).json()["role"], "policy_maker")

    def test_deactivated_member_session_stops_working(self):
        self._create_member(role="policy_maker")
        token = self.client.post("/api/v1/auth/login",
                                 json={"email": "analyst@acme.com", "password": "s3cret-pass"}).json()["token"]
        bearer = {"Authorization": "Bearer " + token}
        member_id = next(m["id"] for m in self.client.get("/api/v1/access/members", headers=self.admin_key).json()
                        if m["email"] == "analyst@acme.com")
        self.client.delete("/api/v1/access/members/" + member_id, headers=self.admin_key)
        self.assertEqual(self.client.get("/api/v1/access/me", headers=bearer).status_code, 401)

    # ── email OTP login ─────────────────────────────────────────────────
    def test_otp_login_flow(self):
        self._create_member(role="viewer", password=None)  # SSO/OTP-only member, no password
        req = self.client.post("/api/v1/auth/otp/request", json={"email": "analyst@acme.com"})
        self.assertEqual(req.status_code, 200)
        code = req.json().get("debug_code")  # surfaced inline in dev (no SMTP)
        self.assertTrue(code and len(code) == 6)
        verify = self.client.post("/api/v1/auth/otp/verify", json={"email": "analyst@acme.com", "code": code})
        self.assertEqual(verify.status_code, 200, verify.text)
        me = self.client.get("/api/v1/access/me", headers={"Authorization": "Bearer " + verify.json()["token"]})
        self.assertEqual(me.status_code, 200)

    def test_otp_request_for_unknown_email_does_not_enumerate(self):
        r = self.client.post("/api/v1/auth/otp/request", json={"email": "nobody@acme.com"})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("debug_code", r.json())  # no code minted for a non-member

    def test_wrong_otp_is_rejected(self):
        self._create_member(password=None)
        self.client.post("/api/v1/auth/otp/request", json={"email": "analyst@acme.com"})
        r = self.client.post("/api/v1/auth/otp/verify", json={"email": "analyst@acme.com", "code": "000000"})
        self.assertEqual(r.status_code, 401)

    # ── API-key role change in place ────────────────────────────────────
    def test_api_key_role_change_in_place(self):
        created = self.client.post("/api/v1/access/keys", headers=self.admin_key,
                                   json={"role": "viewer", "label": "svc"}).json()
        kid, plaintext = created["kid"], created["plaintext"]
        svc = {"x-api-key": plaintext}
        # viewer key can't author
        self.assertEqual(self.client.post("/api/v1/rules", headers=svc, json={"name": "R", "expression": "x>1"}).status_code, 403)
        # promote the key to policy_maker in place
        up = self.client.patch("/api/v1/access/keys/{0}/role".format(kid), headers=self.admin_key, json={"role": "policy_maker"})
        self.assertEqual(up.status_code, 200, up.text)
        # now authoring is allowed (cache was cleared eagerly)
        self.assertNotEqual(self.client.post("/api/v1/rules", headers=svc, json={"name": "R2", "expression": "x>1"}).status_code, 403)

    def test_unauthenticated_request_still_401(self):
        self.assertEqual(self.client.get("/api/v1/access/me").status_code, 401)


if __name__ == "__main__":
    unittest.main()
