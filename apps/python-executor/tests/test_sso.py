"""Enterprise SSO: OIDC (ID-token verification) and SAML 2.0 (assertion signature).

The network is never touched — OIDC discovery/JWKS/token-exchange go through the
module's injectable helpers, and both protocols' *cryptographic* verification runs
for real: a genuine RS256 ID token checked against a JWKS, and a genuine XML-DSig
signature over a SAML Response checked against a self-signed X.509 certificate.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from lxml import etree
from signxml import XMLSigner

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

import app.main as app_main  # noqa: E402
import app.sso as sso  # noqa: E402
from app.storage import Storage  # noqa: E402


def _rsa_keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                            serialization.NoEncryption()).decode()
    return key, pem


def _self_signed_cert(key):
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "idp.example.com")])
    cert = (
        x509.CertificateBuilder().subject_name(subject).issuer_name(issuer)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime.utcnow() - dt.timedelta(days=1))
        .not_valid_after(dt.datetime.utcnow() + dt.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


class SsoUnitTests(unittest.TestCase):
    def test_state_roundtrip(self):
        token = sso.issue_state("t1", "oidc", "nonce123", "https://app/cb")
        claims = sso.verify_state(token)
        self.assertEqual(claims["t"], "t1")
        self.assertEqual(claims["n"], "nonce123")

    def test_tampered_state_rejected(self):
        with self.assertRaises(sso.SsoError):
            sso.verify_state("not-a-token")

    def test_domain_allow_list(self):
        self.assertTrue(sso.email_domain_allowed("a@acme.com", []))          # empty = any
        self.assertTrue(sso.email_domain_allowed("a@acme.com", ["acme.com"]))
        self.assertFalse(sso.email_domain_allowed("a@evil.com", ["acme.com"]))

    def test_oidc_id_token_verification(self):
        key, pem = _rsa_keypair()
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        jwk["kid"] = "k1"
        jwks = {"keys": [jwk]}
        now = dt.datetime.now(dt.timezone.utc)
        id_token = jwt.encode(
            {"iss": "https://idp", "aud": "client-1", "email": "u@acme.com", "name": "U",
             "nonce": "n1", "iat": now, "exp": now + dt.timedelta(minutes=5)},
            pem, algorithm="RS256", headers={"kid": "k1"},
        )
        claims = sso.verify_oidc_id_token(id_token, jwks, issuer="https://idp", client_id="client-1", nonce="n1")
        self.assertEqual(claims["email"], "u@acme.com")

    def test_oidc_nonce_mismatch_rejected(self):
        key, pem = _rsa_keypair()
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())); jwk["kid"] = "k1"
        now = dt.datetime.now(dt.timezone.utc)
        id_token = jwt.encode({"iss": "https://idp", "aud": "client-1", "email": "u@acme.com",
                               "nonce": "n1", "iat": now, "exp": now + dt.timedelta(minutes=5)},
                              pem, algorithm="RS256", headers={"kid": "k1"})
        with self.assertRaises(sso.SsoError):
            sso.verify_oidc_id_token(id_token, {"keys": [jwk]}, issuer="https://idp", client_id="client-1", nonce="WRONG")

    def test_oidc_wrong_key_rejected(self):
        _, pem = _rsa_keypair()
        other_key, _ = _rsa_keypair()
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(other_key.public_key())); jwk["kid"] = "k1"
        now = dt.datetime.now(dt.timezone.utc)
        id_token = jwt.encode({"iss": "https://idp", "aud": "client-1", "email": "u@acme.com",
                               "iat": now, "exp": now + dt.timedelta(minutes=5)},
                              pem, algorithm="RS256", headers={"kid": "k1"})
        with self.assertRaises(sso.SsoError):
            sso.verify_oidc_id_token(id_token, {"keys": [jwk]}, issuer="https://idp", client_id="client-1")


def _signed_saml_response(cert_pem, key_pem, email, name="Jane Doe"):
    ns = 'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"'
    xml = (
        f'<samlp:Response {ns} ID="_resp1" Version="2.0" IssueInstant="2026-01-01T00:00:00Z">'
        f'<saml:Issuer>https://idp.example.com</saml:Issuer>'
        f'<saml:Assertion ID="_assert1" Version="2.0" IssueInstant="2026-01-01T00:00:00Z">'
        f'<saml:Issuer>https://idp.example.com</saml:Issuer>'
        f'<saml:Subject><saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress">{email}</saml:NameID></saml:Subject>'
        f'<saml:AttributeStatement>'
        f'<saml:Attribute Name="email"><saml:AttributeValue>{email}</saml:AttributeValue></saml:Attribute>'
        f'<saml:Attribute Name="name"><saml:AttributeValue>{name}</saml:AttributeValue></saml:Attribute>'
        f'</saml:AttributeStatement>'
        f'</saml:Assertion></samlp:Response>'
    )
    root = etree.fromstring(xml.encode())
    signed = XMLSigner().sign(root, key=key_pem.encode(), cert=cert_pem.encode())
    return base64.b64encode(etree.tostring(signed)).decode()


class SamlUnitTests(unittest.TestCase):
    def test_saml_signature_verified_and_email_extracted(self):
        key, pem = _rsa_keypair()
        cert = _self_signed_cert(key)
        resp = _signed_saml_response(cert, pem, "jane@acme.com")
        identity = sso.parse_and_verify_saml_response({"idp_cert": cert}, resp)
        self.assertEqual(identity["email"], "jane@acme.com")
        self.assertEqual(identity["name"], "Jane Doe")

    def test_saml_wrong_cert_rejected(self):
        key, pem = _rsa_keypair()
        cert = _self_signed_cert(key)
        resp = _signed_saml_response(cert, pem, "jane@acme.com")
        other_key, _ = _rsa_keypair()
        other_cert = _self_signed_cert(other_key)  # different signer
        with self.assertRaises(sso.SsoError):
            sso.parse_and_verify_saml_response({"idp_cert": other_cert}, resp)

    def test_saml_tampered_payload_rejected(self):
        key, pem = _rsa_keypair()
        cert = _self_signed_cert(key)
        resp = _signed_saml_response(cert, pem, "jane@acme.com")
        raw = base64.b64decode(resp).replace(b"jane@acme.com", b"attacker@acme.com")
        with self.assertRaises(sso.SsoError):
            sso.parse_and_verify_saml_response({"idp_cert": cert}, base64.b64encode(raw).decode())


class SsoApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "sso.db"))
        self.client = TestClient(app_main.app)
        self.tenant_id = app_main.storage.default_tenant_id
        self.admin = {"x-api-key": app_main.storage.default_api_key or ""}
        self._saved = (sso._GET_JSON, sso._POST_FORM)

    def tearDown(self):
        sso._GET_JSON, sso._POST_FORM = self._saved
        self.client.close()
        self.tempdir.cleanup()

    def test_config_crud_masks_secret(self):
        r = self.client.put("/api/v1/access/sso", headers=self.admin, json={
            "provider": "oidc", "enabled": True, "issuer": "https://idp", "client_id": "c1",
            "client_secret": "topsecret", "redirect_uri": "https://app/cb", "allowed_domains": ["acme.com"],
            "authorization_endpoint": "https://idp/auth", "token_endpoint": "https://idp/token", "jwks_uri": "https://idp/jwks",
        })
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("client_secret", r.json())
        self.assertTrue(r.json()["client_secret_set"])
        got = self.client.get("/api/v1/access/sso", headers=self.admin).json()
        self.assertEqual(got["client_id"], "c1")
        self.assertTrue(got["enabled"])

    def test_start_returns_authorize_url(self):
        self.client.put("/api/v1/access/sso", headers=self.admin, json={
            "provider": "oidc", "enabled": True, "client_id": "c1", "redirect_uri": "https://app/cb",
            "authorization_endpoint": "https://idp/auth", "token_endpoint": "https://idp/token",
            "jwks_uri": "https://idp/jwks", "issuer": "https://idp",
        })
        r = self.client.get(f"/api/v1/auth/sso/start?tenant_id={self.tenant_id}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("https://idp/auth?", r.json()["redirect_url"])
        self.assertIn("client_id=c1", r.json()["redirect_url"])

    def test_start_404_when_disabled(self):
        self.assertEqual(self.client.get(f"/api/v1/auth/sso/start?tenant_id={self.tenant_id}").status_code, 404)

    def test_oidc_callback_jit_provisions_and_logs_in(self):
        key, pem = _rsa_keypair()
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())); jwk["kid"] = "k1"
        self.client.put("/api/v1/access/sso", headers=self.admin, json={
            "provider": "oidc", "enabled": True, "client_id": "c1", "redirect_uri": "https://app/cb",
            "authorization_endpoint": "https://idp/auth", "token_endpoint": "https://idp/token",
            "jwks_uri": "https://idp/jwks", "issuer": "https://idp",
            "allowed_domains": ["acme.com"], "default_role": "reviewer", "jit_provisioning": True,
        })
        nonce = "nonce-xyz"
        state = sso.issue_state(self.tenant_id, "oidc", nonce, "https://app/cb")
        now = dt.datetime.now(dt.timezone.utc)
        id_token = jwt.encode({"iss": "https://idp", "aud": "c1", "email": "newuser@acme.com", "name": "New User",
                               "nonce": nonce, "iat": now, "exp": now + dt.timedelta(minutes=5)},
                              pem, algorithm="RS256", headers={"kid": "k1"})
        sso._POST_FORM = lambda url, data: {"id_token": id_token}
        sso._GET_JSON = lambda url: {"keys": [jwk]}
        r = self.client.post("/api/v1/auth/sso/oidc/callback", json={"code": "abc", "state": state})
        self.assertEqual(r.status_code, 200, r.text)
        token = r.json()["token"]
        self.assertEqual(r.json()["member"]["role"], "reviewer")  # provisioned at default role
        # the issued session authenticates the API
        me = self.client.get("/api/v1/access/me", headers={"Authorization": "Bearer " + token})
        self.assertEqual(me.json()["role"], "reviewer")

    def test_oidc_callback_rejects_disallowed_domain(self):
        key, pem = _rsa_keypair()
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())); jwk["kid"] = "k1"
        self.client.put("/api/v1/access/sso", headers=self.admin, json={
            "provider": "oidc", "enabled": True, "client_id": "c1", "redirect_uri": "https://app/cb",
            "authorization_endpoint": "https://idp/auth", "token_endpoint": "https://idp/token",
            "jwks_uri": "https://idp/jwks", "issuer": "https://idp", "allowed_domains": ["acme.com"],
        })
        nonce = "n2"
        state = sso.issue_state(self.tenant_id, "oidc", nonce, "https://app/cb")
        now = dt.datetime.now(dt.timezone.utc)
        id_token = jwt.encode({"iss": "https://idp", "aud": "c1", "email": "x@evil.com", "nonce": nonce,
                               "iat": now, "exp": now + dt.timedelta(minutes=5)},
                              pem, algorithm="RS256", headers={"kid": "k1"})
        sso._POST_FORM = lambda url, data: {"id_token": id_token}
        sso._GET_JSON = lambda url: {"keys": [jwk]}
        r = self.client.post("/api/v1/auth/sso/oidc/callback", json={"code": "abc", "state": state})
        self.assertEqual(r.status_code, 403)

    def test_saml_acs_logs_in(self):
        key, pem = _rsa_keypair()
        cert = _self_signed_cert(key)
        self.client.put("/api/v1/access/sso", headers=self.admin, json={
            "provider": "saml", "enabled": True, "sp_entity_id": "rulemind", "sso_url": "https://idp/sso",
            "acs_url": "https://app/acs", "idp_cert": cert, "default_role": "policy_maker", "jit_provisioning": True,
        })
        state = sso.issue_state(self.tenant_id, "saml", "n", "https://app/acs")
        resp = _signed_saml_response(cert, pem, "sam@acme.com", name="Sam Ople")
        r = self.client.post("/api/v1/auth/sso/saml/acs", json={"saml_response": resp, "relay_state": state})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["member"]["email"], "sam@acme.com")
        self.assertEqual(r.json()["member"]["role"], "policy_maker")

    def test_sso_endpoints_are_public(self):
        # No API key needed to reach the SSO login endpoints — they self-verify.
        # A bad state is a 401 (auth failure), NOT a 401 "Missing API key" from the
        # middleware: reaching the handler at all proves the endpoint is public.
        r = self.client.post("/api/v1/auth/sso/oidc/callback", json={"code": "x", "state": "bad"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("state", r.text.lower())  # handler-level message, not "Missing API key"

    def test_availability_probe_is_public_and_leaks_nothing(self):
        self.client.put("/api/v1/access/sso", headers=self.admin, json={
            "provider": "oidc", "enabled": True, "client_id": "c1", "client_secret": "s",
            "issuer": "https://idp", "authorization_endpoint": "https://idp/a", "token_endpoint": "https://idp/t", "jwks_uri": "https://idp/j",
        })
        r = self.client.get(f"/api/v1/auth/sso/available?tenant_id={self.tenant_id}")  # no key
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["enabled"])
        self.assertEqual(r.json()["provider"], "oidc")
        self.assertNotIn("client", r.text)  # no client_id / secret leaked


if __name__ == "__main__":
    unittest.main()
