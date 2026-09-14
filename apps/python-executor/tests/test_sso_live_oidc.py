"""OIDC against a REAL HTTP identity provider over real sockets (#127).

tests/test_sso.py already does genuine crypto -- real RS256, a real JWKS, real XML-DSig. What
it does not do is any networking: `sso._GET_JSON` and `sso._POST_FORM` (sso.py:41-56, both
marked `# pragma: no cover - network`) are monkeypatched in every one of its tests. So
discovery, the token exchange and the JWKS fetch -- the three places this code actually talks
to an identity provider -- have never executed.

This file starts a real OIDC provider on 127.0.0.1 (stdlib ThreadingHTTPServer, RSA key from
`cryptography`, tokens signed with PyJWT -- all already pinned) and leaves the transport
functions at their defaults, so httpx performs the round trip for real.

Deliberately not mocked: a patched transport returns whatever shape the test author already
believed in, which is the assumption under doubt. It is also why the negatives matter more
than the happy path here -- a wrong `aud`, an expired `exp` and a mismatched `nonce` are
exactly what a mock waves through.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.pop("AUTH_MODE", None)

import jwt  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from app import sso  # noqa: E402

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_KID = "live-test-key"


def _jwk() -> dict:
    """Public key as a JWK. PyJWT can emit this directly, so the test does not hand-roll the
    base64url encoding of n and e -- getting that subtly wrong would make the test pass or
    fail for reasons unrelated to the code under test."""
    pub = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_KEY.public_key()))
    pub.update({"kid": _KID, "use": "sig", "alg": "RS256"})
    return pub


class _Provider:
    """A real OIDC provider: discovery, JWKS and a token endpoint, over real sockets."""

    def __init__(self):
        self.claims_override: dict = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep the test output readable
                pass

            def _send(self, payload: dict, status: int = 200):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.startswith("/.well-known/openid-configuration"):
                    self._send({
                        "issuer": outer.issuer,
                        "authorization_endpoint": f"{outer.issuer}/authorize",
                        "token_endpoint": f"{outer.issuer}/token",
                        "jwks_uri": f"{outer.issuer}/jwks",
                    })
                elif self.path.startswith("/jwks"):
                    self._send({"keys": [_jwk()]})
                else:
                    self._send({"error": "not_found"}, 404)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                form = parse_qs(self.rfile.read(length).decode())
                if not self.path.startswith("/token"):
                    return self._send({"error": "not_found"}, 404)
                # The nonce is carried in the auth code so the provider can echo it back,
                # which is how a real IdP binds the ID token to the original request.
                code = (form.get("code") or [""])[0]
                nonce = code.split(":", 1)[1] if ":" in code else ""
                now = int(time.time())
                claims = {
                    "iss": outer.issuer,
                    "aud": "rulemind-client",
                    "sub": "user-123",
                    "email": "sso.user@example.com",
                    "name": "SSO User",
                    "nonce": nonce,
                    "iat": now,
                    "exp": now + 300,
                }
                claims.update(outer.claims_override)
                token = jwt.encode(claims, _KEY, algorithm="RS256", headers={"kid": _KID})
                self._send({"id_token": token, "access_token": "at", "token_type": "Bearer"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.issuer = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class LiveOidcTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.provider = _Provider()

    @classmethod
    def tearDownClass(cls):
        cls.provider.stop()

    def setUp(self):
        self.provider.claims_override = {}
        self.config = {
            "issuer": self.provider.issuer,
            "client_id": "rulemind-client",
            "client_secret": "shh",
            "redirect_uri": "http://127.0.0.1:3100/sso/callback",
        }

    # ── the seam that had never executed ────────────────────────────────────

    def test_discovery_runs_over_real_http(self):
        """sso.discover_oidc -> _GET_JSON -> httpx. Left at its default here, so this is the
        first time that function has run in a test."""
        endpoints = sso.discover_oidc(self.config)
        self.assertEqual(endpoints["issuer"], self.provider.issuer)
        self.assertEqual(endpoints["token_endpoint"], f"{self.provider.issuer}/token")
        self.assertEqual(endpoints["jwks_uri"], f"{self.provider.issuer}/jwks")

    def test_explicit_endpoints_skip_discovery(self):
        """When every endpoint is configured, no discovery request should be made at all --
        asserted by pointing the issuer at a port nothing is listening on."""
        config = dict(self.config)
        config.update({
            "issuer": "http://127.0.0.1:1",
            "authorization_endpoint": "http://x/a",
            "token_endpoint": "http://x/t",
            "jwks_uri": "http://x/j",
        })
        endpoints = sso.discover_oidc(config)
        self.assertEqual(endpoints["token_endpoint"], "http://x/t")

    def test_full_login_exchanges_the_code_and_verifies_the_id_token(self):
        nonce = "nonce-abc"
        claims = sso.complete_oidc_login(self.config, code=f"authcode:{nonce}", nonce=nonce)
        self.assertEqual(claims["email"], "sso.user@example.com")
        self.assertEqual(claims["name"], "SSO User")
        self.assertEqual(claims["sub"], "user-123")

    # ── the negatives a mocked transport waves through ──────────────────────

    def test_a_token_for_another_client_is_rejected(self):
        """Wrong `aud` means the token was minted for a different application. Accepting it
        would let any client of the same IdP log in as anyone here."""
        self.provider.claims_override = {"aud": "some-other-app"}
        with self.assertRaises(sso.SsoError):
            sso.complete_oidc_login(self.config, code="authcode:n", nonce="n")

    def test_an_expired_token_is_rejected(self):
        now = int(time.time())
        self.provider.claims_override = {"exp": now - 60, "iat": now - 600}
        with self.assertRaises(sso.SsoError):
            sso.complete_oidc_login(self.config, code="authcode:n", nonce="n")

    def test_a_replayed_nonce_is_rejected(self):
        """The nonce binds the ID token to the login request this browser started. Without
        the check, a token captured from one session can be replayed into another."""
        with self.assertRaises(sso.SsoError):
            sso.complete_oidc_login(self.config, code="authcode:issued-for-a", nonce="expected-b")

    def test_a_token_from_the_wrong_issuer_is_rejected(self):
        self.provider.claims_override = {"iss": "https://evil.example.com"}
        with self.assertRaises(sso.SsoError):
            sso.complete_oidc_login(self.config, code="authcode:n", nonce="n")


if __name__ == "__main__":
    unittest.main()
