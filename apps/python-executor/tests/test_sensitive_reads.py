"""Access-management reads need the access-management capability.

Every GET mapped to READ, so a viewer could list every member's email, the API keys (masked, with
the key ids that are now OAuth client_ids) and the SSO configuration -- while the members handler's
docstring said "requires manage_access". Found while adding the data-subject export (plan 6.7),
which is everything held about one person and needs the same rule.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import preserve_storage_global  # noqa: E402

SENSITIVE = ("/api/v1/access/members", "/api/v1/access/keys", "/api/v1/access/sso",
             "/api/v1/data-subjects/someone")


class SensitiveReadsTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tempdir.cleanup)
        preserve_storage_global(self)
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "reads.db"))
        self.client = TestClient(app_main.app)
        self.addCleanup(self.client.close)
        tenant = app_main.storage.default_tenant_id
        self.key = {role: {"x-api-key": app_main.storage.generate_api_key_for_tenant(tenant, role=role)["plaintext"]}
                    for role in ("viewer", "policy_maker", "admin")}

    def test_viewers_and_authors_cannot_read_access_management_data(self):
        for role in ("viewer", "policy_maker"):
            for path in SENSITIVE:
                self.assertEqual(self.client.get(path, headers=self.key[role]).status_code, 403, (role, path))

    def test_an_admin_can(self):
        for path in SENSITIVE[:3]:
            self.assertEqual(self.client.get(path, headers=self.key["admin"]).status_code, 200, path)

    def test_the_callers_own_identity_stays_readable(self):
        """The dashboard asks every user who they are; that must not become admin-only."""
        for path in ("/api/v1/access/me", "/api/v1/access/roles"):
            self.assertEqual(self.client.get(path, headers=self.key["viewer"]).status_code, 200, path)


if __name__ == "__main__":
    unittest.main()
