"""Outbound client certificates from a PKCS#12 bundle (parity P02).

`app/mtls.py` is INBOUND mutual TLS -- it evaluates a certificate a caller presents TO this
server. This is the opposite direction, and nothing did it: there was no `cert=` anywhere in the
codebase, so a datasource behind mutual TLS could not be connected to, and the failure surfaced
as an opaque handshake error rather than as "you have not configured a client certificate".

THE TEST THAT MATTERS MOST IS `test_no_private_key_survives_on_disk`. Python's `ssl` module can
only build a client identity through `SSLContext.load_cert_chain`, which reads from PATHS and has
no in-memory equivalent, so the decrypted PEM has to touch a file descriptor. What the
implementation controls is how narrow that window is -- 0600 from mkstemp, unlinked immediately
after load, unlinked in a `finally` so a failed load still removes it -- and a test that only
checked "the context builds" would not notice any of that regressing.
"""
from __future__ import annotations

import base64
import datetime
import glob
import os
import ssl
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.hazmat.primitives.serialization import pkcs12  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

from app import client_certs  # noqa: E402


def make_p12(passphrase: str = "", common_name: str = "rulemind-client", days: int = 30) -> str:
    """A real, self-signed PKCS#12 bundle. Generated rather than committed: a checked-in bundle
    is a private key in the repository, and a generated one also lets a test choose expiry."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=days))
        .sign(key, hashes.SHA256())
    )
    encryption = (
        serialization.BestAvailableEncryption(passphrase.encode()) if passphrase
        else serialization.NoEncryption())
    blob = pkcs12.serialize_key_and_certificates(
        name=b"rulemind", key=key, cert=cert, cas=None, encryption_algorithm=encryption)
    return base64.b64encode(blob).decode()


def connector(p12=None, passphrase=None, connector_id="conn1"):
    config = {}
    if p12 is not None:
        config["client_cert_p12"] = p12
    if passphrase is not None:
        config["client_cert_passphrase"] = passphrase
    return {"id": connector_id, "name": "partner", "config": config}


class _Base(unittest.TestCase):
    def setUp(self):
        client_certs.reset_cache()

    def tearDown(self):
        client_certs.reset_cache()


class LoadIdentityTest(_Base):

    def test_an_unprotected_bundle_loads(self):
        pem, cn = client_certs.load_identity(make_p12())
        self.assertIn(b"PRIVATE KEY", pem)
        self.assertIn(b"CERTIFICATE", pem)
        self.assertEqual(cn, "rulemind-client")

    def test_a_passphrase_protected_bundle_loads(self):
        pem, _ = client_certs.load_identity(make_p12(passphrase="hunter2"), "hunter2")
        self.assertIn(b"PRIVATE KEY", pem)

    def test_the_wrong_passphrase_is_a_named_failure(self):
        with self.assertRaises(client_certs.ClientCertificateInvalid) as caught:
            client_certs.load_identity(make_p12(passphrase="hunter2"), "wrong")
        self.assertEqual(caught.exception.code, "unreadable")

    def test_the_error_does_not_echo_the_bundle_or_the_library_message(self):
        """"mac verify failure" tells an operator nothing they can act on, and the underlying
        exception can carry fragments of the bundle."""
        bundle = make_p12(passphrase="hunter2")
        with self.assertRaises(client_certs.ClientCertificateInvalid) as caught:
            client_certs.load_identity(bundle, "wrong")
        message = str(caught.exception)
        self.assertNotIn(bundle[:32], message)
        self.assertIn("passphrase", message)

    def test_something_that_is_not_base64_is_refused(self):
        with self.assertRaises(client_certs.ClientCertificateInvalid) as caught:
            client_certs.load_identity("this is not base64!!")
        self.assertEqual(caught.exception.code, "not_base64")

    def test_valid_base64_that_is_not_a_p12_is_refused(self):
        with self.assertRaises(client_certs.ClientCertificateInvalid) as caught:
            client_certs.load_identity(base64.b64encode(b"hello world").decode())
        self.assertEqual(caught.exception.code, "unreadable")

    def test_an_empty_bundle_is_refused(self):
        with self.assertRaises(client_certs.ClientCertificateInvalid) as caught:
            client_certs.load_identity("")
        self.assertEqual(caught.exception.code, "empty")

    def test_the_chain_is_included_when_the_bundle_has_one(self):
        """Without the intermediates a partner that does not have the chain cached rejects the
        handshake, and the error looks like a bad certificate rather than a missing one."""
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "leaf")])
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "intermediate")])
        now = datetime.datetime.now(datetime.timezone.utc)

        def build(subject, issuer, signer):
            return (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer)
                    .public_key(key.public_key()).serial_number(x509.random_serial_number())
                    .not_valid_before(now - datetime.timedelta(days=1))
                    .not_valid_after(now + datetime.timedelta(days=30)).sign(signer, hashes.SHA256()))

        ca = build(ca_name, ca_name, key)
        leaf = build(name, ca_name, key)
        blob = pkcs12.serialize_key_and_certificates(
            name=b"x", key=key, cert=leaf, cas=[ca],
            encryption_algorithm=serialization.NoEncryption())
        pem, _ = client_certs.load_identity(base64.b64encode(blob).decode())
        self.assertEqual(pem.count(b"BEGIN CERTIFICATE"), 2,
                         "the intermediate was dropped from the identity")


class ContextTest(_Base):

    def test_a_connector_without_a_bundle_gets_no_context(self):
        """Every existing connector. This must stay None or ordinary HTTPS changes behaviour."""
        self.assertIsNone(client_certs.context_for(connector()))
        self.assertIsNone(client_certs.context_for({"id": "x"}))
        self.assertIsNone(client_certs.context_for({"id": "x", "config": None}))

    def test_a_configured_connector_gets_a_real_ssl_context(self):
        context = client_certs.context_for(connector(make_p12()), "t1")
        self.assertIsInstance(context, ssl.SSLContext)

    def test_server_verification_is_not_weakened(self):
        """`verify=<SSLContext>` reads like an override of httpx's default verification. It is
        not one -- the context starts from ssl.create_default_context() -- and that is worth an
        assertion rather than a comment, because turning verification off here would be silent."""
        context = client_certs.context_for(connector(make_p12()), "t1")
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_a_mismatched_or_unreadable_bundle_raises_rather_than_silently_skipping(self):
        with self.assertRaises(client_certs.ClientCertificateInvalid):
            client_certs.context_for(connector(base64.b64encode(b"nope").decode()), "t1")

    def test_configured_reports_whether_a_bundle_is_present(self):
        self.assertTrue(client_certs.configured(connector(make_p12())))
        self.assertFalse(client_certs.configured(connector()))


class TheKeyDoesNotSurviveOnDiskTest(_Base):
    """The security property the temp-file approach has to earn."""

    def test_no_private_key_survives_on_disk(self):
        before = set(glob.glob(os.path.join(tempfile.gettempdir(), "rulemind-clientcert-*")))
        client_certs.context_for(connector(make_p12()), "t1")
        after = set(glob.glob(os.path.join(tempfile.gettempdir(), "rulemind-clientcert-*")))
        self.assertEqual(after - before, set(),
                         "a decrypted private key was left in the temp directory")

    def test_the_file_is_removed_even_when_loading_fails(self):
        """In a `finally`. A load failure is exactly when a leftover key is most likely and least
        noticed."""
        before = set(glob.glob(os.path.join(tempfile.gettempdir(), "rulemind-clientcert-*")))
        with mock.patch.object(ssl.SSLContext, "load_cert_chain",
                               side_effect=ssl.SSLError("mismatched")):
            with self.assertRaises(client_certs.ClientCertificateInvalid):
                client_certs.context_for(connector(make_p12()), "t1")
        after = set(glob.glob(os.path.join(tempfile.gettempdir(), "rulemind-clientcert-*")))
        self.assertEqual(after - before, set(),
                         "a failed load left a decrypted private key on disk")

    def test_the_file_is_never_group_or_world_readable_while_it_exists(self):
        """0600 from mkstemp, and written through the fd we already own so there is no window in
        which the path exists with looser permissions."""
        seen = {}
        real_load = ssl.SSLContext.load_cert_chain

        def capture(self_inner, certfile, *a, **k):
            seen["mode"] = os.stat(certfile).st_mode & 0o777
            seen["dir"] = os.path.dirname(certfile)
            return real_load(self_inner, certfile, *a, **k)

        with mock.patch.object(ssl.SSLContext, "load_cert_chain", capture):
            client_certs.context_for(connector(make_p12()), "t1")
        self.assertEqual(seen["mode"], 0o600,
                         "the key file was readable by someone other than the owner")
        self.assertEqual(seen["dir"], tempfile.gettempdir(),
                         "the key was written outside the system temp directory")


class CacheTest(_Base):

    def test_the_context_is_reused_rather_than_rebuilt_per_request(self):
        """A PKCS#12 unwrap is a deliberately slow PBKDF2. A decision path cannot pay it per
        request."""
        bundle = make_p12()
        first = client_certs.context_for(connector(bundle), "t1")
        with mock.patch.object(client_certs, "_build_context",
                               side_effect=AssertionError("rebuilt a cached context")):
            second = client_certs.context_for(connector(bundle), "t1")
        self.assertIs(first, second)

    def test_rotating_the_bundle_takes_effect_without_a_restart(self):
        """The reason the cache key carries the bundle fingerprint and not just the connector id:
        keyed on the id alone, a rotated connector keeps presenting the REVOKED certificate."""
        first = client_certs.context_for(connector(make_p12(), connector_id="c1"), "t1")
        second = client_certs.context_for(connector(make_p12(), connector_id="c1"), "t1")
        self.assertIsNot(first, second, "the rotated bundle reused the old context")

    def test_correcting_the_passphrase_builds_a_new_context(self):
        bundle = make_p12(passphrase="right")
        with self.assertRaises(client_certs.ClientCertificateInvalid):
            client_certs.context_for(connector(bundle, "wrong"), "t1")
        self.assertIsInstance(client_certs.context_for(connector(bundle, "right"), "t1"),
                              ssl.SSLContext)

    def test_two_tenants_never_share_a_context(self):
        """Connector ids are seeded identically per tenant ("bureau", "bank"), so a cache keyed
        without the tenant would present tenant A's client certificate on tenant B's calls."""
        bundle = make_p12()
        a = client_certs.context_for(connector(bundle, connector_id="bureau"), "tenant-a")
        b = client_certs.context_for(connector(bundle, connector_id="bureau"), "tenant-b")
        self.assertIsNot(a, b, "one tenant's client identity was reused for another")


class TheBundleIsTreatedAsASecretTest(_Base):
    """Found while wiring this, and it was a real hole in it.

    `SECRET_FIELD_MARKERS` is a SUBSTRING test over config key names, and it decides three
    separate things: whether a value is encrypted at rest, whether it is masked in API responses,
    and whether it is redacted out of error messages and traces. It contained "password" -- which
    does NOT match "passphrase" -- and nothing matching "client_cert_p12". So the PKCS#12 bundle
    and its passphrase were stored in the clear and echoed back unredacted: a private key sitting
    in a connector row, readable by anyone with the read capability.

    Naming a config key something that sounds secret does not make it one. That is the property
    under test here, not the marker list itself.
    """

    def test_both_client_cert_fields_are_classified_as_secrets(self):
        from app.connector_probe import _is_secret_key

        for key in ("client_cert_p12", "client_cert_passphrase"):
            with self.subTest(key=key):
                self.assertTrue(_is_secret_key(key),
                                "{0} is stored and echoed in the clear".format(key))

    def test_both_values_are_in_the_redaction_set(self):
        from app.connector_probe import connector_secret_values

        row = connector(make_p12(), "hunter2")
        values = connector_secret_values(row)
        self.assertIn("hunter2", values, "the passphrase would survive into an error message")
        self.assertIn(row["config"]["client_cert_p12"], values,
                      "the bundle would survive into an error message")

    def test_a_passphrase_does_not_survive_into_a_redacted_error(self):
        from app.connector_probe import redact_connector_secrets

        row = connector(make_p12(), "hunter2")
        message = "handshake failed using passphrase hunter2"
        self.assertNotIn("hunter2", str(redact_connector_secrets(message, row)))


class WiredIntoTheFetchPathTest(_Base):
    """A module nothing calls is not a feature."""

    def test_the_fetch_request_carries_the_identity(self):
        from app import connector_fetch

        row = connector(make_p12())
        row["config"].update({"base_url": "https://partner.example.com/x", "auth_type": "none"})
        request = connector_fetch._build_request(row, {}, "t1")
        self.assertIsInstance(request.ssl_context, ssl.SSLContext)

    def test_an_ordinary_connector_carries_none(self):
        from app import connector_fetch

        row = connector()
        row["config"].update({"base_url": "https://partner.example.com/x", "auth_type": "none"})
        self.assertIsNone(connector_fetch._build_request(row, {}, "t1").ssl_context)

    def test_the_context_reaches_the_http_client_as_verify(self):
        from app import connector_fetch

        captured = {}

        class _Client:
            def __init__(self_inner, **kwargs):
                captured.update(kwargs)
            async def __aenter__(self_inner):
                return self_inner
            async def __aexit__(self_inner, *a):
                return False
            async def request(self_inner, *a, **k):
                class _R:
                    status_code = 200
                    text = '{"ok": 1}'
                return _R()

        class _Pinned:
            url = "https://93.184.216.34/x"
            host_header = "partner.example.com"
            sni_hostname = "partner.example.com"

        context = client_certs.context_for(connector(make_p12()), "t1")
        request = connector_fetch._Request(
            method="GET", url="https://partner.example.com/x", headers={}, body=None,
            timeout_ms=2000, retries=0, fingerprint="fp", display_url="https://partner.example.com/x",
            secrets=set(), ssl_context=context)

        import asyncio

        with mock.patch.object(connector_fetch, "build_pinned_request", return_value=_Pinned()):
            with mock.patch.object(connector_fetch.httpx, "AsyncClient", _Client):
                asyncio.run(connector_fetch._send(request, None))
        self.assertIs(captured.get("verify"), context,
                      "the client identity never reached the HTTP client")
        self.assertIs(captured.get("follow_redirects"), False,
                      "adding the client certificate lost follow_redirects=False")


if __name__ == "__main__":
    unittest.main()
