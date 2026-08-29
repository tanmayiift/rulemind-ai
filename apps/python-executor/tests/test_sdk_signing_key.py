"""GET /sdk/v1/signing-key — the published bundle-verification key.

This endpoint had **zero** test references anywhere in the repo: not in
apps/python-executor/tests, not in qa/, not in e2e/, not in the SDKs. Its only
consumer was demo-app/lib/engine.dart:99.

That matters more than an untested endpoint usually would. The SDK refuses any
bundle whose signature it cannot verify, and before this endpoint existed there
was no way to obtain the public half -- so verification could never succeed. A
test that only checked the response shape would not have caught that: the
assertion that earns its keep is that the *published* key actually verifies a
*real* block signature. If the endpoint ever serves the wrong key, every SDK
silently falls back to rejecting bundles, and the shape assertion stays green.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import unittest
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from fastapi.testclient import TestClient

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

import app.main as app_main  # noqa: E402


class SdkSigningKeyTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}
        self.tenant_id = app_main.storage.default_tenant_id
        prod = app_main.storage.list_policies(status="prod", tenant_id=self.tenant_id)
        self.assertTrue(prod, "seed demo should provide at least one production policy")
        self.policy = prod[0]

    def tearDown(self):
        self.client.close()

    def _key(self):
        r = self.client.get("/sdk/v1/signing-key", headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text[:300])
        return r.json()

    def test_publishes_an_rs256_public_key(self):
        body = self._key()
        self.assertEqual(body["algorithm"], "RS256")
        self.assertIn("publicKeyPem", body)
        self.assertIsInstance(body["ephemeral"], bool)
        # A PEM that does not parse is worse than no endpoint: the SDK would
        # fail at bundle-verification time rather than here.
        key = serialization.load_pem_public_key(body["publicKeyPem"].encode("utf-8"))
        self.assertGreaterEqual(key.key_size, 2048)

    def test_the_published_key_verifies_a_real_block_signature(self):
        """The assertion the endpoint exists for."""
        pem = self._key()["publicKeyPem"]
        public_key = serialization.load_pem_public_key(pem.encode("utf-8"))

        block_resp = self.client.get(
            "/sdk/v1/blocks/{0}".format(self.policy["id"]), headers=self.headers
        )
        self.assertEqual(block_resp.status_code, 200, block_resp.text[:300])
        body = block_resp.json()
        payload = json.dumps(body["block"], separators=(",", ":"), sort_keys=True).encode("utf-8")

        # Raises InvalidSignature on mismatch; no exception means the key an SDK
        # would download genuinely validates the bundle it would download.
        public_key.verify(
            base64.b64decode(body["signature"]), payload, padding.PKCS1v15(), hashes.SHA256()
        )

    def test_a_tampered_block_fails_verification(self):
        """Guards the guard: proves the check above can actually fail."""
        pem = self._key()["publicKeyPem"]
        public_key = serialization.load_pem_public_key(pem.encode("utf-8"))
        body = self.client.get(
            "/sdk/v1/blocks/{0}".format(self.policy["id"]), headers=self.headers
        ).json()

        tampered = dict(body["block"])
        tampered["__tampered__"] = True
        payload = json.dumps(tampered, separators=(",", ":"), sort_keys=True).encode("utf-8")

        with self.assertRaises(InvalidSignature):
            public_key.verify(
                base64.b64decode(body["signature"]), payload, padding.PKCS1v15(), hashes.SHA256()
            )

    def test_is_stable_across_calls(self):
        """An SDK caches this. A key that changed per request would make every
        cached bundle unverifiable after the next restart."""
        self.assertEqual(self._key()["publicKeyPem"], self._key()["publicKeyPem"])

    def test_requires_authentication(self):
        r = self.client.get("/sdk/v1/signing-key")
        self.assertIn(r.status_code, (401, 403), f"expected auth failure, got {r.status_code}")
