"""RBAC — role-scoped API keys, unit + smoke + regression.

* unit        — the capability mapping in app.rbac (method+path -> capability,
                role -> capabilities)
* smoke       — the /access endpoints (me / roles / create-key / list / revoke)
* regression  — the enforcement matrix across viewer / reviewer / policy_maker /
                admin, plus the owner default key keeping full access
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
os.environ.setdefault("RULEMIND_ADMIN_JWT_SECRET", "rulemind-test-admin-secret")
os.environ.pop("AUTH_MODE", None)  # apikey mode so RBAC is enforced

import app.main as app_main  # noqa: E402
from app import rbac  # noqa: E402
from app.storage import Storage  # noqa: E402


class RbacUnitTests(unittest.TestCase):
    def test_get_is_read(self):
        self.assertEqual(rbac.required_capability("GET", "/api/v1/rules"), rbac.READ)

    def test_authoring_write_is_author(self):
        self.assertEqual(rbac.required_capability("POST", "/api/v1/rules"), rbac.AUTHOR)
        self.assertEqual(rbac.required_capability("PUT", "/api/v1/decision-tables/x"), rbac.AUTHOR)

    def test_decide_is_decide(self):
        self.assertEqual(rbac.required_capability("POST", "/api/v1/decide"), rbac.DECIDE)

    def test_review_and_deploy_and_access(self):
        self.assertEqual(rbac.required_capability("POST", "/api/v1/reviews/t1/decide"), rbac.REVIEW)
        self.assertEqual(rbac.required_capability("POST", "/api/v1/promotions"), rbac.DEPLOY)
        self.assertEqual(rbac.required_capability("POST", "/api/v1/policies/p1/promote"), rbac.DEPLOY)
        self.assertEqual(rbac.required_capability("POST", "/api/v1/access/keys"), rbac.MANAGE_ACCESS)

    def test_role_capability_sets(self):
        self.assertEqual(rbac.capabilities_for("viewer"), {rbac.READ})
        self.assertNotIn(rbac.AUTHOR, rbac.capabilities_for("reviewer"))
        self.assertIn(rbac.AUTHOR, rbac.capabilities_for("policy_maker"))
        self.assertNotIn(rbac.MANAGE_ACCESS, rbac.capabilities_for("policy_maker"))
        self.assertEqual(rbac.capabilities_for("admin"), rbac.ALL_CAPABILITIES)

    def test_unknown_role_defaults_to_owner(self):
        self.assertEqual(rbac.normalize_role("nonsense"), "owner")


class RbacApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "rbac.db"))
        self.client = TestClient(app_main.app)
        self.owner = {"x-api-key": app_main.storage.default_api_key or ""}

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def _key(self, role):
        r = self.client.post("/api/v1/access/keys", headers=self.owner, json={"role": role, "label": role})
        self.assertEqual(r.status_code, 200, r.text)
        return {"x-api-key": r.json()["plaintext"]}

    # ---- smoke: access endpoints ----
    def test_access_me_and_roles(self):
        me = self.client.get("/api/v1/access/me", headers=self.owner).json()
        self.assertEqual(me["role"], "owner")
        self.assertIn("manage_access", me["capabilities"])
        roles = self.client.get("/api/v1/access/roles", headers=self.owner).json()
        self.assertEqual(roles["assignable"], ["admin", "policy_maker", "reviewer", "viewer"])

    def test_create_list_revoke_key(self):
        created = self.client.post("/api/v1/access/keys", headers=self.owner, json={"role": "viewer", "label": "read-only"}).json()
        self.assertEqual(created["role"], "viewer")
        keys = self.client.get("/api/v1/access/keys", headers=self.owner).json()
        self.assertTrue(any(k["kid"] == created["kid"] and k["role"] == "viewer" for k in keys))
        revoked = self.client.delete(f"/api/v1/access/keys/{created['kid']}", headers=self.owner)
        self.assertEqual(revoked.status_code, 200, revoked.text)

    def test_cannot_revoke_current_key(self):
        keys = self.client.get("/api/v1/access/keys", headers=self.owner).json()
        current = next(k for k in keys if k.get("is_current"))
        r = self.client.delete(f"/api/v1/access/keys/{current['kid']}", headers=self.owner)
        self.assertEqual(r.status_code, 409)

    def test_invalid_role_rejected(self):
        r = self.client.post("/api/v1/access/keys", headers=self.owner, json={"role": "superuser"})
        self.assertEqual(r.status_code, 422)

    # ---- regression: enforcement matrix ----
    def test_viewer_is_read_only(self):
        v = self._key("viewer")
        self.assertEqual(self.client.get("/api/v1/policies", headers=v).status_code, 200)          # read ok
        self.assertEqual(self.client.post("/api/v1/rules", headers=v, json={"name": "x"}).status_code, 403)  # author blocked
        self.assertEqual(self.client.post("/api/v1/decide", headers=v, json={"policyId": "policy_instant_personal_loan", "payload": {}}).status_code, 403)  # decide blocked
        self.assertEqual(self.client.post("/api/v1/access/keys", headers=v, json={"role": "viewer"}).status_code, 403)  # manage blocked

    def test_reviewer_can_review_and_decide_not_author(self):
        rv = self._key("reviewer")
        self.assertEqual(self.client.get("/api/v1/policies", headers=rv).status_code, 200)
        self.assertEqual(self.client.post("/api/v1/decide", headers=rv, json={"policyId": "policy_instant_personal_loan", "payload": {}}).status_code, 200)
        self.assertEqual(self.client.post("/api/v1/rules", headers=rv, json={"name": "x"}).status_code, 403)  # author blocked
        # reviewer reaches the review endpoint (RBAC passes) — 404 for a missing task, not 403
        self.assertNotEqual(self.client.post("/api/v1/reviews/nope/decide", headers=rv, json={"decision": "approve"}).status_code, 403)

    def test_policy_maker_can_author_not_review_or_manage(self):
        pm = self._key("policy_maker")
        created = self.client.post("/api/v1/rules", headers=pm, json={"name": "PM Rule", "nodes": []})
        self.assertNotEqual(created.status_code, 403)  # author allowed (403 would be RBAC denial)
        self.assertEqual(self.client.post("/api/v1/reviews/nope/decide", headers=pm, json={"decision": "approve"}).status_code, 403)  # review blocked
        self.assertEqual(self.client.post("/api/v1/access/keys", headers=pm, json={"role": "viewer"}).status_code, 403)  # manage blocked

    def test_admin_has_full_access(self):
        ad = self._key("admin")
        self.assertEqual(self.client.get("/api/v1/policies", headers=ad).status_code, 200)
        self.assertNotEqual(self.client.post("/api/v1/rules", headers=ad, json={"name": "Admin Rule", "nodes": []}).status_code, 403)
        self.assertEqual(self.client.post("/api/v1/access/keys", headers=ad, json={"role": "viewer"}).status_code, 200)

    def test_owner_default_key_keeps_full_access(self):
        # the pre-existing/default key defaults to owner -> nothing is blocked
        self.assertNotEqual(self.client.post("/api/v1/rules", headers=self.owner, json={"name": "Owner Rule", "nodes": []}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
