"""SCIM 2.0 provisioning (Track B6).

Enterprise buyers will not manage people in two places. SCIM is how Okta and Entra ID push
joiners, movers and leavers in automatically -- and the leaver half is what gets audited: a
termination in the IdP must end access here with nobody remembering to do it.

The tests are weighted accordingly. Creating a user is the easy half and is covered once;
most of this file is about the ways a SCIM integration silently half-works:

  * the endpoints being unauthenticated (a user-provisioning API on a new URL prefix that the
    auth middleware did not know about);
  * `active: false` arriving in a shape the server does not recognise, so terminations no-op;
  * pagination read as 0-based, so one user per page never provisions;
  * a duplicate create answering 500 instead of 409, so an IdP retry loops;
  * errors returned in FastAPI's envelope instead of SCIM's, which an IdP cannot parse.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
# Not optional: with AUTH_MODE=none the middleware early-returns and the unauthenticated test
# below would pass for the wrong reason.
os.environ.pop("AUTH_MODE", None)

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app import scim  # noqa: E402
from app.storage import Storage  # noqa: E402


class ScimMappingTest(unittest.TestCase):
    """The pure mapping layer, where the IdP-compatibility bugs live."""

    def test_patch_accepts_both_shapes_idps_send(self):
        """Okta sends {"value": {"active": false}}; others send {"path": "active", ...}. Both
        are legal SCIM. Handling one means terminations silently do not apply."""
        okta = scim.apply_patch({}, [{"op": "replace", "value": {"active": False}}])
        pathed = scim.apply_patch({}, [{"op": "replace", "path": "active", "value": False}])
        self.assertEqual(okta, {"is_active": False})
        self.assertEqual(pathed, {"is_active": False})

    def test_the_string_False_deactivates(self):
        """Some IdPs send `active` as the STRING "False". Truthiness would leave a terminated
        employee's access enabled -- the worst possible failure for this feature."""
        self.assertEqual(scim.apply_patch({}, [{"op": "replace", "path": "active", "value": "False"}]),
                         {"is_active": False})
        self.assertEqual(scim.apply_patch({}, [{"op": "replace", "path": "active", "value": "true"}]),
                         {"is_active": True})

    def test_filter_parses_the_probe_idps_actually_send(self):
        self.assertEqual(scim.parse_filter('userName eq "a@b.com"'), ("username", "eq", "a@b.com"))
        self.assertIsNone(scim.parse_filter(None))
        self.assertIsNone(scim.parse_filter("   "))

    def test_an_unsupported_filter_raises_rather_than_matching_everything(self):
        """Returning every user for a filter we cannot honour reads to an IdP as 'no match',
        and it then creates a duplicate."""
        with self.assertRaises(ValueError):
            scim.parse_filter('userName sw "a"')
        with self.assertRaises(ValueError):
            scim.parse_filter('name.givenName eq "A"')

    def test_error_status_is_a_string(self):
        """RFC 7644 s3.12. An integer here is rejected by strict IdP parsers."""
        body = scim.error(404, "nope")
        self.assertEqual(body["status"], "404")
        self.assertIsInstance(body["status"], str)
        self.assertEqual(body["schemas"], [scim.ERROR_SCHEMA])

    def test_a_provisioned_user_defaults_to_the_least_privileged_role(self):
        self.assertEqual(scim.DEFAULT_ROLE, "viewer",
                         "an IdP must not be able to mint an admin by omission")


class ScimEndpointTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "scim.db"))
        self.client = TestClient(app_main.app)
        self.tenant = app_main.storage.default_tenant_id
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def _create(self, email="joiner@example.com", active=True):
        return self.client.post("/scim/v2/Users", headers=self.headers, json={
            "schemas": [scim.USER_SCHEMA], "userName": email,
            "name": {"givenName": "Ada", "familyName": "Lovelace"},
            "externalId": "idp-1", "active": active})

    # ── the security precondition ───────────────────────────────────────────

    def test_scim_is_not_reachable_without_auth(self):
        """/scim/v2 was a NEW url prefix, and the auth middleware guarded only /api/v1 and
        /sdk/v1. Without extending it, anyone could enumerate and create accounts. This is the
        test that would have caught shipping that."""
        probes = (
            ("GET", "/scim/v2/Users", None),
            ("POST", "/scim/v2/Users", {"userName": "intruder@example.com"}),
            ("GET", "/scim/v2/Users/anything", None),
            ("DELETE", "/scim/v2/Users/anything", None),
        )
        for method, path, body in probes:
            with self.subTest(path=path, method=method):
                res = self.client.request(method, path, json=body)
                self.assertEqual(res.status_code, 401, f"{method} {path} was unauthenticated")

    # ── provisioning ────────────────────────────────────────────────────────

    def test_create_returns_201_and_a_scim_user(self):
        res = self._create()
        self.assertEqual(res.status_code, 201, res.text[:300])
        self.assertTrue(res.headers["content-type"].startswith(scim.CONTENT_TYPE))
        body = res.json()
        self.assertEqual(body["schemas"], [scim.USER_SCHEMA])
        self.assertEqual(body["userName"], "joiner@example.com")
        self.assertEqual(body["externalId"], "idp-1")
        self.assertTrue(body["active"])
        self.assertEqual(body["meta"]["resourceType"], "User")

    def test_a_provisioned_user_is_a_viewer(self):
        member = app_main.storage.get_member(self.tenant, self._create().json()["id"])
        self.assertEqual(member["role"], "viewer")

    def test_creating_a_duplicate_is_409_not_500(self):
        """An IdP retrying a create after a timeout uses 409 to tell 'already provisioned'
        from a real failure; a 500 makes it retry forever."""
        self._create()
        clash = self._create()
        self.assertEqual(clash.status_code, 409)
        self.assertEqual(clash.json()["scimType"], "uniqueness")
        self.assertEqual(clash.json()["status"], "409")

    # ── the leaver path ─────────────────────────────────────────────────────

    def test_patch_active_false_deactivates_the_member(self):
        user_id = self._create().json()["id"]
        res = self.client.patch(f"/scim/v2/Users/{user_id}", headers=self.headers, json={
            "schemas": [scim.PATCH_SCHEMA],
            "Operations": [{"op": "replace", "value": {"active": False}}]})
        self.assertEqual(res.status_code, 200, res.text[:200])
        self.assertFalse(res.json()["active"])
        self.assertFalse(app_main.storage.get_member(self.tenant, user_id)["is_active"])

    def test_deprovisioning_revokes_a_live_session_immediately(self):
        """The assertion that makes this an offboarding control rather than a flag. If the
        session survived until expiry, a terminated employee would keep access for hours."""
        user_id = self._create().json()["id"]
        session = app_main.storage.create_member_session(self.tenant, user_id)
        token = session["token"]
        self.assertIsNotNone(app_main.storage.resolve_member_session(token))

        self.client.patch(f"/scim/v2/Users/{user_id}", headers=self.headers, json={
            "Operations": [{"op": "replace", "path": "active", "value": False}]})

        resolved = app_main.storage.resolve_member_session(token)
        self.assertFalse(resolved and resolved.get("is_active"),
                         "a deprovisioned member must not resolve to an active session")

    def test_delete_deactivates_rather_than_destroying_history(self):
        """Hard-deleting would orphan or cascade the audit trail of what this person did --
        the opposite of what an offboarding control is for."""
        user_id = self._create().json()["id"]
        self.assertEqual(self.client.delete(f"/scim/v2/Users/{user_id}",
                                            headers=self.headers).status_code, 204)
        member = app_main.storage.get_member(self.tenant, user_id)
        self.assertIsNotNone(member, "the row must survive so history stays attributable")
        self.assertFalse(member["is_active"])

    # ── listing ─────────────────────────────────────────────────────────────

    def test_the_provisioning_probe_finds_an_existing_user(self):
        """Okta and Entra both filter by userName before deciding POST vs PATCH."""
        self._create("known@example.com")
        res = self.client.get("/scim/v2/Users", headers=self.headers,
                              params={"filter": 'userName eq "known@example.com"'})
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["schemas"], [scim.LIST_SCHEMA])
        self.assertEqual(body["totalResults"], 1)
        self.assertEqual(body["Resources"][0]["userName"], "known@example.com")

    def test_a_probe_for_an_unknown_user_returns_zero_not_everyone(self):
        self._create("known@example.com")
        body = self.client.get("/scim/v2/Users", headers=self.headers,
                               params={"filter": 'userName eq "nobody@example.com"'}).json()
        self.assertEqual(body["totalResults"], 0)
        self.assertEqual(body["Resources"], [])

    def test_start_index_is_one_based(self):
        """RFC 7644 s3.4.2.4. Read as a 0-based offset, the first user of every page is
        dropped -- surfacing as 'one user never provisions', which is very hard to trace."""
        for i in range(3):
            self._create(f"u{i}@example.com")
        first = self.client.get("/scim/v2/Users", headers=self.headers,
                                params={"startIndex": 1, "count": 1}).json()
        second = self.client.get("/scim/v2/Users", headers=self.headers,
                                 params={"startIndex": 2, "count": 1}).json()
        self.assertEqual(first["startIndex"], 1)
        self.assertEqual(len(first["Resources"]), 1)
        self.assertNotEqual(first["Resources"][0]["id"], second["Resources"][0]["id"],
                            "startIndex 1 and 2 must return different users")

    def test_an_unsupported_filter_is_400_with_a_scim_error(self):
        res = self.client.get("/scim/v2/Users", headers=self.headers,
                              params={"filter": 'userName sw "a"'})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["schemas"], [scim.ERROR_SCHEMA])
        self.assertEqual(res.json()["scimType"], "invalidFilter")

    def test_unknown_user_is_a_scim_404(self):
        res = self.client.get("/scim/v2/Users/does-not-exist", headers=self.headers)
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json()["schemas"], [scim.ERROR_SCHEMA],
                         "an IdP parses the SCIM error envelope, not FastAPI's {'detail': ...}")

    def test_users_are_scoped_to_the_calling_tenant(self):
        self._create("mine@example.com")
        other = app_main.storage.create_tenant("Other Co")
        other_key = app_main.storage.generate_api_key_for_tenant(other["id"])["plaintext"]
        body = self.client.get("/scim/v2/Users", headers={"x-api-key": other_key}).json()
        self.assertNotIn("mine@example.com", [r["userName"] for r in body["Resources"]])

    def test_service_provider_config_advertises_only_what_works(self):
        body = self.client.get("/scim/v2/ServiceProviderConfig", headers=self.headers).json()
        self.assertTrue(body["patch"]["supported"])
        self.assertTrue(body["filter"]["supported"])
        self.assertFalse(body["bulk"]["supported"], "claiming bulk would make an IdP send calls that fail")
        self.assertFalse(body["sort"]["supported"])


if __name__ == "__main__":
    unittest.main()
