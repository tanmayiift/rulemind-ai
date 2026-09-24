"""OAuth2 client credentials for RuleMind's own API.

The only machine credential was a long-lived x-api-key sent on every request -- something
standard OAuth2 client libraries cannot use, and the first thing a security review objects to.
`POST /api/v1/oauth/token` exchanges a key for a short-lived bearer token.

A token endpoint is a credential oracle, so most of these tests are about what it must REFUSE:
a secret presented under the wrong client_id, an admin JWT replayed as an API token, a token
outliving its revoked key, a tampered or expired token, and unthrottled guessing.
"""
from __future__ import annotations

import base64
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

import jwt  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app import oauth2_server  # noqa: E402
from app.auth import create_admin_jwt  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import preserve_storage_global  # noqa: E402


class OAuth2ServerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tempdir.cleanup)
        preserve_storage_global(self)
        self.storage = Storage(path=os.path.join(self.tempdir.name, "oauth.db"))
        app_main.storage = self.storage
        self.client = TestClient(app_main.app)
        self.addCleanup(self.client.close)
        self.tenant = self.storage.default_tenant_id
        issued = self.storage.generate_api_key_for_tenant(self.tenant)
        self.secret = issued["plaintext"]
        self.kid = self.storage.get_tenant_by_api_key(self.secret)["api_key"]["kid"]

    def _token(self, client_id=None, secret=None, basic=True, grant="client_credentials"):
        client_id = self.kid if client_id is None else client_id
        secret = self.secret if secret is None else secret
        data = {"grant_type": grant}
        headers = {}
        if basic:
            raw = f"{client_id}:{secret}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
        else:
            data.update(client_id=client_id, client_secret=secret)
        return self.client.post("/api/v1/oauth/token", data=data, headers=headers)

    def _call(self, token):
        return self.client.get("/api/v1/policies", headers={"Authorization": f"Bearer {token}"})

    # ── the happy path ─────────────────────────────────────────────────────

    def test_a_key_exchanges_for_a_bearer_token_that_works(self):
        response = self._token()
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["token_type"], "Bearer")
        self.assertGreater(body["expires_in"], 0)
        self.assertEqual(self._call(body["access_token"]).status_code, 200)

    def test_client_secret_post_is_accepted_too(self):
        response = self._token(basic=False)
        self.assertEqual(response.status_code, 200, response.text)

    def test_json_bodies_are_accepted(self):
        response = self.client.post("/api/v1/oauth/token", json={
            "grant_type": "client_credentials", "client_id": self.kid, "client_secret": self.secret})
        self.assertEqual(response.status_code, 200, response.text)

    def test_the_token_acts_as_the_key_it_came_from(self):
        """Audit attribution and role must be the KEY's, not a new anonymous identity."""
        token = self._token().json()["access_token"]
        claims = oauth2_server.verify(token)
        self.assertEqual(claims["sub"], self.kid)
        self.assertEqual(claims["tid"], self.tenant)

    # ── refusals ───────────────────────────────────────────────────────────

    def test_a_wrong_secret_is_invalid_client_with_www_authenticate(self):
        response = self._token(secret="rm_live_not_a_real_key")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "invalid_client")
        self.assertIn("WWW-Authenticate", response.headers, "RFC 6749 5.2 requires it on a 401")

    def test_a_valid_secret_under_the_WRONG_client_id_is_refused(self):
        """Without the binding the client_id is decorative: any key would mint a token under any
        id, and an audit line naming the client would name the wrong key."""
        other = self.storage.generate_api_key_for_tenant(self.tenant)["plaintext"]
        response = self._token(client_id=self.kid, secret=other)
        self.assertEqual(response.status_code, 401)

    def test_an_unsupported_grant_is_refused_with_the_rfc_error(self):
        response = self._token(grant="password")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "unsupported_grant_type")

    def test_the_error_body_satisfies_both_rfc_6749_and_the_envelope(self):
        body = self._token(secret="nope").json()
        self.assertIn("error_description", body)   # RFC 6749 section 5.2
        self.assertIn("detail", body)              # this API's envelope
        self.assertIsInstance(body["error"], str)  # both

    # ── a token is only as good as its key ─────────────────────────────────

    def test_revoking_the_key_kills_its_tokens(self):
        token = self._token().json()["access_token"]
        self.assertEqual(self._call(token).status_code, 200)
        self.assertTrue(self.storage.revoke_api_key(self.tenant, self.kid))
        self.assertEqual(self._call(token).status_code, 401,
                         "a token outlived the key it was minted from")

    def test_a_role_change_on_the_key_applies_to_its_tokens(self):
        token = self._token().json()["access_token"]
        # Use it first: that warms the kid cache, which is what a stale role would be served from.
        # Against a cold cache this test passed with the invalidation deleted.
        self.assertEqual(self._call(token).status_code, 200)
        self.storage.update_api_key_role(self.tenant, self.kid, "viewer")
        response = self.client.post("/api/v1/policies", json={"name": "x", "steps": []},
                                    headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 403, "the token kept the key's old, larger role")

    def test_an_expired_token_is_refused(self):
        with mock.patch.object(oauth2_server.time, "time", return_value=time.time() - 7200):
            token = self._token().json()["access_token"]
        self.assertEqual(self._call(token).status_code, 401)

    def test_a_tampered_token_is_refused(self):
        token = self._token().json()["access_token"]
        head, payload, sig = token.split(".")
        forged = jwt.encode({**jwt.decode(token, options={"verify_signature": False}), "tid": "someone-else"},
                            "wrong-key", algorithm="HS256")
        self.assertEqual(self._call(f"{head}.{forged.split('.')[1]}.{sig}").status_code, 401)
        self.assertEqual(self._call(forged).status_code, 401)

    def test_an_admin_jwt_is_NOT_accepted_as_an_api_token(self):
        """Domain separation. Admin sessions are HS256 JWTs signed with the admin secret; if access
        tokens used the same key, a platform-admin token would replay as a tenant API token."""
        admin = create_admin_jwt("admin", "a@example.com")
        self.assertEqual(self._call(admin).status_code, 401)

    def test_a_token_signed_with_the_raw_admin_secret_is_refused(self):
        """The strongest form of the above: correct claims, correct typ, WRONG key domain."""
        claims = {"iss": "rulemind", "typ": "rm_access", "sub": self.kid, "tid": self.tenant,
                  "iat": int(time.time()), "exp": int(time.time()) + 600}
        raw_secret = os.environ.get("RULEMIND_ADMIN_JWT_SECRET", "rulemind-admin-dev-secret")
        self.assertEqual(self._call(jwt.encode(claims, raw_secret, algorithm="HS256")).status_code, 401)

    def _mint(self, **overrides):
        """A token signed with the REAL access-token key, so only the claim under test is wrong."""
        now = int(time.time())
        claims = {"iss": "rulemind", "typ": "rm_access", "sub": self.kid, "tid": self.tenant,
                  "iat": now, "exp": now + 600, **overrides}
        return jwt.encode(claims, oauth2_server._signing_key(), algorithm="HS256")

    def test_a_correctly_signed_token_naming_ANOTHER_tenant_is_refused(self):
        """The key is re-resolved by kid; the tenant in the token must match the key's tenant.
        Otherwise a token is authority over whatever tenant its claims name."""
        other = self.storage.create_tenant("Other")["id"]
        self.assertEqual(self._call(self._mint()).status_code, 200, "the control must work")
        self.assertEqual(self._call(self._mint(tid=other)).status_code, 401)

    def test_a_correctly_signed_token_of_another_type_is_refused(self):
        """`typ` is what keeps a future token kind signed with this key from passing as an
        access token."""
        self.assertEqual(self._call(self._mint(typ="rm_refresh")).status_code, 401)

    def test_an_access_token_is_NOT_accepted_as_an_admin_jwt(self):
        """The reverse direction of domain separation: a tenant key's token must not open the
        platform-admin surface."""
        from app.auth import decode_admin_jwt

        token = self._token().json()["access_token"]
        with self.assertRaises(jwt.PyJWTError):
            decode_admin_jwt(token)

    def test_a_member_session_token_still_works(self):
        """The bearer path is shared with human sessions; adding JWTs must not break them."""
        self.assertFalse(oauth2_server.looks_like_access_token("rms_abcdef"))

    # ── the endpoint itself ───────────────────────────────────────────────

    def test_the_token_endpoint_needs_no_prior_auth(self):
        """It is how a client authenticates. It must not demand the credential it issues."""
        self.assertEqual(self._token().status_code, 200)

    def test_the_token_endpoint_is_rate_limited_like_login(self):
        """It accepts a guess -- the client secret -- so unthrottled it is an API-key oracle."""
        from app import middleware

        self.assertIn("/api/v1/oauth/token", middleware.CREDENTIAL_PATHS)
        self.assertEqual(middleware._pre_auth_limit("/api/v1/oauth/token")[0], "login")


if __name__ == "__main__":
    unittest.main()
