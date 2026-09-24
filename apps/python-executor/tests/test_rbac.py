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
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")  # tests use the sample lending inventory
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

    def test_absent_role_is_the_bootstrap_owner(self):
        """No role recorded at all means a seed or bootstrap key: those ARE the workspace
        owner, and demoting them would lock a fresh install out of itself."""
        self.assertEqual(rbac.normalize_role(None), "owner")
        self.assertEqual(rbac.normalize_role(""), "owner")
        self.assertEqual(rbac.normalize_role("   "), "owner")

    def test_unrecognised_role_fails_CLOSED(self):
        """This test used to assert the opposite -- `normalize_role("nonsense") == "owner"` --
        and so encoded a fail-open as intended behaviour.

        A role that is present but unrecognised is a typo, a hand-edited DB row, a role renamed
        in a future release, or a value written by a newer node mid-rolling-deploy. Resolving
        any of those to `owner` grants FULL ownership on a data-integrity error. It must resolve
        to the least-privileged defined role instead -- the same choice, for the same reason, as
        scim.py:37: nothing may mint an owner by getting a string wrong.

        Note the asymmetry with the test above is the whole point: absent and unrecognised are
        different questions and used to share one answer."""
        # NB: "Owner " is NOT in this list -- it strips and lowercases to a real role, which is
        # correct normalisation, not a fail-open. The first draft of this test had it here and
        # the test caught the mistake.
        for bogus in ("nonsense", "superuser", "ownerr", "root", "admin;--", "own er"):
            self.assertEqual(rbac.normalize_role(bogus), "viewer", bogus)
            self.assertEqual(rbac.capabilities_for(bogus), {rbac.READ}, bogus)
            self.assertFalse(rbac.is_allowed(bogus, "POST", "/api/v1/rules"), bogus)
            self.assertFalse(rbac.is_allowed(bogus, "DELETE", "/api/v1/access/keys"), bogus)

    def test_known_roles_survive_casing_and_whitespace(self):
        self.assertEqual(rbac.normalize_role("ADMIN"), "admin")
        self.assertEqual(rbac.normalize_role("  Viewer  "), "viewer")

    def test_capabilities_for_returns_a_copy(self):
        """A caller mutating the returned set must not edit the capability table for every
        request the process serves afterwards."""
        got = rbac.capabilities_for("viewer")
        got.add(rbac.MANAGE_ACCESS)
        self.assertEqual(rbac.capabilities_for("viewer"), {rbac.READ})


class RbacApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._orig_storage = app_main.storage
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "rbac.db"))
        self.client = TestClient(app_main.app)
        self.owner = {"x-api-key": app_main.storage.default_api_key or ""}

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

        # Restore the global, and release this test's engine. Placed at the END of tearDown so
        # the teardown above still runs against THIS test's storage -- an earlier version put it
        # first and silently redirected those statements at the restored global.
        #
        # The restore is the load-bearing half. Leaving app_main.storage pointed at a deleted
        # tempdir made an UNRELATED module later fail with
        # `sqlite3.OperationalError: unable to open database file`: the failure lands on whoever
        # reads the global next, never on the test that leaked it, which is why it looked like a
        # random flake for so long.
        try:
            app_main.storage.engine.dispose()
        except Exception:  # pragma: no cover - teardown must not fail the test
            pass
        try:
            app_main.storage = self._orig_storage
        except Exception:  # pragma: no cover
            pass

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
