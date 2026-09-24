"""`POST /api/mobile/v1/auth/demo` handed any caller the workspace's owner API key.

Found by the plan 5.1 credentials sweep, which sends every documented operation with no
credentials: the endpoint is public (the middleware does not gate /api/mobile/v1 at all, and the
handler checks nothing) and its response carries `apiKey` -- the DEFAULT tenant's key, in
plaintext. On a laptop that key is the committed development default, so handing it out
discloses nothing. In production it cannot be: `verify_production_secrets` refuses to boot until
RULEMIND_DEV_API_KEY is set to a real secret, the seed gives the default tenant exactly that key,
and its role is `owner`. So one unauthenticated POST against a correctly configured production
install returned the owner key of the workspace that install exists to serve.

The demo now answers only when the key it would hand out is the published default, and never
mints a key for an anonymous caller.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import preserve_storage_global  # noqa: E402

PRODUCTION_KEY = "rm_live_" + "7Qx2" * 8   # what an operator sets RULEMIND_DEV_API_KEY to


class MobileDemoAccessTests(unittest.TestCase):
    def _install(self, dev_key=None):
        tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tempdir.cleanup)
        preserve_storage_global(self)
        env = {"RULEMIND_DEV_API_KEY": dev_key} if dev_key else {}
        with mock.patch.dict(os.environ, env):
            if not dev_key:
                os.environ.pop("RULEMIND_DEV_API_KEY", None)
            app_main.storage = Storage(path=os.path.join(tempdir.name, "demo.db"))
        client = TestClient(app_main.app)
        self.addCleanup(client.close)
        return client

    def test_a_configured_install_does_not_hand_its_key_to_an_anonymous_caller(self):
        client = self._install(PRODUCTION_KEY)
        # The key is live and owner-level: this is what the demo endpoint used to give away.
        self.assertEqual(client.get("/api/v1/access/keys", headers={"x-api-key": PRODUCTION_KEY}).status_code, 200)
        response = client.post("/api/mobile/v1/auth/demo")
        self.assertEqual(response.status_code, 403, response.text[:200])
        self.assertNotIn(PRODUCTION_KEY, response.text)

    def test_no_key_is_minted_for_an_anonymous_caller(self):
        client = self._install(PRODUCTION_KEY)
        before = len(app_main.storage.list_api_keys(app_main.storage.default_tenant_id))
        client.post("/api/mobile/v1/auth/demo")
        self.assertEqual(len(app_main.storage.list_api_keys(app_main.storage.default_tenant_id)), before)

    def test_a_development_install_still_gets_the_demo(self):
        """The published default is in this repository; handing it out discloses nothing."""
        client = self._install()
        response = client.post("/api/mobile/v1/auth/demo")
        self.assertEqual(response.status_code, 200, response.text[:200])
        self.assertEqual(response.json()["mode"], "demo")


if __name__ == "__main__":
    unittest.main()
