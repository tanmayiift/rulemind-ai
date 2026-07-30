"""Production-hardening: DB pragmas/pool, webhook auth, access-change audit."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.setdefault("RULEMIND_ADMIN_JWT_SECRET", "rulemind-test-admin-secret")
os.environ.pop("AUTH_MODE", None)

import app.main as app_main  # noqa: E402
from app.storage import Storage  # noqa: E402


class DbTuningTests(unittest.TestCase):
    def test_sqlite_runs_in_wal_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Storage(path=os.path.join(tmp, "wal.db"))
            with s.connect() as session:
                mode = session.execute(__import__("sqlalchemy").text("PRAGMA journal_mode")).scalar()
            self.assertEqual(str(mode).lower(), "wal")


class WebhookAuthTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "wh.db"))
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def _create(self):
        r = self.client.post("/api/v1/webhooks", headers=self.headers,
                             json={"policy_id": "policy_instant_personal_loan", "payload_mapping": {}})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_webhook_gets_a_secret_even_without_one(self):
        wh = self._create()
        self.assertTrue(wh.get("secret"))  # auto-generated, returned once

    def test_unsigned_or_wrong_signature_is_rejected(self):
        wh = self._create()
        body = {"bureau_score": 780}
        # no signature -> 401
        self.assertEqual(self.client.post(f"/api/v1/webhooks/{wh['id']}", json=body).status_code, 401)
        # wrong signature -> 401
        self.assertEqual(self.client.post(f"/api/v1/webhooks/{wh['id']}", json=body,
                         headers={"x-webhook-signature": "deadbeef"}).status_code, 401)

    def test_correctly_signed_request_is_accepted(self):
        wh = self._create()
        body = {"bureau_score": 780}
        raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
        sig = hmac.new(wh["secret"].encode(), raw, hashlib.sha256).hexdigest()
        resp = self.client.post(f"/api/v1/webhooks/{wh['id']}", json=body, headers={"x-webhook-signature": sig})
        self.assertNotEqual(resp.status_code, 401, resp.text)  # signature accepted (executes)


class AccessAuditTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "audit.db"))
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def test_key_issue_and_revoke_are_audited(self):
        created = self.client.post("/api/v1/access/keys", headers=self.headers, json={"role": "viewer", "label": "read-only"}).json()
        tid = app_main.storage.default_tenant_id
        issued = app_main.storage.list_audit_events(tenant_id=tid, event_type="api_key_issued")
        self.assertTrue(issued and any(e.get("entity_id") == created["kid"] for e in issued))
        self.client.delete(f"/api/v1/access/keys/{created['kid']}", headers=self.headers)
        revoked = app_main.storage.list_audit_events(tenant_id=tid, event_type="api_key_revoked")
        self.assertTrue(revoked and any(e.get("entity_id") == created["kid"] for e in revoked))


if __name__ == "__main__":
    unittest.main()
