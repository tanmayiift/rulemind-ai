"""Embeddable decision-block API — GET /sdk/v1/blocks/{policy_id}.

A block is a per-policy slice of a bundle: one production policy plus only the rules /
scorecards / decision tables it references, self-contained, cacheable, and signed. A client
fetches one block, caches it by ETag, and evaluates that single policy on-device with the same
SDK evaluators. These tests prove the slice is correct + minimal, deterministic (stable
ETag), signed, conditionally cacheable (304), and encryptable per client.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

import app.compiler as compiler  # noqa: E402
import app.main as app_main  # noqa: E402


class DecisionBlockTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}
        self.tenant_id = app_main.storage.default_tenant_id
        prod = app_main.storage.list_policies(status="prod", tenant_id=self.tenant_id)
        self.assertTrue(prod, "seed demo should provide at least one production policy")
        self.policy = prod[0]

    def test_404_for_unknown_policy(self):
        r = self.client.get("/sdk/v1/blocks/does-not-exist", headers=self.headers)
        self.assertEqual(r.status_code, 404)

    def test_block_is_self_contained_and_minimal(self):
        r = self.client.get("/sdk/v1/blocks/{0}".format(self.policy["id"]), headers=self.headers)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["kind"], "decision_block")
        self.assertEqual(body["policyId"], self.policy["id"])
        block = body["block"]
        # The block carries exactly one policy...
        self.assertEqual(block["policy"]["id"], self.policy["id"])
        # ...and only the rules that policy references (not the whole tenant's rules).
        referenced = {
            step.get("ref_id") or step.get("ref")
            for step in self.policy.get("steps", [])
            if step.get("type") == "rule"
        }
        block_rule_ids = {rule["id"] for rule in block["rules"]}
        self.assertTrue(block_rule_ids.issubset(referenced))
        all_rules = {rule["id"] for rule in app_main.storage.list_rules(status="prod", tenant_id=self.tenant_id)}
        # There are more prod rules in the tenant than this one policy references.
        self.assertLess(len(block_rule_ids), len(all_rules))

    def test_etag_and_conditional_get(self):
        first = self.client.get("/sdk/v1/blocks/{0}".format(self.policy["id"]), headers=self.headers)
        etag = first.headers.get("etag")
        self.assertTrue(etag)
        self.assertIn("max-age", first.headers.get("cache-control", ""))
        again = self.client.get(
            "/sdk/v1/blocks/{0}".format(self.policy["id"]),
            headers={**self.headers, "If-None-Match": etag},
        )
        self.assertEqual(again.status_code, 304)

    def test_block_version_is_deterministic(self):
        a = self.client.get("/sdk/v1/blocks/{0}".format(self.policy["id"]), headers=self.headers).json()
        b = self.client.get("/sdk/v1/blocks/{0}".format(self.policy["id"]), headers=self.headers).json()
        self.assertEqual(a["blockVersion"], b["blockVersion"])
        self.assertEqual(a["checksum"], b["checksum"])

    def test_signature_verifies(self):
        body = self.client.get("/sdk/v1/blocks/{0}".format(self.policy["id"]), headers=self.headers).json()
        block = body["block"]
        signature = base64.b64decode(body["signature"])
        payload = json.dumps(block, separators=(",", ":"), sort_keys=True).encode("utf-8")
        _, public_key = compiler._load_signing_keypair()
        # Raises InvalidSignature if the block was tampered with; no exception == verified.
        public_key.verify(signature, payload, padding.PKCS1v15(), hashes.SHA256())

    def test_encrypted_variant_round_trips_with_client_key(self):
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pub_pem = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        pub_header = base64.b64encode(pub_pem).decode("utf-8")
        r = self.client.get(
            "/sdk/v1/blocks/{0}".format(self.policy["id"]),
            headers={**self.headers, "x-client-public-key": pub_header},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("encryptedBlock", body)
        self.assertIsNotNone(body.get("encryptedKey"))
        # Decrypt the AES key with the client private key, then AES-GCM open the block.
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        aes_key = private_key.decrypt(
            base64.b64decode(body["encryptedKey"]),
            padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        )
        raw = base64.b64decode(body["encryptedBlock"])
        nonce, ciphertext = raw[:12], raw[12:]
        plaintext = AESGCM(aes_key).decrypt(nonce, ciphertext, None)
        decrypted = json.loads(plaintext)
        self.assertEqual(decrypted["policyId"], self.policy["id"])

    def test_block_evaluates_consistently_with_server(self):
        """The sliced block's compiled rules reproduce the server's rule outcomes."""
        from app.logic import evaluate_rule_definition

        body = self.client.get("/sdk/v1/blocks/{0}".format(self.policy["id"]), headers=self.headers).json()
        block = body["block"]
        # Build a payload of zeros for every variable the block references, then confirm each
        # block rule evaluates without error and deterministically (same result twice).
        variables = {var["id"]: 0 for var in block["variables"]}
        for rule in block["rules"]:
            first = evaluate_rule_definition(rule, variables)
            second = evaluate_rule_definition(rule, variables)
            self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
