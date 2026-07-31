"""In-app mutual-TLS client-certificate verification.

The app enforces mTLS itself (independent of API-key auth) when an operator opts in, so a
request to a guarded path is rejected unless it carries an allow-listed, unexpired client
cert forwarded by the terminating proxy. These tests drive each mode with real X.509 certs
generated in-process, plus one end-to-end pass through the ASGI middleware.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import unittest
from pathlib import Path
from urllib.parse import quote

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app.mtls import MtlsSettings, evaluate_client_cert, fingerprint_sha256  # noqa: E402


def _make_cert(common_name: str = "client-a", *, not_before=None, not_after=None):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = dt.datetime.now(dt.timezone.utc)
    not_before = not_before or (now - dt.timedelta(days=1))
    not_after = not_after or (now + dt.timedelta(days=365))
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
    return cert, pem


class MtlsEvaluateTests(unittest.TestCase):
    def _settings(self, **env) -> MtlsSettings:
        base = {"MTLS_MODE": "required"}
        base.update(env)
        return MtlsSettings(env=base)

    def test_off_mode_always_passes(self):
        settings = MtlsSettings(env={"MTLS_MODE": "off"})
        self.assertFalse(settings.enabled)
        ok, reason, _ = evaluate_client_cert({}, settings)
        self.assertTrue(ok)
        self.assertEqual(reason, "disabled")

    def test_required_rejects_missing_cert(self):
        ok, reason, _ = evaluate_client_cert({}, self._settings())
        self.assertFalse(ok)
        self.assertEqual(reason, "client_cert_missing")

    def test_required_accepts_valid_cert_when_no_allowlist(self):
        _, pem = _make_cert()
        ok, reason, identity = evaluate_client_cert({"x-client-cert": pem}, self._settings())
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "ok")
        self.assertIn("fingerprint", identity)
        self.assertIn("client-a", identity["subject"])

    def test_allowlist_match_and_mismatch(self):
        cert, pem = _make_cert()
        fp = fingerprint_sha256(cert)
        allowed = self._settings(MTLS_ALLOWED_FINGERPRINTS=fp)
        ok, reason, _ = evaluate_client_cert({"x-client-cert": pem}, allowed)
        self.assertTrue(ok, reason)

        # A different cert whose fingerprint is not allow-listed is rejected.
        _, other_pem = _make_cert("client-b")
        ok, reason, ident = evaluate_client_cert({"x-client-cert": other_pem}, allowed)
        self.assertFalse(ok)
        self.assertEqual(reason, "client_cert_not_allowed")
        self.assertIn("fingerprint", ident)

    def test_allowlist_accepts_colon_separated_fingerprints(self):
        cert, pem = _make_cert()
        fp = fingerprint_sha256(cert)
        colonised = ":".join(fp[i : i + 2] for i in range(0, len(fp), 2)).upper()
        settings = self._settings(MTLS_ALLOWED_FINGERPRINTS=colonised)
        ok, reason, _ = evaluate_client_cert({"x-client-cert": pem}, settings)
        self.assertTrue(ok, reason)

    def test_expired_cert_rejected(self):
        now = dt.datetime.now(dt.timezone.utc)
        _, pem = _make_cert(not_before=now - dt.timedelta(days=10), not_after=now - dt.timedelta(days=1))
        ok, reason, _ = evaluate_client_cert({"x-client-cert": pem}, self._settings())
        self.assertFalse(ok)
        self.assertEqual(reason, "client_cert_expired")

    def test_url_encoded_pem_from_proxy(self):
        # nginx $ssl_client_escaped_cert forwards the PEM URL-encoded.
        cert, pem = _make_cert()
        settings = self._settings(MTLS_ALLOWED_FINGERPRINTS=fingerprint_sha256(cert))
        ok, reason, _ = evaluate_client_cert({"x-client-cert": quote(pem)}, settings)
        self.assertTrue(ok, reason)

    def test_single_line_pem_is_reconstructed(self):
        cert, pem = _make_cert()
        one_line = pem.replace("\n", " ")
        settings = self._settings(MTLS_ALLOWED_FINGERPRINTS=fingerprint_sha256(cert))
        ok, reason, _ = evaluate_client_cert({"x-client-cert": one_line}, settings)
        self.assertTrue(ok, reason)

    def test_optional_mode_allows_absent_but_still_rejects_bad_allowlisted(self):
        settings = MtlsSettings(env={"MTLS_MODE": "optional"})
        ok, reason, _ = evaluate_client_cert({}, settings)
        self.assertTrue(ok)
        self.assertEqual(reason, "optional_absent")

    def test_optional_mode_rejects_disallowed_cert(self):
        _, pem = _make_cert("intruder")
        settings = MtlsSettings(env={"MTLS_MODE": "optional", "MTLS_ALLOWED_FINGERPRINTS": "deadbeef"})
        ok, reason, _ = evaluate_client_cert({"x-client-cert": pem}, settings)
        self.assertFalse(ok)
        self.assertEqual(reason, "client_cert_not_allowed")

    def test_proxy_verify_required(self):
        cert, pem = _make_cert()
        settings = self._settings(MTLS_REQUIRE_PROXY_VERIFY="1")
        # verify header missing -> reject in required mode
        ok, reason, _ = evaluate_client_cert({"x-client-cert": pem}, settings)
        self.assertFalse(ok)
        self.assertEqual(reason, "proxy_verify_failed")
        # verify header SUCCESS -> accept
        ok, reason, _ = evaluate_client_cert(
            {"x-client-cert": pem, "x-client-verify": "SUCCESS"}, settings
        )
        self.assertTrue(ok, reason)

    def test_custom_header_name(self):
        cert, pem = _make_cert()
        settings = self._settings(MTLS_CERT_HEADER="ssl-client-cert")
        ok, reason, _ = evaluate_client_cert({"ssl-client-cert": pem}, settings)
        self.assertTrue(ok, reason)

    def test_guards_only_protected_prefixes(self):
        settings = MtlsSettings(env={"MTLS_MODE": "required"})
        self.assertTrue(settings.guards("/sdk/v1/health"))
        self.assertFalse(settings.guards("/api/v1/policies"))
        custom = MtlsSettings(env={"MTLS_MODE": "required", "MTLS_PROTECTED_PREFIXES": "/sdk/v1,/api/v1/decide"})
        self.assertTrue(custom.guards("/api/v1/decide"))


class MtlsMiddlewareTests(unittest.TestCase):
    """End-to-end through the ASGI stack: a guarded SDK path is 401 without a cert."""

    def setUp(self):
        os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
        os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
        os.environ.pop("AUTH_MODE", None)
        self._saved = {k: os.environ.get(k) for k in ("MTLS_MODE", "MTLS_ALLOWED_FINGERPRINTS")}

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_middleware_blocks_and_allows(self):
        from fastapi.testclient import TestClient

        import app.main as app_main

        cert, pem = _make_cert()
        os.environ["MTLS_MODE"] = "required"
        os.environ["MTLS_ALLOWED_FINGERPRINTS"] = fingerprint_sha256(cert)
        client = TestClient(app_main.app)

        api_key = app_main.storage.default_api_key or ""

        # No client cert -> blocked by mTLS before the API-key check even runs.
        blocked = client.get("/sdk/v1/health", headers={"x-api-key": api_key})
        self.assertEqual(blocked.status_code, 401)
        self.assertEqual(blocked.json().get("error"), "Client certificate required")

        # Valid, allow-listed cert + API key -> passes mTLS and auth.
        allowed = client.get(
            "/sdk/v1/health",
            headers={"x-api-key": api_key, "x-client-cert": quote(pem)},
        )
        self.assertEqual(allowed.status_code, 200)

        # Wrong (non-allow-listed) cert -> still blocked even with a valid API key.
        _, other_pem = _make_cert("intruder")
        intruder = client.get(
            "/sdk/v1/health",
            headers={"x-api-key": api_key, "x-client-cert": quote(other_pem)},
        )
        self.assertEqual(intruder.status_code, 401)


if __name__ == "__main__":
    unittest.main()
