"""Per-workspace data-protection controls — retention window + PII redaction + encryption status.

Surfaces and manages the decision-log retention window, the PII fields redacted from stored
payloads (per workspace), and the read-only at-rest encryption / archive-sink status.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

import app.main as app_main  # noqa: E402
from app.logic import redact_payload  # noqa: E402


class DataProtectionTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}
        self.tenant_id = app_main.storage.default_tenant_id

    def test_get_shows_posture(self):
        body = self.client.get("/api/v1/settings/data-protection", headers=self.headers).json()
        self.assertIn("retention_days", body)
        self.assertTrue(body["encryption_at_rest"])  # on by default
        self.assertIn("email", body["builtin_redact_keys"])  # built-in PII field
        self.assertIn("archive_sink", body)

    def test_put_updates_retention_and_pii_keys(self):
        r = self.client.put(
            "/api/v1/settings/data-protection", headers=self.headers,
            json={"retention_days": 45, "pii_redact_keys": ["account_no", "member_id"]},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["retention_days"], 45)
        self.assertEqual(sorted(body["pii_redact_keys"]), ["account_no", "member_id"])
        # Persisted + reflected on GET.
        again = self.client.get("/api/v1/settings/data-protection", headers=self.headers).json()
        self.assertEqual(again["retention_days"], 45)
        # And exposed to the redaction hot path via the cached helper.
        self.assertIn("account_no", app_main.storage.tenant_pii_redact_keys(self.tenant_id))

    def test_configured_keys_redact_stored_payloads(self):
        self.client.put("/api/v1/settings/data-protection", headers=self.headers,
                        json={"pii_redact_keys": ["account_no"]})
        keys = app_main.storage.tenant_pii_redact_keys(self.tenant_id)
        redacted = redact_payload({"account_no": "12345", "amount": 500, "email": "a@b.com"}, extra_keys=keys)
        self.assertEqual(redacted["account_no"], "***")  # tenant custom key
        self.assertEqual(redacted["email"], "***")  # built-in still applies
        self.assertEqual(redacted["amount"], 500)  # non-PII kept

    def test_retention_floor(self):
        body = self.client.put("/api/v1/settings/data-protection", headers=self.headers,
                               json={"retention_days": 0}).json()
        self.assertGreaterEqual(body["retention_days"], 1)


if __name__ == "__main__":
    unittest.main()
